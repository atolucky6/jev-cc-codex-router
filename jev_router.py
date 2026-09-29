#!/usr/bin/env python3
"""jev-router: per-turn model routing proxy for Codex (Responses API).

Sits between Codex and any OpenAI-Responses-compatible upstream. On each new user
turn it asks Jev (TypeSafe System One) which model tier the task needs, rewrites
the request's `model` field, and reuses that choice for the rest of the turn.
Also retries flaky upstream 400/502/503/504 on POST /responses.
Configuration: environment variables, see .env.example and README.md.
"""

from __future__ import annotations

import json
import re
import os
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


LISTEN_HOST = _env("JEV_LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(_env("JEV_LISTEN_PORT", "8787"))
UPSTREAM_ORIGIN = _env("JEV_UPSTREAM", "http://127.0.0.1:15721").rstrip("/")
JEV_URL = _env("JEV_API_URL", "https://api.typesafe.ai/v1/systemone")
DEBUG_DUMPS = _env("JEV_DEBUG", "0") in ("1", "true", "True")  # debug on: write decisions/headers/400 dumps; off: write nothing 开启调试才写日志
LOG = _env("JEV_LOG", os.path.join(os.path.dirname(os.path.abspath(__file__)), "decisions.jsonl"))

REWRITE = _env("JEV_REWRITE", "1") not in ("0", "false", "False", "")
DEFAULT_MODEL = _env("JEV_FALLBACK_MODEL", "gpt-5.6-sol")
MAP = {
    "luna": _env("JEV_MODEL_LUNA", "gpt-5.6-luna"),
    "terra": _env("JEV_MODEL_TERRA", "gpt-5.6-terra"),
    "sol": _env("JEV_MODEL_SOL", "gpt-5.6-sol"),
    "astra": _env("JEV_MODEL_ASTRA", "gpt-6-astra"),
}

LEASE: dict[str, str] = {}
BASE: dict[str, str] = {}
LAST_PROBS: dict = {}
# Bare continuation words (Chinese and English) that reuse the previous tier / 纯接续词（中英文），直接沿用上一档
CONTINUE_WORDS = {"继续", "继续吧", "接着", "接着来", "go on", "continue", "keep going", "proceed", "go", "ok", "好", "好的", "可以", "行", "是的", "yes", "你再试试", "再试试", "再试一次", "重试", "再来", "再来一次", "retry", "try again", "again"}
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


def load_key() -> str:
    """Read TYPESAFE_API_KEY from the environment, else from .env next to this script."""
    if os.environ.get("TYPESAFE_API_KEY"):
        return os.environ["TYPESAFE_API_KEY"]
    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.isfile(env_file):
        with open(env_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("export "):
                    line = line[7:].strip()
                if line.startswith("TYPESAFE_API_KEY="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    os.environ.setdefault("TYPESAFE_API_KEY", val)
    return os.environ.get("TYPESAFE_API_KEY", "")


KEY = load_key()


def log(obj: dict) -> None:
    """Write the routing decision log only in debug mode. 仅调试模式下写决策日志。"""
    if not DEBUG_DUMPS:
        return
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def ask_jev(text: str, extra: str = "") -> tuple[str, float]:
    if not KEY:
        return "sol", 0.0
    payload = {
        "model": "jev-latest",
        "state": {"task": text[-6000:], "via": "jev-router"},
        "questions": {
            "tier": {
                "type": "choice",
                "instructions": (
                    "Pick the cheapest tier that can finish THIS user turn. "
                    "Ignore the word 继续 when grading; grade the real task. If the input includes earlier conversation, grade the whole ongoing task, not just the last short sentence. "
                    "git/repo/仓库 sync is never luna. "
                    "If unsure, pick terra not luna, sol not astra."
                    + extra
                ),
                "criteria": {
                    "luna": (
                        "Trivial, mechanical, one-shot. "
                        "git status/pull/push/fetch only; rename a symbol; format; typo; "
                        "change one obvious line or one file with a fully specified edit; "
                        "reply to a greeting. No design and no debugging."
                    ),
                    "terra": (
                        "Bounded local work with clear requirements. "
                        "Single-repo rebase/merge with a few known conflicts; "
                        "repo sync in one repository; "
                        "add or fix a function/test in one area; small feature in existing files; "
                        "follow an already agreed approach."
                    ),
                    "sol": (
                        "Non-trivial implementation or diagnosis. "
                        "Cross-file changes; unclear bug; multi-repo or submodule sync with "
                        "divergent history or custom sync scripts; implement something that "
                        "needs reading several modules. Not a full architecture review."
                    ),
                    "astra": (
                        "High-stakes or structural. "
                        "Architecture, authz/authn, concurrency, data migration, "
                        "monorepo/CI/permission redesign, security review, final audit."
                    ),
                },
            }
        },
    }
    req = urllib.request.Request(
        JEV_URL,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = json.load(r)
    ans = (raw.get("answers") or {}).get("tier") or {}
    tier = ans.get("choice") or ans.get("value") or "sol"
    conf = ans.get("confidence")
    if conf is None:
        probs = ans.get("probabilities") or {}
        conf = max(probs.values()) if probs else 0.0
    conf = float(conf or 0)
    LAST_PROBS["v"] = ans.get("probabilities") or {}
    if tier not in MAP:
        tier = "sol"
    return tier, conf


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
    # drop the current prompt (last user row equal to it)
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
        "in": None,
        "out": None,
        "new": False,
        "prompt": "",
    }
    try:
        body = json.loads(raw or b"{}")
    except Exception:
        return raw, meta
    incoming = body.get("model")
    meta["in"] = incoming
    prompt, is_new = last_user_text(body)
    meta["new"] = is_new
    meta["prompt"] = (prompt or "")[:160]
    if not REWRITE or "/responses" not in path.split("?", 1)[0]:
        meta["out"] = incoming
        return raw, meta
    _h = {k.lower(): v for k, v in (hdrs or {}).items()}
    sid = str(
        _h.get("session-id")
        or _h.get("thread-id")
        or body.get("conversation")
        or body.get("conversation_id")
        or "default"
    )
    chosen = incoming
    if is_new and prompt and incoming:
        BASE[sid] = incoming
    _p = prompt.strip().strip("。.!！~ ") if prompt else ""
    if is_new and prompt and _p.lower() in CONTINUE_WORDS and sid in LEASE:
        meta["tier"] = "reuse"
        chosen = LEASE[sid]
    elif is_new and prompt:
        _hist = build_history(body, prompt)
        _ask = prompt
        if _hist:
            _ask = "【此前的对话（旧到新，可能是同一任务的多轮）】\n" + _hist + "\n\n【用户当前这句话，请评估整个任务当前需要的复杂度】\n" + prompt
        meta["hist"] = len(_hist)
        try:
            tier, conf = ask_jev(_ask)
        except Exception as e:
            tier, conf = "sol", 0.0
            meta["err"] = str(e)
        meta["tier"] = tier
        meta["conf"] = conf
        meta["probs"] = dict(LAST_PROBS.get("v") or {})
        chosen = MAP.get(tier, incoming) or incoming
        LEASE[sid] = chosen
    else:
        chosen = LEASE.get(sid, incoming)
        if BASE.get(sid) != incoming:
            chosen = incoming  # Codex switched model itself (helper request): do not override / Codex 自己换了模型(辅助请求), 不覆盖
        elif sid in LEASE:
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
                        _t, _c = ask_jev(_ask, " This is a mid-task step: a tool call just failed. Grade the difficulty of fixing it and finishing the task, from the failure output and the task.")
                        _new = MAP.get(_t)
                        meta["esc_tier"] = _t
                        meta["esc_probs"] = dict(LAST_PROBS.get("v") or {})
                        if _new and RANK.get(_new, -1) > RANK.get(chosen, -1):
                            meta["esc_from"] = chosen
                            chosen = _new
                            LEASE[sid] = chosen
                    except Exception as e:
                        meta["esc_err"] = str(e)

    if not chosen or chosen == incoming:
        meta["out"] = incoming
        return raw, meta
    body["model"] = chosen
    meta["out"] = chosen
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
    """Retry flaky upstream 400/502/503/504 on POST /responses; re-raise the last failure. 上游偶发 400/502 等错误时静默重试(仅 POST /responses), 最后一次失败则原样抛出。"""
    retry_ok = method == "POST" and "/responses" in path.split("?", 1)[0]
    attempt = 0
    while True:
        try:
            up = urllib.request.urlopen(req, timeout=600)
            if attempt:
                print(f"RETRY OK after {attempt} retry", method, path, flush=True)
            return up
        except urllib.error.HTTPError as e:
            if retry_ok and e.code in RETRY_CODES and attempt < RETRY_MAX:
                try:
                    body = e.read()[:200]
                except Exception:
                    body = b""
                attempt += 1
                print(f"RETRY {attempt}/{RETRY_MAX} upstream HTTP {e.code}", path, body, flush=True)
                time.sleep(RETRY_DELAY)
                continue
            raise
        except urllib.error.URLError as e:
            if retry_ok and attempt < RETRY_MAX:
                attempt += 1
                print(f"RETRY {attempt}/{RETRY_MAX} forward error {e}", path, flush=True)
                time.sleep(RETRY_DELAY)
                continue
            raise


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        print("http", args[0] if args else fmt)

    def do_GET(self) -> None:
        self._forward("GET")

    def do_POST(self) -> None:
        self._forward("POST")

    def do_PUT(self) -> None:
        self._forward("PUT")

    def do_DELETE(self) -> None:
        self._forward("DELETE")

    def do_PATCH(self) -> None:
        self._forward("PATCH")

    def do_OPTIONS(self) -> None:
        self._forward("OPTIONS")

    def _forward(self, method: str) -> None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        path = self.path if self.path.startswith("/") else "/" + self.path
        url = UPSTREAM_ORIGIN + path

        if DEBUG_DUMPS and method == "POST" and "/responses" in path.split("?", 1)[0]:
            try:
                with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "req-headers.jsonl"), "a") as _f:
                    _f.write(json.dumps({
                        "t": time.time(), "path": path, "len": len(raw),
                        "headers": {k: v for k, v in self.headers.items() if k.lower() not in ("authorization", "x-api-key")},
                    }, ensure_ascii=False) + "\n")
            except Exception as _ex:
                print("hdrlog fail", _ex)
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
                    print("zstd decode fail", _ex)
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
                "new": meta.get("new"),
                "prompt": meta.get("prompt"),
                "tier": meta.get("tier"),
                "conf": meta.get("conf"),
                "in": meta.get("in"),
                "out": meta.get("out"),
            }
            if meta.get("hist") is not None:
                slim["hist"] = meta["hist"]
            if meta.get("probs"):
                slim["probs"] = meta["probs"]
            if meta.get("err"):
                slim["err"] = meta["err"]
            for _k in ("err_streak", "esc_tier", "esc_from", "esc_probs", "esc_err"):
                if meta.get(_k) is not None:
                    slim[_k] = meta[_k]
            log(slim)

        headers = copy_req_headers(self)
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
            print("UPSTREAM HTTP", e.code, method, path)
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
                    print("dump400 fail", _ex)
            return
        except Exception as e:
            msg = f"forward fail {method} {path}: {e}".encode()
            self.send_response(502)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
            print("FORWARD FAIL", method, path, e)
            return

        self.send_response(up.status)
        for k, v in up.headers.items():
            if k.lower() in HOP_BY_HOP:
                continue
            self.send_header(k, v)
        # Upstream response has no Content-Length and Transfer-Encoding is stripped: end it with Connection: close / 上游响应没有 Content-Length 且已去掉 Transfer-Encoding: 用 Connection: close 标记结束
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
        finally:
            up.close()


def main() -> None:
    print(
        f"jev-router http://{LISTEN_HOST}:{LISTEN_PORT} -> {UPSTREAM_ORIGIN}  "
        f"REWRITE={REWRITE} key_len={len(KEY)}"
    )
    ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()