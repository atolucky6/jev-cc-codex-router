#!/usr/bin/env python3
"""jev-router: per-turn model routing proxy for Codex (Responses API).

Sits between Codex and any OpenAI-Responses-compatible upstream. On each new user
turn it asks Jev (TypeSafe System One / OpenRouter) which model tier and reasoning level
the task needs, rewrites the request's `model` and `reasoning` fields, and reuses that
choice for the rest of the turn.
Also retries flaky upstream 400/502/503/504 on POST /responses.
Logs decisions to SQLite database (decisions.db) and provides a web dashboard.
Configuration: environment variables, see .env.example and README.md.
"""

from __future__ import annotations

import collections
import difflib
import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _load_env_file() -> None:
    """Load key-value pairs from .env next to this script into os.environ if not already set."""
    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.isfile(env_file):
        with open(env_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:].strip()
                if "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    os.environ.setdefault(k, v)


_load_env_file()


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


START_TIME = time.time()
LISTEN_HOST = _env("JEV_LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(_env("JEV_LISTEN_PORT", "8787"))
UPSTREAM_ORIGIN = _env("JEV_UPSTREAM", "http://127.0.0.1:8317").rstrip("/")
UPSTREAM_KEY = _env("JEV_UPSTREAM_KEY", "sk-4a7dc2e928a61731-87c6c6-e2d7ca9c")
_default_url = (
    "https://openrouter.ai/api/alpha/decisions"
    if (os.environ.get("OPENROUTER_API_KEY") or "openrouter" in os.environ.get("JEV_API_URL", ""))
    else "https://api.typesafe.ai/v1/systemone"
)
JEV_URL = _env("JEV_API_URL", _default_url)
_default_model = "typesafe/jev-1.13" if "openrouter" in JEV_URL else "jev-latest"
JEV_MODEL = _env("JEV_MODEL", _default_model)
DEBUG_DUMPS = _env("JEV_DEBUG", "0") in ("1", "true", "True")
LOG = _env("JEV_LOG", os.path.join(os.path.dirname(os.path.abspath(__file__)), "decisions.jsonl"))
DB_PATH = _env("JEV_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "decisions.db"))
DB_LOCK = threading.Lock()
SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")

REWRITE = _env("JEV_REWRITE", "1") not in ("0", "false", "False", "")
PLANNING_BOOST = _env("JEV_PLANNING_BOOST", "1") not in ("0", "false", "False", "")
AUTO_ESCALATION = _env("JEV_AUTO_ESCALATION", "1") not in ("0", "false", "False", "")
DEFAULT_MODEL = _env("JEV_FALLBACK_MODEL", "gpt-6.1-sol")
MAP = {
    "luna": _env("JEV_MODEL_LUNA", "gpt-6-luna"),
    "terra": _env("JEV_MODEL_TERRA", "gpt-6-sol"),
    "sol": _env("JEV_MODEL_SOL", "gpt-6.1-sol"),
    "astra": _env("JEV_MODEL_ASTRA", "gpt-6-astra"),
}

LEASE: dict[str, str] = {}
LEASE_EFFORT: dict[str, str] = {}
BASE: dict[str, str] = {}
LAST_PROBS: dict = {}
CONTINUE_WORDS = {
    "继续", "继续吧", "接着", "接着来", "go on", "continue", "keep going",
    "proceed", "go", "ok", "好", "好的", "可以", "行", "是的", "yes",
    "你再试试", "再试试", "再试一次", "重试", "再来", "再来一次", "retry", "try again", "again"
}
SKIP_PREFIXES = (
    "Generate a concise, single-line task title",
    "You are a helpful assistant. You will be presented with a user prompt",
    "# AGENTS.md instructions",
    "Analyze this rollout and produce JSON",
)
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "host",
}
TOOL_TYPES = {
    "function_call",
    "function_call_output",
    "tool_result",
    "custom_tool_call",
    "custom_tool_call_output",
}


def init_db() -> None:
    """Initialize SQLite database schema for storing routing decisions."""
    with DB_LOCK:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
            conn = sqlite3.connect(DB_PATH)
            conn.execute("""
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                datetime TEXT NOT NULL,
                session_id TEXT,
                is_new_turn INTEGER,
                prompt TEXT,
                tier TEXT,
                confidence REAL,
                selected_model TEXT,
                original_model TEXT,
                probabilities TEXT,
                escalation TEXT,
                error_msg TEXT,
                reasoning_effort TEXT,
                reasoning_conf REAL
            )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_decisions_timestamp ON decisions(timestamp DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_decisions_tier ON decisions(tier)")
            
            # Migration check for existing databases
            for col_name, col_type in (
                ("reasoning_effort", "TEXT"),
                ("reasoning_conf", "REAL"),
                ("opt_status", "TEXT"),
                ("optimized_prompt", "TEXT"),
                ("opt_latency_ms", "REAL"),
                ("diff_summary", "TEXT"),
                ("original_tokens_est", "INTEGER"),
                ("optimized_tokens_est", "INTEGER"),
            ):
                try:
                    conn.execute(f"ALTER TABLE decisions ADD COLUMN {col_name} {col_type}")
                except Exception:
                    pass

            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[DB INIT ERROR] {e}", flush=True)


def log_decision_db(meta: dict, session_id: str = "") -> None:
    """Insert a routing decision record into SQLite."""
    now = time.time()
    dt_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    with DB_LOCK:
        try:
            conn = sqlite3.connect(DB_PATH, timeout=5.0)
            conn.execute(
                """
                INSERT INTO decisions (
                    timestamp, datetime, session_id, is_new_turn,
                    prompt, tier, confidence, selected_model,
                    original_model, probabilities, escalation, error_msg,
                    reasoning_effort, reasoning_conf,
                    opt_status, optimized_prompt, opt_latency_ms,
                    diff_summary, original_tokens_est, optimized_tokens_est
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    dt_str,
                    session_id,
                    1 if meta.get("new") else 0,
                    meta.get("raw_prompt") or meta.get("prompt") or "",
                    meta.get("tier") or "",
                    float(meta.get("conf") or 0.0),
                    meta.get("out") or "",
                    meta.get("in") or "",
                    json.dumps(meta.get("probs") or {}),
                    meta.get("esc_tier") or ("planning" if meta.get("planning") else ""),
                    meta.get("err") or meta.get("esc_err") or "",
                    meta.get("effort") or "medium",
                    float(meta.get("effort_conf") or 0.0),
                    meta.get("opt_status") or "",
                    meta.get("optimized_prompt") or "",
                    float(meta.get("opt_latency_ms") or 0.0),
                    meta.get("diff_summary") or "",
                    int(meta.get("original_tokens_est") or 0),
                    int(meta.get("optimized_tokens_est") or 0),
                ),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[DB ERROR] Failed to log decision: {e}", flush=True)


def query_stats() -> dict:
    """Query high-level routing statistics from SQLite."""
    with DB_LOCK:
        try:
            conn = sqlite3.connect(DB_PATH, timeout=5.0)
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM decisions")
            total = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM decisions WHERE is_new_turn = 1")
            total_turns = cur.fetchone()[0]

            cur.execute("SELECT tier, COUNT(*) FROM decisions GROUP BY tier")
            tier_counts = {row[0] or "other": row[1] for row in cur.fetchall()}

            cur.execute("SELECT selected_model, COUNT(*) FROM decisions GROUP BY selected_model")
            model_counts = {row[0] or "unknown": row[1] for row in cur.fetchall()}

            cur.execute("SELECT reasoning_effort, COUNT(*) FROM decisions WHERE reasoning_effort IS NOT NULL AND reasoning_effort != '' GROUP BY reasoning_effort")
            effort_counts = {row[0]: row[1] for row in cur.fetchall()}

            # Optimizer metrics
            cur.execute("SELECT COUNT(*) FROM decisions WHERE opt_status = 'OPTIMIZED'")
            optimized_count = cur.fetchone()[0]

            cur.execute("SELECT AVG(opt_latency_ms) FROM decisions WHERE opt_status = 'OPTIMIZED'")
            avg_opt_latency = cur.fetchone()[0] or 0.0

            cur.execute("SELECT SUM(original_tokens_est), SUM(optimized_tokens_est) FROM decisions WHERE opt_status = 'OPTIMIZED'")
            tok_row = cur.fetchone()
            sum_orig_tok = (tok_row[0] or 0) if tok_row else 0
            sum_opt_tok = (tok_row[1] or 0) if tok_row else 0

            conn.close()
            return {
                "total_records": total,
                "total_turns": total_turns,
                "tier_counts": tier_counts,
                "model_counts": model_counts,
                "effort_counts": effort_counts,
                "optimized_count": optimized_count,
                "avg_opt_latency_ms": round(avg_opt_latency, 1),
                "optimization_rate": round((optimized_count / total_turns * 100), 1) if total_turns > 0 else 0.0,
                "token_expansion_ratio": round((sum_opt_tok / sum_orig_tok), 2) if sum_orig_tok > 0 else 1.0,
                "mapping": MAP,
                "upstream": UPSTREAM_ORIGIN,
                "jev_model": JEV_MODEL,
                "uptime": int(time.time() - START_TIME),
            }
        except Exception as e:
            return {
                "error": str(e),
                "total_records": 0,
                "total_turns": 0,
                "tier_counts": {},
                "model_counts": {},
                "effort_counts": {},
                "optimized_count": 0,
                "avg_opt_latency_ms": 0.0,
                "optimization_rate": 0.0,
                "token_expansion_ratio": 1.0,
                "mapping": MAP,
                "upstream": UPSTREAM_ORIGIN,
                "jev_model": JEV_MODEL,
                "uptime": int(time.time() - START_TIME),
            }


def query_recent_decisions(limit: int = 50) -> list[dict]:
    """Query recent routing decisions ordered by timestamp descending."""
    with DB_LOCK:
        try:
            conn = sqlite3.connect(DB_PATH, timeout=5.0)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT * FROM decisions ORDER BY timestamp DESC LIMIT ?", (limit,))
            rows = [dict(r) for r in cur.fetchall()]
            conn.close()
            return rows
        except Exception as e:
            print(f"[DB ERROR] query_recent_decisions: {e}", flush=True)
            return []


def query_recent_diffs(limit: int = 50) -> list[dict]:
    """Query recent prompt diffs ordered by timestamp descending."""
    with DB_LOCK:
        try:
            conn = sqlite3.connect(DB_PATH, timeout=5.0)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, timestamp, datetime, prompt, optimized_prompt, opt_status, opt_latency_ms,
                       diff_summary, original_tokens_est, optimized_tokens_est, selected_model, tier, reasoning_effort
                FROM decisions
                WHERE opt_status IS NOT NULL AND opt_status != ''
                ORDER BY timestamp DESC LIMIT ?
                """,
                (limit,),
            )
            rows = [dict(r) for r in cur.fetchall()]
            conn.close()
            return rows
        except Exception as e:
            print(f"[DB ERROR] query_recent_diffs: {e}", flush=True)
            return []


def load_key() -> str:
    """Read OPENROUTER_API_KEY or TYPESAFE_API_KEY from environment, else from .env."""
    if os.environ.get("OPENROUTER_API_KEY"):
        return os.environ["OPENROUTER_API_KEY"]
    if os.environ.get("TYPESAFE_API_KEY"):
        return os.environ["TYPESAFE_API_KEY"]
    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.isfile(env_file):
        with open(env_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("export "):
                    line = line[7:].strip()
                if line.startswith("OPENROUTER_API_KEY="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    os.environ.setdefault("OPENROUTER_API_KEY", val)
                elif line.startswith("TYPESAFE_API_KEY="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    os.environ.setdefault("TYPESAFE_API_KEY", val)
    return os.environ.get("OPENROUTER_API_KEY") or os.environ.get("TYPESAFE_API_KEY", "")


KEY = load_key()


def log(obj: dict) -> None:
    """Write routing decision log to decisions.jsonl."""
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[LOG ERROR] {e}", flush=True)


def ask_jev(text: str, extra: str = "") -> tuple[str, float, str, float]:
    """Ask Jev for both Model Tier and Reasoning Effort in a single request."""
    if not KEY:
        return "sol", 0.0, "medium", 0.0
    payload = {
        "model": JEV_MODEL,
        "state": {"task": text[-6000:], "via": "jev-router"},
        "questions": {
            "tier": {
                "type": "choice",
                "instructions": (
                    "Pick the cheapest tier that can finish THIS user turn. "
                    "Ignore the word 继续 when grading; grade the real task. If the input includes earlier conversation, grade the whole ongoing task, not just the last short sentence. "
                    "git/repo/仓库 sync is never luna. "
                    "Planning, architecture design, breakdown, or writing specs (such as Superpowers plan mode) is NEVER luna or terra; pick sol (standard feature plan) or astra (system/architecture plan). "
                    "If unsure, pick terra not luna, sol not astra."
                    + extra
                ),
                "criteria": {
                    "luna": (
                        "Trivial, mechanical, one-shot. "
                        "git status/pull/push/fetch only; rename a symbol; format; typo; "
                        "change one obvious line or one file with a fully specified edit; "
                        "reply to a greeting. No design, no planning, and no debugging."
                    ),
                    "terra": (
                        "Bounded local work with clear requirements. "
                        "Single-repo rebase/merge with a few known conflicts; "
                        "repo sync in one repository; "
                        "add or fix a function/test in one area; small feature in existing files; "
                        "follow an already agreed approach or already written plan. Not for creating plans."
                    ),
                    "sol": (
                        "Non-trivial implementation or diagnosis. "
                        "Cross-file changes; unclear bug; multi-repo or submodule sync with "
                        "divergent history or custom sync scripts; implement something that "
                        "needs reading several modules; writing feature plans, specs, and task breakdowns. Not a full architecture review."
                    ),
                    "astra": (
                        "High-stakes or structural. "
                        "Architecture design and system-wide planning, authz/authn, concurrency, data migration, "
                        "monorepo/CI/permission redesign, security review, final audit."
                    ),
                },
            },
            "reasoning_effort": {
                "type": "choice",
                "instructions": "Pick the reasoning/thinking depth needed to complete this task correctly.",
                "criteria": {
                    "low": "Simple, direct tasks, clear UI tweaks, obvious mechanical edits, greeting, minimal thinking needed.",
                    "medium": "Standard programming tasks, logic debugging, component comparison, moderate algorithmic reasoning.",
                    "high": "Deep architectural reasoning, planning & system design, subtle race conditions, complex multi-component refactoring, security audits."
                },
            },
        },
    }
    req_headers = {
        "Authorization": f"Bearer {KEY}",
        "Content-Type": "application/json",
    }
    if "openrouter" in JEV_URL:
        req_headers["HTTP-Referer"] = "https://github.com/peterwanghot/jev-cc-codex-router"
        req_headers["X-Title"] = "Jev Codex Router"

    req = urllib.request.Request(
        JEV_URL,
        data=json.dumps(payload).encode(),
        headers=req_headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = json.load(r)
    answers = raw.get("answers") or {}

    t_ans = answers.get("tier") or {}
    tier = t_ans.get("choice") or t_ans.get("value") or "sol"
    t_conf = t_ans.get("confidence")
    if t_conf is None:
        probs = t_ans.get("probabilities") or {}
        t_conf = max(probs.values()) if probs else 0.0
    t_conf = float(t_conf or 0)
    LAST_PROBS["v"] = t_ans.get("probabilities") or {}
    if tier not in MAP:
        tier = "sol"

    e_ans = answers.get("reasoning_effort") or {}
    effort = e_ans.get("choice") or e_ans.get("value") or "medium"
    e_conf = e_ans.get("confidence")
    if e_conf is None:
        probs = e_ans.get("probabilities") or {}
        e_conf = max(probs.values()) if probs else 0.0
    e_conf = float(e_conf or 0)
    LAST_PROBS["effort"] = e_ans.get("probabilities") or {}
    if effort not in ("low", "medium", "high"):
        effort = "medium"

    return tier, t_conf, effort, e_conf


def _item_text(item: dict) -> str:
    content = item.get("content")
    if content is None:
        content = item.get("text") or ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for x in content:
            if isinstance(x, str):
                parts.append(x)
            elif isinstance(x, dict):
                parts.append(x.get("text") or x.get("input_text") or x.get("content") or "")
        return " ".join(str(p) for p in parts if p)
    return str(content) if content else ""


def last_user_text(body: dict) -> tuple[str | None, bool]:
    items = body.get("input") or body.get("messages") or []
    if isinstance(items, str):
        text = items.strip()
        if text and not text.startswith(SKIP_PREFIXES):
            return text[:3500], True
        return None, False
    if not isinstance(items, list):
        return None, False
    for it in reversed(items):
        if not isinstance(it, dict):
            continue
        typ = it.get("type") or ""
        role = it.get("role") or ""
        if typ in TOOL_TYPES or role == "tool":
            return None, False
        if role != "user":
            continue
        text = _item_text(it).strip()
        if not text or text.startswith(SKIP_PREFIXES):
            continue
        return text[:3500], True
    return None, False


HIST_TURNS = 50
HIST_ITEM = 240
HIST_BUDGET = 4200


def build_history(body: dict, current: str) -> str:
    items = body.get("input") or []
    if not isinstance(items, list):
        return ""
    rows = []
    for it in items:
        if not isinstance(it, dict) or it.get("type", "message") != "message":
            continue
        role = it.get("role")
        if role not in ("user", "assistant"):
            continue
        t = _item_text(it).strip()
        if not t or t.startswith(SKIP_PREFIXES) or t.startswith("<"):
            continue
        rows.append(("用户" if role == "user" else "助手", t))
    if rows and rows[-1][0] == "用户" and rows[-1][1][:200] == current[:200]:
        rows.pop()
    rows = rows[-HIST_TURNS:]
    out, used = [], 0
    for who, t in reversed(rows):
        line = f"{who}: {t[:HIST_ITEM]}"
        if used + len(line) > HIST_BUDGET:
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(reversed(out))


ERR_RX = re.compile(r"(?i)(traceback \(most recent|exit code:? *[1-9]|exited with code [1-9]|exit status [1-9]|assertion ?error|fatal:|panic:|command not found|no such file or directory|permission denied|npm err|error\[e\d+\]|\bFAILED\b|\d+ failed|syntaxerror|modulenotfounderror|importerror|merge conflict|CONFLICT \()")
RANK = {MAP["luna"]: 0, MAP["terra"]: 1, MAP["sol"]: 2, MAP["astra"]: 3}
ERR_STREAK: dict[str, int] = {}
ERR_LAST_ASK: dict[str, float] = {}
ERR_COOLDOWN = 20.0

# Default keywords for detecting Planning, Brainstorming, Specs, and System Architecture
DEFAULT_PLANNING_KEYWORDS = [
    "planning",
    "implementation plan",
    "action plan",
    "step-by-step plan",
    "execution plan",
    "design spec",
    "technical spec",
    "specs",
    "brainstorm",
    "brainstorming",
    "superpowers:writing-plans",
    "superpowers:executing-plans",
    "superpowers:brainstorming",
    "subagent-driven-development",
    "/plan",
    "/planning",
    "/grill-me",
    "/boost",
    "lập kế hoạch",
    "lên kế hoạch",
    "bản kế hoạch",
    "thiết kế kiến trúc",
    "thiết kế hệ thống",
    "phân tích kiến trúc",
    "lên plan",
    "tạo plan",
    "viết plan",
    "review plan",
    "sửa plan",
    "thảo luận plan",
    "xây dựng plan",
    "plan cho",
    "plan để",
    "规划",
    "制定计划",
    "架构设计",
    "方案设计",
]

DEFAULT_ARCH_KEYWORDS = [
    "system architecture",
    "microservices",
    "distributed system",
    "infrastructure redesign",
    "database migration",
    "schema migration",
    "db migration",
    "concurrency",
    "race condition",
    "authz",
    "authn",
    "authorization",
    "authentication",
    "security audit",
    "security review",
    "thiết kế kiến trúc",
    "kiến trúc hệ thống",
    "phân tán",
    "bảo mật hệ thống",
    "migration database",
]

CURRENT_PLANNING_KEYWORDS = list(DEFAULT_PLANNING_KEYWORDS)
CURRENT_ARCH_KEYWORDS = list(DEFAULT_ARCH_KEYWORDS)


def compile_keyword_rx(keywords: list[str]) -> re.Pattern:
    parts = []
    for k in keywords:
        k = k.strip()
        if not k:
            continue
        esc = re.escape(k)
        if re.match(r"^\w+$", k):
            parts.append(r"\b" + esc + r"\b")
        else:
            parts.append(esc)
    if not parts:
        return re.compile(r"(?!)")
    return re.compile("|".join(parts), re.IGNORECASE)


PLANNING_RX = compile_keyword_rx(CURRENT_PLANNING_KEYWORDS)
ARCH_RX = compile_keyword_rx(CURRENT_ARCH_KEYWORDS)

# ================= PROMPT OPTIMIZER MODULE =================
PLACEHOLDER_REGEX = re.compile(r"(\{\{[^{}\n\r]+\}\}|\$\{[^{}\n\r]+\}|\b\{[a-zA-Z0-9_]+\}\b)")

AUTOMATED_PATTERNS = [
    re.compile(r"(?i)(Traceback \(most recent|exit code:? *\d+|exited with code \d+|exit status \d+)"),
    re.compile(r"(?i)(\[tool_output\]|\[execution_result\]|COMMAND_EXIT_CODE|command not found)"),
    re.compile(r"(?i)(npm ERR!|fatal: not a git repository|SyntaxError:|ImportError:|AssertionError)"),
    re.compile(r"(?i)(diff --git a\/|index [0-9a-f]{7}\.\.[0-9a-f]{7}|@@ -\d+,\d+ \+\d+,\d+ @@)"),
    re.compile(r"(?i)(superpowers:|agent_loop|<system_generated>|subagent-driven-development)"),
]

SHORT_CONFIRMATIONS = {
    "yes", "no", "ok", "okay", "sure", "proceed", "continue", "y", "n",
    "done", "cancel", "confirm", "approve", "go ahead", "run it", "looks good",
    "lgtm", "đồng ý", "tiếp tục", "chạy đi", "ok rồi", "được rồi", "ok tiếp tục đi",
    "tiếp tục đi", "làm đi", "chạy tiếp", "cứ làm đi", "xong rồi", "được đấy", "tiếp đi"
}

DEFAULT_OPTIMIZER_SYSTEM_PROMPT = (
    "You are an expert Prompt Optimization Engine. Your task is to rewrite and refine human user prompts "
    "before they are passed to an upstream reasoning LLM.\n\n"
    "CRITICAL RULES:\n"
    "1. PRESERVE CORE INTENT & TECHNICAL CONSTRAINTS: Never change the fundamental goal, technical stack, or constraints.\n"
    "2. PRESERVE PLACEHOLDERS & CODE: Any {{variable}}, ${variable}, file paths, or exact code blocks MUST be retained exactly.\n"
    "3. CLARIFY & STRUCTURE: Disambiguate vague terminology, add missing context structure, and make output format requirements concrete.\n"
    "4. NO CHATTER / DIRECT OUTPUT ONLY: Output ONLY the enhanced prompt content directly. Do not include introductory or explanatory remarks."
)

OPT_ENABLED = True
OPT_URL = "https://openrouter.ai/api/v1/chat/completions"
OPT_MODEL = "google/gemini-2.5-flash"
OPT_KEY = ""
OPT_TIMEOUT = 3.5
OPT_TEMPERATURE = 0.3
OPT_MAX_TOKENS = 1200
OPT_SYSTEM_PROMPT = DEFAULT_OPTIMIZER_SYSTEM_PROMPT
OPT_MIN_CHARS = 12
OPT_MIN_WORDS = 3
OPT_PRESERVE_VARS = True
OPT_SKIP_SHORT = True
OPT_LOOP_MARKERS = [
    "superpowers:",
    "autonomous execution",
    "agent_loop",
    "subagent",
    "tool_call_id",
    "Traceback (most recent",
    "exit code:",
    "diff --git",
]


def extract_placeholders(text: str) -> set[str]:
    return set(PLACEHOLDER_REGEX.findall(text))


def verify_variable_integrity(original: str, optimized: str) -> tuple[bool, list[str]]:
    orig_placeholders = extract_placeholders(original)
    if not orig_placeholders:
        return True, []
    missing = [ph for ph in orig_placeholders if ph not in optimized]
    return len(missing) == 0, missing


def check_prompt_heuristic(candidate_content: str) -> tuple[bool, str]:
    if not candidate_content:
        return False, "EMPTY_PROMPT"

    cleaned_lower = candidate_content.strip().lower()
    words = cleaned_lower.split()

    if OPT_SKIP_SHORT:
        if cleaned_lower in SHORT_CONFIRMATIONS:
            return False, "SHORT_CONFIRMATION"
        if len(words) <= 4 and any(sc in cleaned_lower for sc in ("tiếp tục", "đồng ý", "chạy đi", "go ahead", "looks good", "ok rồi", "được rồi", "làm đi")):
            return False, "SHORT_CONFIRMATION"

    if len(candidate_content) < OPT_MIN_CHARS or len(words) < OPT_MIN_WORDS:
        return False, "BELOW_MIN_THRESHOLD"

    for rx in AUTOMATED_PATTERNS:
        if rx.search(candidate_content):
            return False, "AUTOMATED_EXECUTION_TRACE"

    for marker in OPT_LOOP_MARKERS:
        if marker.lower() in cleaned_lower:
            return False, f"EXCLUDED_MARKER:{marker}"

    return True, "HUMAN_INPUT_VERIFIED"


def run_prompt_optimizer(original_prompt: str) -> tuple[str, bool, str, float]:
    """Runs prompt through the optimizer model before Jev classification."""
    if not OPT_ENABLED:
        return original_prompt, False, "OPTIMIZER_DISABLED", 0.0

    api_key = OPT_KEY or KEY
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Jev-Codex-Router/1.0",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {
        "model": OPT_MODEL,
        "temperature": OPT_TEMPERATURE,
        "max_tokens": OPT_MAX_TOKENS,
        "messages": [
            {"role": "system", "content": OPT_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Please optimize and clarify the following raw human prompt. "
                    f"Retain all technical details, variable placeholders, and constraints. "
                    f"Output ONLY the optimized prompt directly:\n\n{original_prompt}"
                ),
            },
        ],
    }

    t0 = time.time()
    try:
        req = urllib.request.Request(OPT_URL, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=OPT_TIMEOUT) as resp:
            raw_resp = json.loads(resp.read().decode("utf-8"))

        latency_ms = round((time.time() - t0) * 1000, 1)
        choices = raw_resp.get("choices") or []
        if not choices or not isinstance(choices, list):
            return original_prompt, False, "FALLBACK_EMPTY_CHOICES", latency_ms

        opt_text = choices[0].get("message", {}).get("content", "").strip()
        if not opt_text:
            return original_prompt, False, "FALLBACK_EMPTY_CONTENT", latency_ms

        if opt_text.startswith("```") and opt_text.endswith("```"):
            lines = opt_text.splitlines()
            if len(lines) >= 3:
                opt_text = "\n".join(lines[1:-1]).strip()

        if OPT_PRESERVE_VARS:
            valid, missing = verify_variable_integrity(original_prompt, opt_text)
            if not valid:
                return original_prompt, False, f"FALLBACK_VARIABLE_MUTATED:{','.join(missing)}", latency_ms

        return opt_text, True, "OPTIMIZED", latency_ms

    except urllib.error.HTTPError as e:
        latency_ms = round((time.time() - t0) * 1000, 1)
        err_msg = f"HTTP_{e.code}"
        try:
            err_body = e.read().decode("utf-8", errors="replace")
            err_json = json.loads(err_body)
            err_msg += f": {err_json.get('error', {}).get('message', err_body[:100])}"
        except Exception:
            pass
        return original_prompt, False, f"FALLBACK_ERROR:{err_msg}", latency_ms

    except urllib.error.URLError as e:
        latency_ms = round((time.time() - t0) * 1000, 1)
        if "timed out" in str(e).lower() or isinstance(e.reason, TimeoutError):
            return original_prompt, False, "FALLBACK_TIMEOUT", latency_ms
        return original_prompt, False, f"FALLBACK_NETWORK_ERROR:{str(e.reason)}", latency_ms

    except Exception as e:
        latency_ms = round((time.time() - t0) * 1000, 1)
        return original_prompt, False, f"FALLBACK_EXCEPTION:{str(e)}", latency_ms


def generate_diff_summary(original: str, optimized: str) -> dict:
    orig_chars = len(original)
    opt_chars = len(optimized)
    orig_tokens_est = max(1, int(orig_chars / 3.8))
    opt_tokens_est = max(1, int(opt_chars / 3.8))

    diff_lines = list(difflib.unified_diff(
        original.splitlines(keepends=True),
        optimized.splitlines(keepends=True),
        fromfile="Original",
        tofile="Optimized",
        n=3
    ))
    return {
        "original_tokens_est": orig_tokens_est,
        "optimized_tokens_est": opt_tokens_est,
        "token_delta": opt_tokens_est - orig_tokens_est,
        "unified_diff": "".join(diff_lines)
    }


def update_last_user_prompt(body: dict, new_text: str) -> bool:
    """Updates the content of the latest human user message in body in-place."""
    if "input" in body and isinstance(body["input"], str):
        body["input"] = new_text
        return True
    if "messages" in body and isinstance(body["messages"], str):
        body["messages"] = new_text
        return True

    items = body.get("input")
    if not isinstance(items, list):
        items = body.get("messages")
    if not isinstance(items, list):
        return False

    for it in reversed(items):
        if not isinstance(it, dict):
            continue
        typ = it.get("type") or ""
        role = it.get("role") or ""
        if typ in TOOL_TYPES or role == "tool":
            return False
        if role != "user":
            continue

        content = it.get("content")
        if isinstance(content, str):
            it["content"] = new_text
            return True
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    if "text" in part:
                        part["text"] = new_text
                        return True
                    if "input_text" in part:
                        part["input_text"] = new_text
                        return True
            it["content"] = new_text
            return True
        elif "text" in it:
            it["text"] = new_text
            return True
        else:
            it["content"] = new_text
            return True
    return False


def sync_to_env() -> None:
    """Sync model and routing configurations back to .env file."""
    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.isfile(env_file):
        return
    updates = {
        "JEV_MODEL_LUNA": MAP.get("luna", "gpt-6-luna"),
        "JEV_MODEL_TERRA": MAP.get("terra", "gpt-6-sol"),
        "JEV_MODEL_SOL": MAP.get("sol", "gpt-6.1-sol"),
        "JEV_MODEL_ASTRA": MAP.get("astra", "gpt-6-astra"),
        "JEV_FALLBACK_MODEL": DEFAULT_MODEL,
        "JEV_UPSTREAM": UPSTREAM_ORIGIN,
        "JEV_UPSTREAM_KEY": UPSTREAM_KEY,
        "JEV_REWRITE": "1" if REWRITE else "0",
        "JEV_MODEL": JEV_MODEL,
    }
    if KEY and not KEY.startswith("sk-or-v1-...") and "..." not in KEY:
        updates["OPENROUTER_API_KEY"] = KEY

    try:
        with open(env_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
        new_lines = []
        seen = set()
        for line in lines:
            trimmed = line.strip()
            if trimmed.startswith("#") or not trimmed or "=" not in trimmed:
                new_lines.append(line)
                continue
            k, _ = trimmed.split("=", 1)
            k = k.strip()
            if k in updates:
                new_lines.append(f"{k}={updates[k]}\n")
                seen.add(k)
            else:
                new_lines.append(line)
        for k, v in updates.items():
            if k not in seen:
                new_lines.append(f"{k}={v}\n")
        with open(env_file, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
    except Exception as e:
        print(f"[ENV SYNC ERROR] {e}", flush=True)


def get_current_settings() -> dict:
    key_masked = ""
    if KEY:
        if len(KEY) > 12:
            key_masked = KEY[:7] + "..." + KEY[-4:]
        else:
            key_masked = "sk-***"
    u_key_masked = ""
    if UPSTREAM_KEY:
        if len(UPSTREAM_KEY) > 12:
            u_key_masked = UPSTREAM_KEY[:7] + "..." + UPSTREAM_KEY[-4:]
        else:
            u_key_masked = "sk-***"
    
    effective_opt_key = OPT_KEY or KEY
    opt_key_masked = ""
    if effective_opt_key:
        if len(effective_opt_key) > 12:
            opt_key_masked = effective_opt_key[:7] + "..." + effective_opt_key[-4:]
        else:
            opt_key_masked = "sk-***"

    return {
        "models": {
            "luna": MAP.get("luna", "gpt-6-luna"),
            "terra": MAP.get("terra", "gpt-6-sol"),
            "sol": MAP.get("sol", "gpt-6.1-sol"),
            "astra": MAP.get("astra", "gpt-6-astra"),
            "fallback": DEFAULT_MODEL,
        },
        "upstream": {
            "url": UPSTREAM_ORIGIN,
            "api_key_masked": u_key_masked,
        },
        "jev": {
            "url": JEV_URL,
            "model": JEV_MODEL,
            "api_key_masked": key_masked,
        },
        "routing_rules": {
            "rewrite_enabled": REWRITE,
            "planning_boost": PLANNING_BOOST,
            "auto_escalation": AUTO_ESCALATION,
            "escalation_cooldown": ERR_COOLDOWN,
            "planning_keywords": CURRENT_PLANNING_KEYWORDS,
            "architecture_keywords": CURRENT_ARCH_KEYWORDS,
        },
        "optimizer": {
            "enabled": OPT_ENABLED,
            "url": OPT_URL,
            "model": OPT_MODEL,
            "api_key_masked": opt_key_masked,
            "timeout_seconds": OPT_TIMEOUT,
            "temperature": OPT_TEMPERATURE,
            "max_tokens": OPT_MAX_TOKENS,
            "system_prompt": OPT_SYSTEM_PROMPT,
            "min_character_length": OPT_MIN_CHARS,
            "min_word_count": OPT_MIN_WORDS,
            "preserve_variables_strict": OPT_PRESERVE_VARS,
            "skip_short_confirmations": OPT_SKIP_SHORT,
            "autonomous_loop_markers": OPT_LOOP_MARKERS,
        },
    }


def fetch_upstream_models() -> dict:
    """Query CLIProxyAPI for available models list."""
    url = f"{UPSTREAM_ORIGIN}/v1/models"
    headers = {}
    if UPSTREAM_KEY:
        headers["Authorization"] = f"Bearer {UPSTREAM_KEY}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            raw = json.load(r)
        data = raw.get("data") or []
        models = []
        for it in data:
            if isinstance(it, dict) and it.get("id"):
                models.append({
                    "id": it["id"],
                    "owned_by": it.get("owned_by", "unknown"),
                    "created": it.get("created"),
                })
        models.sort(key=lambda x: x["id"].lower())
        return {"success": True, "total": len(models), "models": models}
    except Exception as e:
        return {"success": False, "error": str(e), "models": []}


def save_settings_data(data: dict) -> tuple[bool, str]:
    global MAP, RANK, DEFAULT_MODEL, UPSTREAM_ORIGIN, UPSTREAM_KEY, JEV_URL, JEV_MODEL, REWRITE
    global PLANNING_BOOST, AUTO_ESCALATION, ERR_COOLDOWN, PLANNING_RX, ARCH_RX
    global CURRENT_PLANNING_KEYWORDS, CURRENT_ARCH_KEYWORDS, KEY
    global OPT_ENABLED, OPT_URL, OPT_MODEL, OPT_KEY, OPT_TIMEOUT, OPT_TEMPERATURE, OPT_MAX_TOKENS
    global OPT_SYSTEM_PROMPT, OPT_MIN_CHARS, OPT_MIN_WORDS, OPT_PRESERVE_VARS, OPT_SKIP_SHORT, OPT_LOOP_MARKERS
    try:
        models = data.get("models") or {}
        if models.get("luna"):
            MAP["luna"] = models["luna"].strip()
        if models.get("terra"):
            MAP["terra"] = models["terra"].strip()
        if models.get("sol"):
            MAP["sol"] = models["sol"].strip()
        if models.get("astra"):
            MAP["astra"] = models["astra"].strip()
        if models.get("fallback"):
            DEFAULT_MODEL = models["fallback"].strip()

        RANK = {MAP["luna"]: 0, MAP["terra"]: 1, MAP["sol"]: 2, MAP["astra"]: 3}

        upstream = data.get("upstream") or {}
        if upstream.get("url"):
            UPSTREAM_ORIGIN = upstream["url"].strip().rstrip("/")
        new_u_key = upstream.get("api_key") or upstream.get("key") or ""
        if new_u_key and "..." not in new_u_key:
            UPSTREAM_KEY = new_u_key.strip()
            os.environ["JEV_UPSTREAM_KEY"] = UPSTREAM_KEY

        jev_cfg = data.get("jev") or {}
        if jev_cfg.get("url"):
            JEV_URL = jev_cfg["url"].strip()
        if jev_cfg.get("model"):
            JEV_MODEL = jev_cfg["model"].strip()
        new_key = jev_cfg.get("api_key") or ""
        if new_key and not new_key.startswith("sk-or-v1-...") and "..." not in new_key:
            KEY = new_key.strip()
            os.environ["OPENROUTER_API_KEY"] = KEY

        rules = data.get("routing_rules") or {}
        if "rewrite_enabled" in rules:
            REWRITE = bool(rules["rewrite_enabled"])
        if "planning_boost" in rules:
            PLANNING_BOOST = bool(rules["planning_boost"])
        if "auto_escalation" in rules:
            AUTO_ESCALATION = bool(rules["auto_escalation"])
        if "escalation_cooldown" in rules:
            try:
                ERR_COOLDOWN = float(rules["escalation_cooldown"])
            except Exception:
                pass

        if "planning_keywords" in rules:
            raw_p = rules["planning_keywords"]
            if isinstance(raw_p, str):
                raw_p = [x.strip() for x in raw_p.replace(",", "\n").split("\n")]
            CURRENT_PLANNING_KEYWORDS = [x for x in raw_p if x]
            PLANNING_RX = compile_keyword_rx(CURRENT_PLANNING_KEYWORDS)

        if "architecture_keywords" in rules:
            raw_a = rules["architecture_keywords"]
            if isinstance(raw_a, str):
                raw_a = [x.strip() for x in raw_a.replace(",", "\n").split("\n")]
            CURRENT_ARCH_KEYWORDS = [x for x in raw_a if x]
            ARCH_RX = compile_keyword_rx(CURRENT_ARCH_KEYWORDS)

        # Prompt Optimizer Settings
        opt_cfg = data.get("optimizer") or {}
        if "enabled" in opt_cfg:
            OPT_ENABLED = bool(opt_cfg["enabled"])
        if opt_cfg.get("url"):
            OPT_URL = opt_cfg["url"].strip()
        if opt_cfg.get("model"):
            OPT_MODEL = opt_cfg["model"].strip()
        new_opt_key = opt_cfg.get("api_key") or ""
        if new_opt_key and not new_opt_key.startswith("sk-or-v1-...") and "..." not in new_opt_key:
            OPT_KEY = new_opt_key.strip()
        if "timeout_seconds" in opt_cfg:
            try:
                OPT_TIMEOUT = float(opt_cfg["timeout_seconds"])
            except Exception:
                pass
        if "temperature" in opt_cfg:
            try:
                OPT_TEMPERATURE = float(opt_cfg["temperature"])
            except Exception:
                pass
        if "max_tokens" in opt_cfg:
            try:
                OPT_MAX_TOKENS = int(opt_cfg["max_tokens"])
            except Exception:
                pass
        if opt_cfg.get("system_prompt"):
            OPT_SYSTEM_PROMPT = opt_cfg["system_prompt"].strip()
        if "min_character_length" in opt_cfg:
            try:
                OPT_MIN_CHARS = int(opt_cfg["min_character_length"])
            except Exception:
                pass
        if "min_word_count" in opt_cfg:
            try:
                OPT_MIN_WORDS = int(opt_cfg["min_word_count"])
            except Exception:
                pass
        if "preserve_variables_strict" in opt_cfg:
            OPT_PRESERVE_VARS = bool(opt_cfg["preserve_variables_strict"])
        if "skip_short_confirmations" in opt_cfg:
            OPT_SKIP_SHORT = bool(opt_cfg["skip_short_confirmations"])
        if "autonomous_loop_markers" in opt_cfg:
            raw_m = opt_cfg["autonomous_loop_markers"]
            if isinstance(raw_m, str):
                raw_m = [x.strip() for x in raw_m.replace(",", "\n").split("\n")]
            OPT_LOOP_MARKERS = [x for x in raw_m if x]

        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(get_current_settings(), f, indent=2, ensure_ascii=False)

        sync_to_env()
        return True, "Cấu hình đã được lưu và áp dụng thành công!"
    except Exception as e:
        return False, str(e)


def init_settings() -> None:
    global MAP, RANK, DEFAULT_MODEL, UPSTREAM_ORIGIN, UPSTREAM_KEY, JEV_URL, JEV_MODEL, REWRITE
    global PLANNING_BOOST, AUTO_ESCALATION, ERR_COOLDOWN, PLANNING_RX, ARCH_RX
    global CURRENT_PLANNING_KEYWORDS, CURRENT_ARCH_KEYWORDS, KEY
    global OPT_ENABLED, OPT_URL, OPT_MODEL, OPT_KEY, OPT_TIMEOUT, OPT_TEMPERATURE, OPT_MAX_TOKENS
    global OPT_SYSTEM_PROMPT, OPT_MIN_CHARS, OPT_MIN_WORDS, OPT_PRESERVE_VARS, OPT_SKIP_SHORT, OPT_LOOP_MARKERS
    if os.path.isfile(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            models = data.get("models") or {}
            if models.get("luna"):
                MAP["luna"] = models["luna"]
            if models.get("terra"):
                MAP["terra"] = models["terra"]
            if models.get("sol"):
                MAP["sol"] = models["sol"]
            if models.get("astra"):
                MAP["astra"] = models["astra"]
            if models.get("fallback"):
                DEFAULT_MODEL = models["fallback"]
            RANK = {MAP["luna"]: 0, MAP["terra"]: 1, MAP["sol"]: 2, MAP["astra"]: 3}

            upstream = data.get("upstream") or {}
            if upstream.get("url"):
                UPSTREAM_ORIGIN = upstream["url"].rstrip("/")
            if upstream.get("key"):
                UPSTREAM_KEY = upstream["key"]
            elif upstream.get("api_key"):
                UPSTREAM_KEY = upstream["api_key"]

            jev_cfg = data.get("jev") or {}
            if jev_cfg.get("url"):
                JEV_URL = jev_cfg["url"]
            if jev_cfg.get("model"):
                JEV_MODEL = jev_cfg["model"]

            rules = data.get("routing_rules") or {}
            if "rewrite_enabled" in rules:
                REWRITE = bool(rules["rewrite_enabled"])
            if "planning_boost" in rules:
                PLANNING_BOOST = bool(rules["planning_boost"])
            if "auto_escalation" in rules:
                AUTO_ESCALATION = bool(rules["auto_escalation"])
            if "escalation_cooldown" in rules:
                ERR_COOLDOWN = float(rules["escalation_cooldown"])

            if rules.get("planning_keywords"):
                CURRENT_PLANNING_KEYWORDS = [k.strip() for k in rules["planning_keywords"] if k.strip()]
            if rules.get("architecture_keywords"):
                CURRENT_ARCH_KEYWORDS = [k.strip() for k in rules["architecture_keywords"] if k.strip()]

            opt_cfg = data.get("optimizer") or {}
            if "enabled" in opt_cfg:
                OPT_ENABLED = bool(opt_cfg["enabled"])
            if opt_cfg.get("url"):
                OPT_URL = opt_cfg["url"]
            if opt_cfg.get("model"):
                OPT_MODEL = opt_cfg["model"]
            if opt_cfg.get("api_key"):
                OPT_KEY = opt_cfg["api_key"]
            if "timeout_seconds" in opt_cfg:
                OPT_TIMEOUT = float(opt_cfg["timeout_seconds"])
            if "temperature" in opt_cfg:
                OPT_TEMPERATURE = float(opt_cfg["temperature"])
            if "max_tokens" in opt_cfg:
                OPT_MAX_TOKENS = int(opt_cfg["max_tokens"])
            if opt_cfg.get("system_prompt"):
                OPT_SYSTEM_PROMPT = opt_cfg["system_prompt"]
            if "min_character_length" in opt_cfg:
                OPT_MIN_CHARS = int(opt_cfg["min_character_length"])
            if "min_word_count" in opt_cfg:
                OPT_MIN_WORDS = int(opt_cfg["min_word_count"])
            if "preserve_variables_strict" in opt_cfg:
                OPT_PRESERVE_VARS = bool(opt_cfg["preserve_variables_strict"])
            if "skip_short_confirmations" in opt_cfg:
                OPT_SKIP_SHORT = bool(opt_cfg["skip_short_confirmations"])
            if opt_cfg.get("autonomous_loop_markers"):
                OPT_LOOP_MARKERS = [x.strip() for x in opt_cfg["autonomous_loop_markers"] if x.strip()]
        except Exception as e:
            print(f"[SETTINGS LOAD ERROR] {e}", flush=True)

    PLANNING_RX = compile_keyword_rx(CURRENT_PLANNING_KEYWORDS)
    ARCH_RX = compile_keyword_rx(CURRENT_ARCH_KEYWORDS)


init_settings()



def last_tool_error(body: dict) -> str | None:
    """If the last input item is a tool output that looks like a real failure, return its tail."""
    items = body.get("input")
    if not isinstance(items, list) or not items:
        return None
    last = items[-1]
    if not isinstance(last, dict) or last.get("type") not in ("function_call_output", "custom_tool_call_output"):
        return None
    out = last.get("output")
    if isinstance(out, str):
        t = out
    elif isinstance(out, list):
        t = " ".join(str(x.get("text") or "") if isinstance(x, dict) else str(x) for x in out)
    else:
        t = str(out or "")
    tail = t[-1500:]
    return tail if ERR_RX.search(tail) else ""


def current_task(body: dict) -> str:
    items = body.get("input")
    if not isinstance(items, list):
        return ""
    for it in reversed(items):
        if isinstance(it, dict) and it.get("role") == "user" and it.get("type", "message") == "message":
            t = _item_text(it).strip()
            if t and not t.startswith(SKIP_PREFIXES) and not t.startswith("<"):
                return t[:1500]
    return ""


def maybe_rewrite(raw: bytes, path: str, hdrs=None) -> tuple[bytes, dict]:
    meta = {
        "tier": None,
        "conf": None,
        "effort": None,
        "effort_conf": None,
        "in": None,
        "out": None,
        "new": False,
        "prompt": "",
        "sid": "default",
    }
    try:
        body = json.loads(raw or b"{}")
    except Exception:
        return raw, meta
    incoming = body.get("model")
    meta["in"] = incoming
    prompt, is_new = last_user_text(body)
    meta["new"] = is_new
    meta["prompt"] = (prompt or "")[:1000]

    _h = {k.lower(): v for k, v in (hdrs or {}).items()}
    sid = str(
        _h.get("session-id")
        or _h.get("x-session-id")
        or _h.get("thread-id")
        or _h.get("x-thread-id")
        or _h.get("x-codex-session-id")
        or _h.get("conversation-id")
        or _h.get("x-conversation-id")
        or body.get("conversation")
        or body.get("conversation_id")
        or body.get("session_id")
        or "default"
    )
    meta["sid"] = sid

    if not REWRITE or ("/responses" not in path.split("?", 1)[0] and "/chat/completions" not in path.split("?", 1)[0]):
        meta["out"] = incoming
        return raw, meta

    chosen = incoming
    effort = LEASE_EFFORT.get(sid, "medium")

    if is_new and prompt and incoming:
        BASE[sid] = incoming
    _p = prompt.strip().strip("。.!！~ ") if prompt else ""
    if is_new and prompt and _p.lower() in CONTINUE_WORDS and sid in LEASE:
        meta["tier"] = "reuse"
        chosen = LEASE[sid]
        effort = LEASE_EFFORT.get(sid, "medium")
        meta["effort"] = effort
    elif is_new and prompt:
        # Step A: Prompt Optimization (BEFORE sending to Jev)
        raw_prompt = prompt
        opt_text = raw_prompt
        opt_status = "BYPASS_DISABLED"
        opt_latency = 0.0
        diff_summary = ""
        orig_tok = max(1, int(len(raw_prompt) / 3.8))
        opt_tok = orig_tok

        if OPT_ENABLED:
            is_eligible, bypass_reason = check_prompt_heuristic(raw_prompt)
            if is_eligible:
                opt_result, success, status, latency_ms = run_prompt_optimizer(raw_prompt)
                opt_status = status
                opt_latency = latency_ms
                if success:
                    opt_text = opt_result
                    opt_tok = max(1, int(len(opt_text) / 3.8))
                    diff_data = generate_diff_summary(raw_prompt, opt_text)
                    diff_summary = diff_data.get("unified_diff", "")
                    update_last_user_prompt(body, opt_text)
                    prompt = opt_text  # CRITICAL: send optimized prompt to Planning detection & Jev!
            else:
                opt_status = f"BYPASS_{bypass_reason}"

        meta["opt_status"] = opt_status
        meta["raw_prompt"] = raw_prompt
        meta["optimized_prompt"] = opt_text
        meta["opt_latency_ms"] = opt_latency
        meta["diff_summary"] = diff_summary
        meta["original_tokens_est"] = orig_tok
        meta["optimized_tokens_est"] = opt_tok

        # Step B: Jev Classification with the (potentially optimized) prompt
        _hist = build_history(body, prompt)
        _ask = prompt
        if _hist:
            _ask = "【此前的对话（旧到新，可能是同一任务的多轮）】\n" + _hist + "\n\n【用户当前这句话，请评估整个任务当前需要的复杂度】\n" + prompt
        meta["hist"] = len(_hist)

        # Detect planning and architectural design intent
        is_planning = (PLANNING_BOOST and bool(PLANNING_RX.search(prompt))) if prompt else False
        is_heavy_arch = (PLANNING_BOOST and bool(ARCH_RX.search(prompt))) if is_planning else False

        extra = ""
        if is_planning:
            extra = " NOTE: The user is in PLANNING / DESIGN mode (e.g. Superpowers plan or implementation spec). Select sol or astra, with reasoning_effort high."

        try:
            tier, conf, effort, effort_conf = ask_jev(_ask, extra)
        except Exception as e:
            tier, conf, effort, effort_conf = "sol", 0.0, "high" if is_planning else "medium", 0.0
            meta["err"] = str(e)
            print(f"[JEV API ERROR] {e}", flush=True)

        # Enforce planning boost guarantee: planning tasks must NEVER be luna or terra
        if is_planning:
            meta["planning"] = True
            if tier in ("luna", "terra"):
                tier = "sol"
                conf = max(conf, 0.95)
            if is_heavy_arch and tier != "astra":
                tier = "astra"
                conf = max(conf, 0.95)
            effort = "high"
            effort_conf = max(effort_conf, 0.95)

        meta["tier"] = tier
        meta["conf"] = conf
        meta["effort"] = effort
        meta["effort_conf"] = effort_conf
        meta["probs"] = dict(LAST_PROBS.get("v") or {})
        meta["effort_probs"] = dict(LAST_PROBS.get("effort") or {})
        chosen = MAP.get(tier, incoming) or incoming or DEFAULT_MODEL
        LEASE[sid] = chosen
        LEASE_EFFORT[sid] = effort
    else:
        chosen = LEASE.get(sid, incoming)
        effort = LEASE_EFFORT.get(sid, "medium")
        meta["effort"] = effort
        if BASE.get(sid) != incoming:
            chosen = incoming
        elif sid in LEASE and AUTO_ESCALATION:
            _err = last_tool_error(body)
            if _err is None:
                pass
            elif _err == "":
                ERR_STREAK[sid] = 0
            else:
                ERR_STREAK[sid] = ERR_STREAK.get(sid, 0) + 1
                meta["err_streak"] = ERR_STREAK[sid]
                now = time.time()
                if now - ERR_LAST_ASK.get(sid, 0) >= ERR_COOLDOWN and RANK.get(chosen, 9) < 3:
                    ERR_LAST_ASK[sid] = now
                    _task = current_task(body)
                    _hist = build_history(body, _task)
                    _ask = ("【此前的对话】\n" + _hist + "\n\n" if _hist else "") + "【用户的任务】\n" + _task + "\n\n【刚刚一步工具执行失败，输出末尾】\n" + _err + "\n\n【请评估：为了解决这个失败并完成任务，接下来需要哪一档模型】"
                    try:
                        _t, _c, _e, _ec = ask_jev(_ask, " This is a mid-task step: a tool call just failed. Grade the difficulty of fixing it and finishing the task, from the failure output and the task.")
                        _new = MAP.get(_t)
                        meta["esc_tier"] = _t
                        meta["esc_effort"] = _e
                        meta["esc_probs"] = dict(LAST_PROBS.get("v") or {})
                        if _new and RANK.get(_new, -1) > RANK.get(chosen, -1):
                            meta["esc_from"] = chosen
                            meta["tier"] = _t
                            meta["conf"] = _c
                            meta["effort"] = _e
                            meta["effort_conf"] = _ec
                            meta["prompt"] = f"[Leo thang lỗi] {_task[:300]}"
                            chosen = _new
                            effort = _e
                            LEASE[sid] = chosen
                            LEASE_EFFORT[sid] = effort
                    except Exception as e:
                        meta["esc_err"] = str(e)
                        print(f"[JEV ESCALATE ERROR] {e}", flush=True)

    # Rewrite model
    body["model"] = chosen
    meta["out"] = chosen

    # Rewrite reasoning effort for Codex / OpenAI Responses API
    if effort:
        if isinstance(body.get("reasoning"), dict):
            body["reasoning"]["effort"] = effort
        else:
            body["reasoning"] = {"effort": effort}
        if "reasoning_effort" in body:
            del body["reasoning_effort"]

    return json.dumps(body, ensure_ascii=False).encode("utf-8"), meta


def copy_req_headers(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in handler.headers.items():
        if k.lower() in HOP_BY_HOP:
            continue
        out[k] = v
    return out


RETRY_CODES = {400, 502, 503, 504}
RETRY_MAX = int(_env("JEV_RETRY_MAX", "2"))
RETRY_DELAY = float(_env("JEV_RETRY_DELAY", "1.0"))


def open_with_retry(req, method: str, path: str):
    """Retry flaky upstream 400/502/503/504 on POST /responses; re-raise the last failure."""
    retry_ok = method == "POST" and "/responses" in path.split("?", 1)[0]
    attempt = 0
    while True:
        try:
            up = urllib.request.urlopen(req, timeout=600)
            if attempt:
                print(f"RETRY OK after {attempt} retry {method} {path}", flush=True)
            return up
        except urllib.error.HTTPError as e:
            if retry_ok and e.code in RETRY_CODES and attempt < RETRY_MAX:
                try:
                    body = e.read()[:200]
                except Exception:
                    body = b""
                attempt += 1
                print(f"RETRY {attempt}/{RETRY_MAX} upstream HTTP {e.code} {path} {body}", flush=True)
                time.sleep(RETRY_DELAY)
                continue
            raise
        except urllib.error.URLError as e:
            if retry_ok and attempt < RETRY_MAX:
                attempt += 1
                print(f"RETRY {attempt}/{RETRY_MAX} forward error {e} {path}", flush=True)
                time.sleep(RETRY_DELAY)
                continue
            raise


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Jev Codex Router - Dashboard & Settings</title>
    <style>
        :root {
            --bg-primary: #0b0f19;
            --bg-secondary: #111827;
            --bg-card: #1f2937;
            --border: #374151;
            --text-main: #f9fafb;
            --text-muted: #9ca3af;
            --accent: #38bdf8;
            --luna: #10b981;
            --terra: #0284c7;
            --sol: #8b5cf6;
            --astra: #f43f5e;
            --reuse: #6b7280;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
        body { background: var(--bg-primary); color: var(--text-main); min-height: 100vh; padding: 24px 16px; }
        .container { max-width: 1200px; margin: 0 auto; }
        
        .header { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 16px; padding-bottom: 20px; border-bottom: 1px solid var(--border); margin-bottom: 16px; }
        .header-left h1 { font-size: 24px; font-weight: 800; display: flex; align-items: center; gap: 10px; color: var(--text-main); }
        .header-left h1 span.icon { color: #f59e0b; }
        .header-left p { color: var(--text-muted); font-size: 13px; margin-top: 4px; }
        .header-right { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; }
        
        .badge-status { background: rgba(16, 185, 129, 0.15); border: 1px solid rgba(16, 185, 129, 0.4); color: #34d399; padding: 6px 14px; border-radius: 9999px; font-size: 13px; font-weight: 600; display: inline-flex; align-items: center; gap: 8px; }
        .pulse-dot { width: 8px; height: 8px; border-radius: 50%; background: #10b981; box-shadow: 0 0 10px #10b981; animation: pulse 1.8s infinite; }
        @keyframes pulse { 0% { opacity: 1; transform: scale(1); } 50% { opacity: 0.4; transform: scale(1.2); } 100% { opacity: 1; transform: scale(1); } }
        
        .btn { background: var(--bg-card); border: 1px solid var(--border); color: var(--text-main); padding: 8px 16px; border-radius: 8px; font-size: 13px; font-weight: 600; cursor: pointer; transition: all 0.2s; display: inline-flex; align-items: center; gap: 6px; }
        .btn:hover { background: #374151; border-color: #4b5563; }
        .btn-primary { background: #0284c7; border-color: #38bdf8; }
        .btn-primary:hover { background: #0369a1; }
        
        .main-nav { display: flex; gap: 10px; margin-bottom: 24px; border-bottom: 1px solid var(--border); padding-bottom: 12px; }
        .nav-tab { background: transparent; border: 1px solid transparent; color: var(--text-muted); padding: 9px 20px; border-radius: 8px; font-size: 14px; font-weight: 700; cursor: pointer; transition: all 0.2s; display: inline-flex; align-items: center; gap: 8px; }
        .nav-tab:hover { color: var(--text-main); background: rgba(255, 255, 255, 0.04); }
        .nav-tab.active { color: #38bdf8; background: rgba(56, 189, 248, 0.12); border-color: rgba(56, 189, 248, 0.35); }

        .grid-stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 16px; margin-bottom: 24px; }
        .stat-card { background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 12px; padding: 18px; position: relative; overflow: hidden; }
        .stat-card::before { content: ""; position: absolute; top: 0; left: 0; right: 0; height: 3px; background: var(--card-color, var(--accent)); }
        .stat-title { font-size: 12px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--text-muted); font-weight: 700; display: flex; justify-content: space-between; }
        .stat-value { font-size: 28px; font-weight: 800; margin-top: 8px; color: var(--card-color, var(--text-main)); }
        .stat-sub { font-size: 12px; color: var(--text-muted); margin-top: 4px; font-family: monospace; }
        
        .info-panel { background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 12px; padding: 18px; margin-bottom: 24px; }
        .info-panel-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
        .info-panel h3 { font-size: 14px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--text-muted); font-weight: 700; }
        .mapping-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; }
        .mapping-item { background: var(--bg-primary); border: 1px solid var(--border); border-radius: 8px; padding: 10px 14px; display: flex; justify-content: space-between; align-items: center; }
        .mapping-tier { font-weight: 700; font-size: 13px; text-transform: uppercase; }
        .mapping-model { font-family: monospace; font-size: 13px; color: #38bdf8; }
        
        .table-section { background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 12px; overflow: hidden; }
        .table-header { padding: 16px 20px; display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 12px; border-bottom: 1px solid var(--border); }
        .table-header h2 { font-size: 16px; font-weight: 700; }
        .search-box { background: var(--bg-primary); border: 1px solid var(--border); color: var(--text-main); padding: 8px 14px; border-radius: 8px; font-size: 13px; width: 280px; outline: none; }
        .search-box:focus { border-color: var(--accent); }
        
        .table-responsive { width: 100%; overflow-x: auto; }
        table { width: 100%; border-collapse: collapse; text-align: left; }
        th { padding: 12px 16px; font-size: 12px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--text-muted); border-bottom: 1px solid var(--border); background: rgba(0,0,0,0.2); }
        td { padding: 12px 16px; font-size: 13px; border-bottom: 1px solid var(--border); }
        tr:hover td { background: rgba(255, 255, 255, 0.02); }
        
        .tier-badge { padding: 3px 10px; border-radius: 9999px; font-size: 11px; font-weight: 700; text-transform: uppercase; display: inline-block; }
        .tier-luna { background: rgba(16, 185, 129, 0.15); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.4); }
        .tier-terra { background: rgba(2, 132, 199, 0.15); color: #38bdf8; border: 1px solid rgba(2, 132, 199, 0.4); }
        .tier-sol { background: rgba(139, 92, 246, 0.15); color: #c084fc; border: 1px solid rgba(139, 92, 246, 0.4); }
        .tier-astra { background: rgba(244, 63, 94, 0.15); color: #fb7185; border: 1px solid rgba(244, 63, 94, 0.4); }
        .tier-reuse { background: rgba(107, 114, 128, 0.15); color: #9ca3af; border: 1px solid rgba(107, 114, 128, 0.4); }

        .effort-badge { padding: 3px 10px; border-radius: 9999px; font-size: 11px; font-weight: 700; text-transform: uppercase; display: inline-block; font-family: monospace; }
        .effort-low { background: rgba(20, 184, 166, 0.15); color: #2dd4bf; border: 1px solid rgba(20, 184, 166, 0.4); }
        .effort-medium { background: rgba(245, 158, 11, 0.15); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.4); }
        .effort-high { background: rgba(217, 70, 239, 0.15); color: #e879f9; border: 1px solid rgba(217, 70, 239, 0.4); }
        
        .prompt-cell { max-width: 380px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: monospace; color: #e2e8f0; }
        .model-cell { font-family: monospace; font-weight: 600; color: #38bdf8; }
        .orig-cell { font-family: monospace; color: #6b7280; font-size: 12px; }

        .badge-planning { background: rgba(245, 158, 11, 0.15); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.4); padding: 2px 8px; border-radius: 6px; font-weight: 700; font-size: 11px; display: inline-flex; align-items: center; gap: 4px; }
        .badge-escalate { background: rgba(239, 68, 68, 0.15); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.4); padding: 2px 8px; border-radius: 6px; font-weight: 700; font-size: 11px; display: inline-flex; align-items: center; gap: 4px; }
        
        #test-alert, #settings-alert { display: none; margin-bottom: 20px; padding: 14px 18px; border-radius: 8px; font-size: 13px; font-weight: 600; line-height: 1.5; }
        .alert-success { background: rgba(16, 185, 129, 0.2); border: 1px solid #10b981; color: #34d399; }
        .alert-error { background: rgba(244, 63, 94, 0.2); border: 1px solid #f43f5e; color: #fb7185; }

        /* Settings View Styles */
        .settings-section { background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 12px; padding: 22px; margin-bottom: 24px; }
        .settings-section h2 { font-size: 16px; font-weight: 700; margin-bottom: 6px; display: flex; align-items: center; gap: 8px; }
        .settings-section p.sec-desc { font-size: 13px; color: var(--text-muted); margin-bottom: 20px; }
        
        .form-grid-4 { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 16px; margin-bottom: 20px; }
        .form-grid-2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 16px; margin-bottom: 16px; }
        
        .model-box { background: var(--bg-primary); border: 1px solid var(--border); border-radius: 10px; padding: 14px; position: relative; }
        .model-box.luna { border-top: 3px solid var(--luna); }
        .model-box.terra { border-top: 3px solid var(--terra); }
        .model-box.sol { border-top: 3px solid var(--sol); }
        .model-box.astra { border-top: 3px solid var(--astra); }
        .model-box label { font-size: 12px; font-weight: 700; text-transform: uppercase; display: block; margin-bottom: 8px; }
        .model-box .desc { font-size: 11px; color: var(--text-muted); margin-top: 6px; line-height: 1.4; }
        
        .form-label { display: block; font-size: 13px; font-weight: 600; margin-bottom: 6px; color: var(--text-main); }
        .form-desc { font-size: 11px; color: var(--text-muted); margin-top: 4px; }
        .form-input { width: 100%; background: var(--bg-primary); border: 1px solid var(--border); color: var(--text-main); padding: 9px 12px; border-radius: 8px; font-size: 13px; font-family: monospace; outline: none; transition: border-color 0.2s; }
        .form-input:focus { border-color: var(--accent); }
        .form-textarea { width: 100%; background: var(--bg-primary); border: 1px solid var(--border); color: var(--text-main); padding: 10px 12px; border-radius: 8px; font-size: 12px; font-family: monospace; outline: none; min-height: 90px; resize: vertical; line-height: 1.5; transition: border-color 0.2s; }
        .form-textarea:focus { border-color: var(--accent); }

        .rule-card { background: var(--bg-primary); border: 1px solid var(--border); border-radius: 10px; padding: 14px 18px; display: flex; justify-content: space-between; align-items: center; gap: 16px; margin-bottom: 12px; }
        .rule-card .rule-title { font-size: 13px; font-weight: 700; color: var(--text-main); display: flex; align-items: center; gap: 8px; }
        .rule-card .rule-desc { font-size: 12px; color: var(--text-muted); margin-top: 4px; }

        .switch { position: relative; display: inline-flex; align-items: center; cursor: pointer; flex-shrink: 0; }
        .switch input { opacity: 0; width: 0; height: 0; position: absolute; }
        .switch-slider { width: 44px; height: 24px; background-color: #334155; transition: .25s; border-radius: 24px; position: relative; display: inline-block; }
        .switch-slider:before { position: absolute; content: ""; height: 18px; width: 18px; left: 3px; bottom: 3px; background-color: #f8fafc; transition: .25s; border-radius: 50%; }
        input:checked + .switch-slider { background-color: #0284c7; }
        input:checked + .switch-slider:before { transform: translateX(20px); }

        .actions-bar { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; padding-top: 8px; }
        .sec-header { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; margin-bottom: 8px; }
        .model-quick-select { width: 100%; margin-top: 8px; background: var(--bg-card); border: 1px solid var(--border); color: var(--text-main); padding: 7px 10px; border-radius: 6px; font-size: 12px; font-family: monospace; outline: none; cursor: pointer; }
        .model-quick-select:focus { border-color: var(--accent); }
        .model-quick-select optgroup { font-weight: 700; color: var(--accent); background: var(--bg-primary); }
        .model-quick-select option { color: var(--text-main); background: var(--bg-card); }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="header-left">
                <h1><span class="icon">⚡</span> Jev Codex Router</h1>
                <p>Hệ thống giám sát định tuyến mô hình & cấu hình quy tắc phân loại thông minh</p>
            </div>
            <div class="header-right">
                <span class="badge-status"><span class="pulse-dot"></span> ROUTER ĐANG CHẠY</span>
                <button class="btn btn-primary" onclick="testJev(false)" id="btn-test">🧪 Test Đơn giản</button>
                <button class="btn" style="background:#7c3aed; border-color:#a78bfa;" onclick="testJev(true)" id="btn-test-plan">🎯 Test Planning</button>
                <button class="btn" onclick="refreshCurrentTab()">🔄 Làm mới</button>
            </div>
        </div>

        <div class="main-nav">
            <button class="nav-tab active" id="tab-btn-dashboard" onclick="switchTab('dashboard')">
                <span>📊</span> Tổng quan & Giám sát (Dashboard)
            </button>
            <button class="nav-tab" id="tab-btn-settings" onclick="switchTab('settings')">
                <span>⚙️</span> Cài đặt Models & Routing Rules
            </button>
        </div>

        <!-- ================= VIEW 1: DASHBOARD ================= -->
        <div id="view-dashboard">
            <div id="test-alert"></div>

            <div class="grid-stats">
                <div class="stat-card" style="--card-color: #38bdf8;">
                    <div class="stat-title"><span>Tổng lượt phân loại</span> <span>🔄</span></div>
                    <div class="stat-value" id="val-total">0</div>
                    <div class="stat-sub" id="val-uptime">Uptime: 0s</div>
                </div>
                <div class="stat-card" style="--card-color: var(--luna);">
                    <div class="stat-title"><span>Luna Tier</span> <span id="pct-luna">0%</span></div>
                    <div class="stat-value" id="val-luna">0</div>
                    <div class="stat-sub" id="sub-luna">gpt-6-luna</div>
                </div>
                <div class="stat-card" style="--card-color: var(--terra);">
                    <div class="stat-title"><span>Terra Tier</span> <span id="pct-terra">0%</span></div>
                    <div class="stat-value" id="val-terra">0</div>
                    <div class="stat-sub" id="sub-terra">gpt-6-sol</div>
                </div>
                <div class="stat-card" style="--card-color: var(--sol);">
                    <div class="stat-title"><span>Sol Tier</span> <span id="pct-sol">0%</span></div>
                    <div class="stat-value" id="val-sol">0</div>
                    <div class="stat-sub" id="sub-sol">gpt-6.1-sol</div>
                </div>
                <div class="stat-card" style="--card-color: var(--astra);">
                    <div class="stat-title"><span>Astra Tier</span> <span id="pct-astra">0%</span></div>
                    <div class="stat-value" id="val-astra">0</div>
                    <div class="stat-sub" id="sub-astra">gpt-6-astra</div>
                </div>
            </div>

            <div class="info-panel">
                <div class="info-panel-header">
                    <h3>⚙️ Ánh xạ mô hình hiện tại (Model Mapping)</h3>
                    <button class="btn" style="padding:4px 10px; font-size:12px;" onclick="switchTab('settings')">Chỉnh sửa trong Cài đặt ⚙️</button>
                </div>
                <div class="mapping-grid">
                    <div class="mapping-item">
                        <span class="mapping-tier" style="color:var(--luna);">🟢 LUNA</span>
                        <span class="mapping-model" id="map-luna">-</span>
                    </div>
                    <div class="mapping-item">
                        <span class="mapping-tier" style="color:var(--terra);">🔵 TERRA</span>
                        <span class="mapping-model" id="map-terra">-</span>
                    </div>
                    <div class="mapping-item">
                        <span class="mapping-tier" style="color:var(--sol);">🟣 SOL</span>
                        <span class="mapping-model" id="map-sol">-</span>
                    </div>
                    <div class="mapping-item">
                        <span class="mapping-tier" style="color:var(--astra);">🔴 ASTRA</span>
                        <span class="mapping-model" id="map-astra">-</span>
                    </div>
                </div>
                <div style="margin-top:14px; font-size:12px; color:var(--text-muted); display:flex; flex-wrap:wrap; gap:16px;">
                    <div><strong>Upstream Gateway:</strong> <span id="info-upstream" style="color:#38bdf8;">-</span></div>
                    <div><strong>Jev Backend:</strong> <span id="info-jev" style="color:#38bdf8;">-</span></div>
                    <div><strong>Phân loại kép:</strong> <span style="color:#34d399;">Model Tier + Reasoning Effort</span></div>
                    <div><strong>Database:</strong> <span style="color:#38bdf8;">SQLite (decisions.db)</span></div>
                </div>
            </div>

            <div class="table-section">
                <div class="table-header">
                    <h2>📋 Lịch sử định tuyến (Routing Decisions Log)</h2>
                    <input type="text" class="search-box" id="search-input" placeholder="Tìm theo prompt, tier, reasoning, model..." oninput="renderTable()">
                </div>
                <div class="table-responsive">
                    <table>
                        <thead>
                            <tr>
                                <th>Thời gian</th>
                                <th>Prompt (Người dùng)</th>
                                <th>Tier</th>
                                <th>Mức suy luận (Reasoning)</th>
                                <th>Độ tin cậy</th>
                                <th>Model được chọn</th>
                                <th>Model gốc</th>
                                <th>Phân loại</th>
                            </tr>
                        </thead>
                        <tbody id="decisions-body">
                            <tr><td colspan="8" style="text-align:center; padding:30px; color:var(--text-muted);">Đang tải dữ liệu...</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- ================= VIEW 2: SETTINGS ================= -->
        <div id="view-settings" style="display:none;">
            <div id="settings-alert"></div>

            <!-- SECTION 1: MODELS SETTINGS -->
            <div class="settings-section">
                <div class="sec-header">
                    <div>
                        <h2>🤖 Cấu hình Model Mappings (Models Settings)</h2>
                        <p class="sec-desc" style="margin-bottom:0;">Tùy biến tên mô hình được ánh xạ cho 4 phân cấp (Tiers) khi Codex gửi request qua Jev Router.</p>
                    </div>
                    <div style="display:flex; align-items:center; gap:10px;">
                        <span id="models-count-badge" style="display:none; background:rgba(56,189,248,0.15); border:1px solid rgba(56,189,248,0.35); color:#38bdf8; font-weight:700; font-size:12px; padding:6px 12px; border-radius:9999px;"></span>
                        <button type="button" class="btn" onclick="fetchUpstreamModels(true)" id="btn-fetch-models">🔄 Lấy danh sách từ CLIProxyAPI</button>
                    </div>
                </div>

                <datalist id="upstream-models-list"></datalist>

                <div class="form-grid-4" style="margin-top:16px;">
                    <div class="model-box luna">
                        <label style="color:var(--luna);">🟢 LUNA Model (Nhẹ / Nhanh)</label>
                        <input id="set-model-luna" type="text" class="form-input" list="upstream-models-list" placeholder="gpt-6-luna">
                        <select class="model-quick-select" id="quick-select-luna" onchange="applyModelChoice('luna', this.value)">
                            <option value="">-- Chọn nhanh model từ danh sách --</option>
                        </select>
                        <div class="desc">Dành cho git status, format, sửa typo, tác vụ đơn giản</div>
                    </div>
                    <div class="model-box terra">
                        <label style="color:var(--terra);">🔵 TERRA Model (Tiêu chuẩn)</label>
                        <input id="set-model-terra" type="text" class="form-input" list="upstream-models-list" placeholder="gpt-6-sol">
                        <select class="model-quick-select" id="quick-select-terra" onchange="applyModelChoice('terra', this.value)">
                            <option value="">-- Chọn nhanh model từ danh sách --</option>
                        </select>
                        <div class="desc">Dành cho 1-2 file, code theo plan có sẵn, unit test</div>
                    </div>
                    <div class="model-box sol">
                        <label style="color:var(--sol);">🟣 SOL Model (Mạnh mẽ)</label>
                        <input id="set-model-sol" type="text" class="form-input" list="upstream-models-list" placeholder="gpt-6.1-sol">
                        <select class="model-quick-select" id="quick-select-sol" onchange="applyModelChoice('sol', this.value)">
                            <option value="">-- Chọn nhanh model từ danh sách --</option>
                        </select>
                        <div class="desc">Dành cho đa module, bug phức tạp, feature planning</div>
                    </div>
                    <div class="model-box astra">
                        <label style="color:var(--astra);">🔴 ASTRA Model (Frontier)</label>
                        <input id="set-model-astra" type="text" class="form-input" list="upstream-models-list" placeholder="gpt-6-astra">
                        <select class="model-quick-select" id="quick-select-astra" onchange="applyModelChoice('astra', this.value)">
                            <option value="">-- Chọn nhanh model từ danh sách --</option>
                        </select>
                        <div class="desc">Dành cho kiến trúc hệ thống, data migration, security</div>
                    </div>
                </div>

                <div class="form-grid-2">
                    <div>
                        <label class="form-label">Model dự phòng mặc định (Fallback Model)</label>
                        <input id="set-model-fallback" type="text" class="form-input" list="upstream-models-list" placeholder="gpt-6.1-sol">
                        <select class="model-quick-select" id="quick-select-fallback" onchange="applyModelChoice('fallback', this.value)">
                            <option value="">-- Chọn nhanh model từ danh sách --</option>
                        </select>
                        <div class="form-desc">Sử dụng khi Jev API không phản hồi hoặc không nhận diện được</div>
                    </div>
                    <div>
                        <label class="form-label">Upstream Gateway URL (CLIProxyAPI)</label>
                        <input id="set-upstream-url" type="text" class="form-input" placeholder="http://127.0.0.1:8317">
                        <div class="form-desc">Địa chỉ của proxy gateway phía sau (cli-proxy-api)</div>
                    </div>
                </div>

                <div class="form-grid-2">
                    <div>
                        <label class="form-label">CLIProxyAPI Bearer Token (nếu có)</label>
                        <div style="display:flex; gap:8px;">
                            <input id="set-upstream-key" type="password" class="form-input" placeholder="Để trống nếu không đổi">
                            <button type="button" class="btn" onclick="toggleUpstreamKeyVisibility()" id="btn-toggle-upstream-key">👁️</button>
                        </div>
                        <div class="form-desc">Khóa Bearer Token xác thực kết nối CLIProxyAPI (đồng bộ /v1/models)</div>
                    </div>
                    <div>
                        <label class="form-label">Jev Backend Model (OpenRouter)</label>
                        <input id="set-jev-model" type="text" class="form-input" placeholder="typesafe/jev-1.13">
                        <div class="form-desc">Model chịu trách nhiệm phân loại tier & reasoning effort</div>
                    </div>
                </div>

                <div class="form-grid-2">
                    <div>
                        <label class="form-label">OpenRouter API Key</label>
                        <div style="display:flex; gap:8px;">
                            <input id="set-jev-key" type="password" class="form-input" placeholder="Để trống nếu không đổi">
                            <button type="button" class="btn" onclick="toggleKeyVisibility()" id="btn-toggle-key">👁️</button>
                        </div>
                        <div class="form-desc">Khóa API của OpenRouter để gọi Jev Model. Để trống để giữ nguyên.</div>
                    </div>
                    <div></div>
                </div>
            </div>

            <!-- SECTION 2: ROUTING RULES -->
            <div class="settings-section">
                <h2>🔀 Cấu hình Quy tắc định tuyến (Routing Rules)</h2>
                <p class="sec-desc">Bật/tắt các hành vi tự động và quản lý bộ từ khóa nhận diện lập kế hoạch (Planning) & kiến trúc (Architecture).</p>

                <div class="rule-card">
                    <div class="rule-text">
                        <div class="rule-title">⚡ Bật Model Rewriting (Định tuyến thông minh)</div>
                        <div class="rule-desc">Tự động viết lại trường <code>model</code> và <code>reasoning.effort</code> trong request gửi tới Codex gateway. Tắt tính năng này để chuyển tiếp nguyên bản (Bypass).</div>
                    </div>
                    <label class="switch">
                        <input type="checkbox" id="set-rule-rewrite">
                        <span class="switch-slider"></span>
                    </label>
                </div>

                <div class="rule-card">
                    <div class="rule-text">
                        <div class="rule-title">🎯 Bật Planning Priority Boost (Ưu tiên Lập kế hoạch)</div>
                        <div class="rule-desc">Tự động ưu tiên chọn tối thiểu <strong>SOL</strong> (hoặc <strong>ASTRA</strong> nếu là kiến trúc) kèm mức suy luận <strong>HIGH</strong> mỗi khi phát hiện task Planning / Superpowers.</div>
                    </div>
                    <label class="switch">
                        <input type="checkbox" id="set-rule-planning">
                        <span class="switch-slider"></span>
                    </label>
                </div>

                <div class="rule-card">
                    <div class="rule-text">
                        <div class="rule-title">🚀 Bật Auto-Escalation khi Tool lỗi</div>
                        <div class="rule-desc">Tự động nâng tier model cao hơn khi các lệnh hoặc tool call gặp lỗi liên tiếp (error streak).</div>
                    </div>
                    <label class="switch">
                        <input type="checkbox" id="set-rule-escalate">
                        <span class="switch-slider"></span>
                    </label>
                </div>

                <div style="margin: 16px 0 20px 0; max-width: 360px;">
                    <label class="form-label">Thời gian Cooldown Escalation (giây)</label>
                    <input id="set-rule-cooldown" type="number" min="5" max="300" step="1" class="form-input" placeholder="20">
                    <div class="form-desc">Khoảng cách thời gian tối thiểu giữa 2 lần kích hoạt leo thang do lỗi tool.</div>
                </div>

                <div style="margin-bottom: 20px;">
                    <label class="form-label">📋 Danh sách từ khóa nhận diện Planning (Lập kế hoạch, Spec, Superpowers)</label>
                    <textarea id="set-keywords-planning" class="form-textarea" rows="4" placeholder="Mỗi từ khóa 1 dòng hoặc cách nhau dấu phẩy..."></textarea>
                    <div class="form-desc">Khi prompt chứa bất kỳ từ khóa nào trong danh sách này, router sẽ tự động ưu tiên gán Sol/Astra và Reasoning High.</div>
                </div>

                <div style="margin-bottom: 24px;">
                    <label class="form-label">🏛️ Danh sách từ khóa nhận diện Heavy Architecture (Kiến trúc lớn / An ninh)</label>
                    <textarea id="set-keywords-arch" class="form-textarea" rows="3" placeholder="Mỗi từ khóa 1 dòng hoặc cách nhau dấu phẩy..."></textarea>
                    <div class="form-desc">Các từ khóa kiến trúc đặc thù (microservices, concurrency, migration...) sẽ kích hoạt nâng thẳng lên <strong>ASTRA</strong>.</div>
                </div>

                <div class="actions-bar">
                    <button class="btn btn-primary" onclick="saveSettings()" id="btn-save-settings">💾 Lưu Cấu hình & Áp dụng ngay</button>
                    <button class="btn" onclick="loadSettings()">🔄 Đọc lại</button>
                    <button class="btn" onclick="resetDefaultSettings()" style="color:#f87171; border-color:rgba(239,68,68,0.4);">⚠️ Khôi phục mặc định</button>
                </div>
            </div>
        </div>
    </div>

    <script>
        let currentTab = 'dashboard';
        let allDecisions = [];
        let defaultSettingsRef = null;

        function getApiUrl(endpoint) {
            let base = window.location.pathname;
            if (base.endsWith('/')) base = base.slice(0, -1);
            let prefix = base.includes('/jev') ? '/jev' : '';
            return prefix + '/api/' + endpoint;
        }

        function switchTab(tab) {
            currentTab = tab;
            window.location.hash = tab;
            
            document.getElementById('tab-btn-dashboard').className = 'nav-tab' + (tab === 'dashboard' ? ' active' : '');
            document.getElementById('tab-btn-settings').className = 'nav-tab' + (tab === 'settings' ? ' active' : '');

            document.getElementById('view-dashboard').style.display = (tab === 'dashboard' ? 'block' : 'none');
            document.getElementById('view-settings').style.display = (tab === 'settings' ? 'block' : 'none');

            if (tab === 'settings') {
                loadSettings();
            } else {
                fetchData();
            }
        }

        function refreshCurrentTab() {
            if (currentTab === 'settings') {
                loadSettings();
            } else {
                fetchData();
            }
        }

        async function fetchData() {
            if (currentTab !== 'dashboard') return;
            try {
                const [statsRes, decRes] = await Promise.all([
                    fetch(getApiUrl('stats')),
                    fetch(getApiUrl('decisions?limit=50'))
                ]);
                const stats = await statsRes.json();
                const decisions = await decRes.json();

                updateStats(stats);
                allDecisions = decisions;
                renderTable();
            } catch (err) {
                console.error("Fetch data error:", err);
            }
        }

        function updateStats(stats) {
            if (!stats) return;
            document.getElementById('val-total').textContent = stats.total_turns || 0;
            
            const upSec = stats.uptime || 0;
            const upH = Math.floor(upSec / 3600);
            const upM = Math.floor((upSec % 3600) / 60);
            document.getElementById('val-uptime').textContent = `Uptime: ${upH}h ${upM}m ${upSec % 60}s`;

            const tc = stats.tier_counts || {};
            const total = stats.total_turns || 1;

            const luna = tc['luna'] || 0;
            const terra = tc['terra'] || 0;
            const sol = tc['sol'] || 0;
            const astra = tc['astra'] || 0;

            document.getElementById('val-luna').textContent = luna;
            document.getElementById('val-terra').textContent = terra;
            document.getElementById('val-sol').textContent = sol;
            document.getElementById('val-astra').textContent = astra;

            document.getElementById('pct-luna').textContent = Math.round((luna / total) * 100) + '%';
            document.getElementById('pct-terra').textContent = Math.round((terra / total) * 100) + '%';
            document.getElementById('pct-sol').textContent = Math.round((sol / total) * 100) + '%';
            document.getElementById('pct-astra').textContent = Math.round((astra / total) * 100) + '%';

            if (stats.mapping) {
                document.getElementById('map-luna').textContent = stats.mapping.luna || '-';
                document.getElementById('map-terra').textContent = stats.mapping.terra || '-';
                document.getElementById('map-sol').textContent = stats.mapping.sol || '-';
                document.getElementById('map-astra').textContent = stats.mapping.astra || '-';
                document.getElementById('sub-luna').textContent = stats.mapping.luna || '-';
                document.getElementById('sub-terra').textContent = stats.mapping.terra || '-';
                document.getElementById('sub-sol').textContent = stats.mapping.sol || '-';
                document.getElementById('sub-astra').textContent = stats.mapping.astra || '-';
            }

            document.getElementById('info-upstream').textContent = stats.upstream || '-';
            document.getElementById('info-jev').textContent = stats.jev_model || '-';
        }

        function renderTable() {
            const tbody = document.getElementById('decisions-body');
            const query = (document.getElementById('search-input').value || '').toLowerCase().trim();

            const filtered = allDecisions.filter(d => {
                if (!query) return true;
                const p = (d.prompt || '').toLowerCase();
                const t = (d.tier || '').toLowerCase();
                const e = (d.reasoning_effort || '').toLowerCase();
                const m = (d.selected_model || '').toLowerCase();
                const esc = (d.escalation || '').toLowerCase();
                return p.includes(query) || t.includes(query) || e.includes(query) || m.includes(query) || esc.includes(query);
            });

            if (!filtered || filtered.length === 0) {
                tbody.innerHTML = '<tr><td colspan="8" style="text-align:center; padding:30px; color:var(--text-muted);">' +
                    (allDecisions.length === 0 ? 'Chưa có quyết định nào trong DB. Hãy gửi câu hỏi qua Codex để bắt đầu!' : 'Không tìm thấy kết quả phù hợp.') +
                    '</td></tr>';
                return;
            }

            tbody.innerHTML = filtered.map(d => {
                const tier = (d.tier || 'unknown').toLowerCase();
                let tierClass = 'tier-reuse';
                if (tier === 'luna') tierClass = 'tier-luna';
                else if (tier === 'terra') tierClass = 'tier-terra';
                else if (tier === 'sol') tierClass = 'tier-sol';
                else if (tier === 'astra') tierClass = 'tier-astra';

                const effort = (d.reasoning_effort || 'medium').toLowerCase();
                let effortClass = 'effort-medium';
                if (effort === 'low') effortClass = 'effort-low';
                else if (effort === 'high') effortClass = 'effort-high';

                const conf = d.confidence ? Math.round(d.confidence * 100) + '%' : '-';
                
                let turnType = 'Tiếp tục';
                if (d.escalation && d.escalation.toLowerCase().includes('planning')) {
                    turnType = '<span class="badge-planning">🎯 Planning</span>';
                } else if (d.escalation) {
                    turnType = `<span class="badge-escalate">⚡ ${escapeHtml(d.escalation)}</span>`;
                } else if (d.is_new_turn) {
                    turnType = 'Mới (New turn)';
                }

                return `<tr>
                    <td style="color:var(--text-muted); font-size:12px; white-space:nowrap;">${d.datetime || '-'}</td>
                    <td class="prompt-cell" title="${escapeHtml(d.prompt || '')}">${escapeHtml(d.prompt || '(Trống)')}</td>
                    <td><span class="tier-badge ${tierClass}">${tier}</span></td>
                    <td><span class="effort-badge ${effortClass}">${effort}</span></td>
                    <td style="font-size:12px; color:var(--text-muted);">${conf}</td>
                    <td class="model-cell">${escapeHtml(d.selected_model || '-')}</td>
                    <td class="orig-cell">${escapeHtml(d.original_model || '-')}</td>
                    <td style="font-size:12px; color:var(--text-muted);">${turnType}</td>
                </tr>`;
            }).join('');
        }

        function escapeHtml(str) {
            return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
        }

        async function testJev(isPlanning = false) {
            const btn = isPlanning ? document.getElementById('btn-test-plan') : document.getElementById('btn-test');
            const alertBox = document.getElementById('test-alert');
            const origText = btn.textContent;
            btn.disabled = true;
            btn.textContent = '⏳ Đang test...';
            alertBox.style.display = 'none';

            const testPrompt = isPlanning
                ? 'Lên plan chi tiết thiết kế kiến trúc hệ thống và các bước triển khai TDD'
                : 'Write a simple unit test in Python';

            try {
                const res = await fetch(getApiUrl('test-jev?prompt=' + encodeURIComponent(testPrompt)));
                const json = await res.json();
                if (json.success) {
                    alertBox.className = 'alert-success';
                    alertBox.innerHTML = `✅ Test thành công! Jev phản hồi trong ${json.elapsed_ms}ms.<br>` +
                        `• Prompt test: <em>"${escapeHtml(json.prompt)}"</em><br>` +
                        `• Nhận diện Planning: <strong>${json.is_planning ? '🎯 CÓ (Ưu tiên Sol/Astra + High Reasoning)' : 'Không (Tiêu chuẩn)'}</strong><br>` +
                        `• Tier: <strong>${json.tier.toUpperCase()}</strong> (Tin cậy: ${Math.round(json.confidence * 100)}%)<br>` +
                        `• Reasoning Effort: <strong>${json.reasoning_effort.toUpperCase()}</strong> (Tin cậy: ${Math.round(json.reasoning_conf * 100)}%)<br>` +
                        `• Mapped Model: <strong>${json.mapped_model}</strong>`;
                } else {
                    alertBox.className = 'alert-error';
                    alertBox.innerHTML = `❌ Test thất bại: ${json.error}`;
                }
            } catch (e) {
                alertBox.className = 'alert-error';
                alertBox.innerHTML = `❌ Lỗi kết nối: ${e.message}`;
            } finally {
                alertBox.style.display = 'block';
                btn.disabled = false;
                btn.textContent = origText;
                setTimeout(fetchData, 1000);
            }
        }

        /* ================= SETTINGS LOGIC ================= */
        let upstreamModelsCache = [];

        async function fetchUpstreamModels(manualClick = false) {
            const btn = document.getElementById('btn-fetch-models');
            const alertBox = document.getElementById('settings-alert');
            const badge = document.getElementById('models-count-badge');
            if (manualClick) {
                btn.disabled = true;
                btn.textContent = '⏳ Đang tải...';
            }
            try {
                const res = await fetch(getApiUrl('upstream-models'));
                const json = await res.json();
                if (json.success && Array.isArray(json.models)) {
                    upstreamModelsCache = json.models;
                    badge.textContent = `${json.models.length} models khả dụng`;
                    badge.style.display = 'inline-block';
                    populateModelSelects(json.models);
                    if (manualClick) {
                        alertBox.className = 'alert-success';
                        alertBox.innerHTML = `✅ Đã tải thành công <strong>${json.models.length}</strong> models từ CLIProxyAPI! Bạn có thể chọn nhanh từ dropdown hoặc gõ tên model để tự hoàn tất.`;
                        alertBox.style.display = 'block';
                    }
                } else {
                    if (manualClick) {
                        alertBox.className = 'alert-error';
                        alertBox.innerHTML = `❌ Không thể tải danh sách model: ${json.error || 'Lỗi không xác định'}`;
                        alertBox.style.display = 'block';
                    }
                }
            } catch (e) {
                if (manualClick) {
                    alertBox.className = 'alert-error';
                    alertBox.innerHTML = `❌ Lỗi kết nối khi tải model: ${e.message}`;
                    alertBox.style.display = 'block';
                }
            } finally {
                if (manualClick) {
                    btn.disabled = false;
                    btn.textContent = '🔄 Lấy danh sách từ CLIProxyAPI';
                }
            }
        }

        function populateModelSelects(models) {
            const datalist = document.getElementById('upstream-models-list');
            if (datalist) {
                datalist.innerHTML = models.map(m => `<option value="${escapeHtml(m.id)}">${escapeHtml(m.id)} (${escapeHtml(m.owned_by)})</option>`).join('');
            }

            const groups = {
                'OpenAI / Codex': [],
                'Anthropic / Claude': [],
                'Google / Gemini': [],
                'Khác': []
            };

            for (const m of models) {
                const id = m.id.toLowerCase();
                if (id.startsWith('gpt') || id.startsWith('codex') || id.startsWith('o1') || id.startsWith('o3') || id.startsWith('text-') || id.startsWith('chatgpt')) {
                    groups['OpenAI / Codex'].push(m);
                } else if (id.startsWith('claude')) {
                    groups['Anthropic / Claude'].push(m);
                } else if (id.startsWith('gemini')) {
                    groups['Google / Gemini'].push(m);
                } else {
                    groups['Khác'].push(m);
                }
            }

            let optionsHtml = '<option value="">-- Chọn nhanh model từ danh sách --</option>';
            for (const [groupName, groupModels] of Object.entries(groups)) {
                if (groupModels.length === 0) continue;
                optionsHtml += `<optgroup label="${groupName} (${groupModels.length})">`;
                for (const m of groupModels) {
                    optionsHtml += `<option value="${escapeHtml(m.id)}">${escapeHtml(m.id)}</option>`;
                }
                optionsHtml += `</optgroup>`;
            }

            const tiers = ['luna', 'terra', 'sol', 'astra', 'fallback'];
            for (const tier of tiers) {
                const sel = document.getElementById('quick-select-' + tier);
                if (sel) {
                    sel.innerHTML = optionsHtml;
                    const curVal = document.getElementById('set-model-' + tier).value;
                    if (curVal) {
                        sel.value = curVal;
                    }
                }
            }
        }

        function applyModelChoice(tier, value) {
            if (!value) return;
            const input = document.getElementById('set-model-' + tier);
            if (input) {
                input.value = value;
                input.style.borderColor = 'var(--accent)';
                setTimeout(() => { input.style.borderColor = ''; }, 600);
            }
        }

        async function loadSettings() {
            const alertBox = document.getElementById('settings-alert');
            try {
                const res = await fetch(getApiUrl('settings'));
                const data = await res.json();
                defaultSettingsRef = data;

                // Populate models
                const m = data.models || {};
                document.getElementById('set-model-luna').value = m.luna || '';
                document.getElementById('set-model-terra').value = m.terra || '';
                document.getElementById('set-model-sol').value = m.sol || '';
                document.getElementById('set-model-astra').value = m.astra || '';
                document.getElementById('set-model-fallback').value = m.fallback || '';

                // Populate upstream & jev
                document.getElementById('set-upstream-url').value = (data.upstream && data.upstream.url) || '';
                document.getElementById('set-upstream-key').placeholder = (data.upstream && data.upstream.api_key_masked) || 'Để trống nếu không đổi';
                document.getElementById('set-upstream-key').value = '';
                document.getElementById('set-jev-model').value = (data.jev && data.jev.model) || '';
                document.getElementById('set-jev-key').placeholder = (data.jev && data.jev.api_key_masked) || 'Để trống nếu không đổi';
                document.getElementById('set-jev-key').value = '';

                // Populate rules
                const r = data.routing_rules || {};
                document.getElementById('set-rule-rewrite').checked = !!r.rewrite_enabled;
                document.getElementById('set-rule-planning').checked = !!r.planning_boost;
                document.getElementById('set-rule-escalate').checked = !!r.auto_escalation;
                document.getElementById('set-rule-cooldown').value = r.escalation_cooldown || 20;

                document.getElementById('set-keywords-planning').value = (r.planning_keywords || []).join('\\n');
                document.getElementById('set-keywords-arch').value = (r.architecture_keywords || []).join('\\n');

                // Load upstream models
                fetchUpstreamModels(false);
            } catch (err) {
                alertBox.className = 'alert-error';
                alertBox.innerHTML = `❌ Không thể tải cài đặt: ${err.message}`;
                alertBox.style.display = 'block';
            }
        }

        async function saveSettings() {
            const btn = document.getElementById('btn-save-settings');
            const alertBox = document.getElementById('settings-alert');
            btn.disabled = true;
            btn.textContent = '⏳ Đang lưu...';
            alertBox.style.display = 'none';

            const payload = {
                models: {
                    luna: document.getElementById('set-model-luna').value.trim(),
                    terra: document.getElementById('set-model-terra').value.trim(),
                    sol: document.getElementById('set-model-sol').value.trim(),
                    astra: document.getElementById('set-model-astra').value.trim(),
                    fallback: document.getElementById('set-model-fallback').value.trim()
                },
                upstream: {
                    url: document.getElementById('set-upstream-url').value.trim(),
                    api_key: document.getElementById('set-upstream-key').value.trim()
                },
                jev: {
                    model: document.getElementById('set-jev-model').value.trim(),
                    api_key: document.getElementById('set-jev-key').value.trim()
                },
                routing_rules: {
                    rewrite_enabled: document.getElementById('set-rule-rewrite').checked,
                    planning_boost: document.getElementById('set-rule-planning').checked,
                    auto_escalation: document.getElementById('set-rule-escalate').checked,
                    escalation_cooldown: parseFloat(document.getElementById('set-rule-cooldown').value) || 20.0,
                    planning_keywords: document.getElementById('set-keywords-planning').value.split(/[\\r\\n,]+/).map(x => x.trim()).filter(Boolean),
                    architecture_keywords: document.getElementById('set-keywords-arch').value.split(/[\\r\\n,]+/).map(x => x.trim()).filter(Boolean)
                }
            };

            try {
                const res = await fetch(getApiUrl('settings'), {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                const resJson = await res.json();
                if (resJson.success) {
                    alertBox.className = 'alert-success';
                    alertBox.innerHTML = `✅ ${resJson.message || 'Cấu hình đã được lưu và áp dụng thành công!'}`;
                    loadSettings();
                } else {
                    alertBox.className = 'alert-error';
                    alertBox.innerHTML = `❌ Lỗi khi lưu: ${resJson.message || 'Thất bại'}`;
                }
            } catch (err) {
                alertBox.className = 'alert-error';
                alertBox.innerHTML = `❌ Lỗi kết nối khi lưu: ${err.message}`;
            } finally {
                alertBox.style.display = 'block';
                btn.disabled = false;
                btn.textContent = '💾 Lưu Cấu hình & Áp dụng ngay';
            }
        }

        function resetDefaultSettings() {
            if (!confirm("Bạn có chắc chắn muốn khôi phục cấu hình mặc định ban đầu không?")) return;
            document.getElementById('set-model-luna').value = 'gpt-6-luna';
            document.getElementById('set-model-terra').value = 'gpt-6-sol';
            document.getElementById('set-model-sol').value = 'gpt-6.1-sol';
            document.getElementById('set-model-astra').value = 'gpt-6-astra';
            document.getElementById('set-model-fallback').value = 'gpt-6.1-sol';
            document.getElementById('set-upstream-url').value = 'http://127.0.0.1:8317';
            document.getElementById('set-upstream-key').value = '';
            document.getElementById('set-jev-model').value = 'typesafe/jev-1.13';
            document.getElementById('set-rule-rewrite').checked = true;
            document.getElementById('set-rule-planning').checked = true;
            document.getElementById('set-rule-escalate').checked = true;
            document.getElementById('set-rule-cooldown').value = 20;
            saveSettings();
        }

        function toggleKeyVisibility() {
            const input = document.getElementById('set-jev-key');
            const btn = document.getElementById('btn-toggle-key');
            if (input.type === 'password') {
                input.type = 'text';
                btn.textContent = '🔒';
            } else {
                input.type = 'password';
                btn.textContent = '👁️';
            }
        }

        function toggleUpstreamKeyVisibility() {
            const input = document.getElementById('set-upstream-key');
            const btn = document.getElementById('btn-toggle-upstream-key');
            if (input.type === 'password') {
                input.type = 'text';
                btn.textContent = '🔒';
            } else {
                input.type = 'password';
                btn.textContent = '👁️';
            }
        }

        // Initialize based on URL hash
        const initialTab = (window.location.hash || '').replace('#', '');
        if (initialTab === 'settings') {
            switchTab('settings');
        } else {
            switchTab('dashboard');
        }

        setInterval(() => {
            if (currentTab === 'dashboard') {
                fetchData();
            }
        }, 3000);
    </script>
</body>
</html>
"""

DASHBOARD_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")


def load_dashboard_content() -> str:
    if os.path.isfile(DASHBOARD_FILE):
        try:
            with open(DASHBOARD_FILE, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            print(f"[DASHBOARD FILE READ ERROR] {e}", flush=True)
    return DASHBOARD_HTML


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        if DEBUG_DUMPS:
            print("http", args[0] if args else fmt, flush=True)

    def do_GET(self) -> None:
        clean_path = self.path.split("?", 1)[0].rstrip("/")
        is_html = "text/html" in self.headers.get("Accept", "")

        if clean_path in ("/dashboard", "/monitor", "/status", "/_jev", "/_jev/status"):
            self._serve_dashboard()
            return
        elif clean_path == "":
            if is_html:
                self._serve_dashboard()
            else:
                self._serve_api_stats()
            return
        elif clean_path == "/api/stats":
            self._serve_api_stats()
            return
        elif clean_path == "/api/decisions":
            self._serve_api_decisions()
            return
        elif clean_path == "/api/diffs":
            self._serve_api_diffs()
            return
        elif clean_path == "/api/test-jev":
            self._serve_api_test_jev()
            return
        elif clean_path == "/api/settings":
            self._serve_api_get_settings()
            return
        elif clean_path == "/api/upstream-models":
            self._serve_api_upstream_models()
            return

        self._forward("GET")

    def do_HEAD(self) -> None:
        clean_path = self.path.split("?", 1)[0].rstrip("/")
        is_html = "text/html" in self.headers.get("Accept", "")
        if clean_path in ("/dashboard", "/monitor", "/status", "/_jev", "/_jev/status") or (clean_path == "" and is_html):
            body = load_dashboard_content().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return
        self._forward("HEAD")

    def _serve_dashboard(self) -> None:
        body = load_dashboard_content().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_api_stats(self) -> None:
        data = query_stats()
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _serve_api_decisions(self) -> None:
        limit = 50
        if "limit=" in self.path:
            try:
                limit = int(self.path.split("limit=")[1].split("&")[0])
            except Exception:
                limit = 50
        data = query_recent_decisions(limit=limit)
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _serve_api_diffs(self) -> None:
        limit = 50
        if "limit=" in self.path:
            try:
                limit = int(self.path.split("limit=")[1].split("&")[0])
            except Exception:
                limit = 50
        data = query_recent_diffs(limit=limit)
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _serve_api_test_jev(self) -> None:
        t0 = time.time()
        try:
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            test_prompt = qs.get("prompt", ["Write a simple unit test in Python"])[0]

            is_planning = bool(PLANNING_RX.search(test_prompt))
            is_heavy_arch = bool(ARCH_RX.search(test_prompt)) if is_planning else False

            extra = ""
            if is_planning:
                extra = " NOTE: The user is in PLANNING / DESIGN mode (e.g. Superpowers plan or implementation spec). Select sol or astra, with reasoning_effort high."

            tier, conf, effort, e_conf = ask_jev(test_prompt, extra)

            # Enforce planning boost guarantee
            if is_planning:
                if tier in ("luna", "terra"):
                    tier = "sol"
                    conf = max(conf, 0.95)
                if is_heavy_arch and tier != "astra":
                    tier = "astra"
                    conf = max(conf, 0.95)
                effort = "high"
                e_conf = max(e_conf, 0.95)

            elapsed = round((time.time() - t0) * 1000, 1)
            resp = {
                "success": True,
                "prompt": test_prompt,
                "is_planning": is_planning,
                "tier": tier,
                "confidence": conf,
                "reasoning_effort": effort,
                "reasoning_conf": e_conf,
                "mapped_model": MAP.get(tier),
                "elapsed_ms": elapsed,
                "jev_url": JEV_URL,
                "jev_model": JEV_MODEL,
            }
        except Exception as e:
            elapsed = round((time.time() - t0) * 1000, 1)
            resp = {
                "success": False,
                "error": str(e),
                "elapsed_ms": elapsed,
                "jev_url": JEV_URL,
                "jev_model": JEV_MODEL,
            }
        body = json.dumps(resp, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _serve_api_test_optimizer(self) -> None:
        content_len = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(content_len) if content_len > 0 else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
            prompt = payload.get("prompt", "").strip()
            force = bool(payload.get("force_optimize", False))
            if not prompt:
                resp = {"success": False, "error": "Prompt không được để trống"}
            else:
                is_eligible, reason = check_prompt_heuristic(prompt)
                if not is_eligible and not force:
                    resp = {
                        "success": True,
                        "raw_prompt": prompt,
                        "optimized_prompt": prompt,
                        "opt_status": f"BYPASS_{reason}",
                        "opt_latency_ms": 0.0,
                        "diff_summary": "",
                        "is_eligible": False,
                        "bypass_reason": reason,
                    }
                else:
                    opt_res, success, status, lat_ms = run_prompt_optimizer(prompt)
                    diff_data = generate_diff_summary(prompt, opt_res if success else prompt)
                    resp = {
                        "success": success,
                        "raw_prompt": prompt,
                        "optimized_prompt": opt_res if success else prompt,
                        "opt_status": status,
                        "opt_latency_ms": lat_ms,
                        "diff_summary": diff_data.get("unified_diff", ""),
                        "diff_stats": diff_data,
                        "is_eligible": is_eligible,
                    }
        except Exception as e:
            resp = {"success": False, "error": str(e)}

        body = json.dumps(resp, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _serve_api_test_pipeline(self) -> None:
        content_len = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(content_len) if content_len > 0 else b"{}"
        t_all = time.time()
        try:
            payload = json.loads(raw.decode("utf-8"))
            prompt = payload.get("prompt", "").strip()
            force = bool(payload.get("force_optimize", False))
            if not prompt:
                resp = {"success": False, "error": "Prompt không được để trống"}
            else:
                raw_prompt = prompt
                opt_prompt = raw_prompt
                opt_status = "BYPASS_DISABLED"
                opt_latency = 0.0
                diff_summary = ""
                diff_stats = {}
                is_eligible, reason = check_prompt_heuristic(raw_prompt)

                if OPT_ENABLED and (is_eligible or force):
                    opt_res, success, status, lat_ms = run_prompt_optimizer(raw_prompt)
                    opt_status = status
                    opt_latency = lat_ms
                    if success:
                        opt_prompt = opt_res
                        diff_data = generate_diff_summary(raw_prompt, opt_prompt)
                        diff_summary = diff_data.get("unified_diff", "")
                        diff_stats = diff_data
                elif not is_eligible:
                    opt_status = f"BYPASS_{reason}"

                prompt_to_classify = opt_prompt
                is_planning = bool(PLANNING_RX.search(prompt_to_classify))
                is_heavy_arch = bool(ARCH_RX.search(prompt_to_classify)) if is_planning else False

                extra = ""
                if is_planning:
                    extra = " NOTE: The user is in PLANNING / DESIGN mode (e.g. Superpowers plan or implementation spec). Select sol or astra, with reasoning_effort high."

                t_jev = time.time()
                tier, conf, effort, e_conf = ask_jev(prompt_to_classify, extra)
                jev_latency = round((time.time() - t_jev) * 1000, 1)

                if is_planning:
                    if tier in ("luna", "terra"):
                        tier = "sol"
                        conf = max(conf, 0.95)
                    if is_heavy_arch and tier != "astra":
                        tier = "astra"
                        conf = max(conf, 0.95)
                    effort = "high"
                    e_conf = max(e_conf, 0.95)

                total_latency = round((time.time() - t_all) * 1000, 1)
                selected_model = MAP.get(tier, DEFAULT_MODEL)

                resp = {
                    "success": True,
                    "raw_prompt": raw_prompt,
                    "optimized_prompt": opt_prompt,
                    "opt_status": opt_status,
                    "opt_latency_ms": opt_latency,
                    "diff_summary": diff_summary,
                    "diff_stats": diff_stats,
                    "jev_latency_ms": jev_latency,
                    "total_latency_ms": total_latency,
                    "is_planning": is_planning,
                    "is_heavy_arch": is_heavy_arch,
                    "tier": tier,
                    "confidence": conf,
                    "reasoning_effort": effort,
                    "reasoning_conf": e_conf,
                    "selected_model": selected_model,
                }
        except Exception as e:
            resp = {"success": False, "error": str(e), "total_latency_ms": round((time.time() - t_all) * 1000, 1)}

        body = json.dumps(resp, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _serve_api_get_settings(self) -> None:
        data = get_current_settings()
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _serve_api_save_settings(self) -> None:
        content_len = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(content_len) if content_len > 0 else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
            success, msg = save_settings_data(payload)
            resp = {"success": success, "message": msg}
        except Exception as e:
            resp = {"success": False, "message": f"Dữ liệu JSON không hợp lệ: {e}"}

        body = json.dumps(resp, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _serve_api_upstream_models(self) -> None:
        data = fetch_upstream_models()
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        clean_path = self.path.split("?", 1)[0].rstrip("/")
        if clean_path == "/api/settings":
            self._serve_api_save_settings()
            return
        elif clean_path == "/api/test-pipeline":
            self._serve_api_test_pipeline()
            return
        elif clean_path == "/api/test-optimizer":
            self._serve_api_test_optimizer()
            return
        self._forward("POST")

    def do_PUT(self) -> None:
        self._forward("PUT")

    def do_DELETE(self) -> None:
        self._forward("DELETE")

    def do_PATCH(self) -> None:
        self._forward("PATCH")

    def do_OPTIONS(self) -> None:
        clean_path = self.path.split("?", 1)[0].rstrip("/")
        if clean_path.startswith("/api/"):
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._forward("OPTIONS")

    def _forward(self, method: str) -> None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        path = self.path if self.path.startswith("/") else "/" + self.path
        url = UPSTREAM_ORIGIN + path

        if method == "POST" and ("/responses" in path.split("?", 1)[0] or "/chat/completions" in path.split("?", 1)[0]):
            if DEBUG_DUMPS:
                try:
                    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "req-headers.jsonl"), "a") as _f:
                        _f.write(json.dumps({
                            "t": time.time(), "path": path, "len": len(raw),
                            "headers": {k: v for k, v in self.headers.items() if k.lower() not in ("authorization", "x-api-key")},
                        }, ensure_ascii=False) + "\n")
                except Exception as _ex:
                    print("hdrlog fail", _ex, flush=True)

            _enc = (self.headers.get("Content-Encoding") or "").lower()
            _orig = raw
            _plain = raw
            if _enc == "zstd" and raw:
                try:
                    try:
                        from compression import zstd as _zstd  # Python 3.14+
                    except ImportError:
                        import zstandard as _z  # pip install zstandard

                        class _zstd:  # minimal shim
                            decompress = staticmethod(lambda b: _z.ZstdDecompressor().decompressobj().decompress(b))
                            compress = staticmethod(lambda b: _z.ZstdCompressor().compress(b))
                    _plain = _zstd.decompress(raw)
                except Exception as _ex:
                    print("zstd decode fail", _ex, flush=True)
                    _plain = None

            if _plain is None:
                raw, meta = _orig, {"tier": None, "conf": None, "in": None, "out": None, "new": False, "prompt": ""}
            else:
                _new, meta = maybe_rewrite(_plain, path, dict(self.headers.items()))
                if _new is _plain:
                    raw = _orig
                elif _enc == "zstd":
                    raw = _zstd.compress(_new)
                else:
                    raw = _new

            slim = {
                "t": time.time(),
                "datetime": time.strftime("%Y-%m-%d %H:%M:%S"),
                "new": meta.get("new"),
                "prompt": meta.get("prompt"),
                "tier": meta.get("tier"),
                "conf": meta.get("conf"),
                "effort": meta.get("effort"),
                "effort_conf": meta.get("effort_conf"),
                "in": meta.get("in"),
                "out": meta.get("out"),
                "session_id": meta.get("sid"),
            }
            if meta.get("opt_status"):
                slim["opt_status"] = meta["opt_status"]
                slim["opt_latency_ms"] = meta.get("opt_latency_ms")
            if meta.get("hist") is not None:
                slim["hist"] = meta["hist"]
            if meta.get("probs"):
                slim["probs"] = meta["probs"]
            if meta.get("effort_probs"):
                slim["effort_probs"] = meta["effort_probs"]
            if meta.get("err"):
                slim["err"] = meta["err"]
            for _k in ("err_streak", "esc_tier", "esc_from", "esc_effort", "esc_probs", "esc_err"):
                if meta.get(_k) is not None:
                    slim[_k] = meta[_k]

            # Write to JSONL
            log(slim)

            # Write to SQLite DB (only on new turn, continuation, or actual model upgrade)
            if meta.get("new") or meta.get("tier") == "reuse" or meta.get("esc_from"):
                log_decision_db(meta, meta.get("sid", ""))

            # Output to stdout/journalctl
            if meta.get("new"):
                if meta.get("opt_status") == "OPTIMIZED":
                    print(f"[PROMPT OPTIMIZER] Optimized in {meta.get('opt_latency_ms')}ms: {meta.get('raw_prompt')[:40]!r} -> {meta.get('optimized_prompt')[:40]!r}", flush=True)
                print(f"[JEV ROUTE] Turn: {meta.get('prompt')[:60]!r} -> Tier: {meta.get('tier')} ({int(meta.get('conf', 0)*100)}%) | Reasoning: {meta.get('effort')} ({int(meta.get('effort_conf', 0)*100)}%) -> Model: {meta.get('out')} (in: {meta.get('in')})", flush=True)
            elif meta.get("esc_from"):
                print(f"[JEV ESCALATE] Upgraded {meta.get('esc_from')} -> {meta.get('out')} (Tier: {meta.get('tier')}, Effort: {meta.get('effort')}) due to tool failure", flush=True)
            elif meta.get("tier") == "reuse":
                print(f"[JEV REUSE] Continuation turn -> Model: {meta.get('out')} (Effort: {meta.get('effort')})", flush=True)
            elif meta.get("out") and meta.get("out") != meta.get("in"):
                print(f"[JEV REWRITE] Path: {path} -> Model: {meta.get('out')} (Effort: {meta.get('effort')})", flush=True)

        headers = copy_req_headers(self)
        if UPSTREAM_KEY and not headers.get("Authorization"):
            headers["Authorization"] = f"Bearer {UPSTREAM_KEY}"
        if raw:
            headers["Content-Length"] = str(len(raw))
        elif "Content-Length" in headers:
            del headers["Content-Length"]

        req = urllib.request.Request(
            url, data=raw or None, headers=headers, method=method
        )
        try:
            up = open_with_retry(req, method, path)
        except urllib.error.HTTPError as e:
            err_body = e.read() or str(e).encode()
            self.send_response(e.code)
            ctype = e.headers.get("Content-Type", "text/plain") if e.headers else "text/plain"
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(err_body)))
            self.end_headers()
            self.wfile.write(err_body)
            print("UPSTREAM HTTP", e.code, method, path, flush=True)
            if e.code == 400 and DEBUG_DUMPS:
                try:
                    import time as _t, os as _os
                    d = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "errors-400")
                    _os.makedirs(d, exist_ok=True)
                    base = _os.path.join(d, _t.strftime("%Y%m%d-%H%M%S") + "-" + str(int(_t.time()*1000) % 1000))
                    with open(base + ".json", "w") as f:
                        json.dump({
                            "method": method, "path": path, "url": url,
                            "status": e.code,
                            "resp_headers": dict(e.headers.items()) if e.headers else {},
                            "resp_body": err_body.decode("utf-8", "replace"),
                            "fwd_req_headers": {k: v for k, v in headers.items() if k.lower() not in ("authorization", "x-api-key")},
                            "req_body_len": len(raw) if raw else 0,
                        }, f, ensure_ascii=False, indent=2)
                    if raw:
                        with open(base + ".req.json", "wb") as f:
                            f.write(raw)
                except Exception as _ex:
                    print("dump400 fail", _ex, flush=True)
            return
        except Exception as e:
            msg = f"forward fail {method} {path}: {e}".encode()
            self.send_response(502)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
            print("FORWARD FAIL", method, path, e, flush=True)
            return

        self.send_response(up.status)
        for k, v in up.headers.items():
            if k.lower() in HOP_BY_HOP:
                continue
            self.send_header(k, v)
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        try:
            while True:
                chunk = up.read(4096)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            up.close()


def main() -> None:
    init_db()
    print(
        f"jev-router http://{LISTEN_HOST}:{LISTEN_PORT} -> {UPSTREAM_ORIGIN}  "
        f"REWRITE={REWRITE} key_len={len(KEY)} jev_url={JEV_URL} jev_model={JEV_MODEL} db={DB_PATH}",
        flush=True,
    )
    ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()