# jev-router

A single-file Python proxy that routes each Codex turn to the cheapest model tier that can handle it.

It sits between Codex and any OpenAI **Responses API**-compatible upstream (a local gateway such as cc-switch, or a remote endpoint). On every new user turn it asks [Jev](https://docs.typesafe.ai) (TypeSafe System One) which tier the task needs, rewrites the request's `model` field, and keeps that choice for the rest of the turn.

> Status: early. It works on the author's setup, but the tier prompts are still being tuned and there is no cost backtest yet. Do not expect a specific saving. One small self-measured cost estimate, with its conditions and limits, is in [BENCHMARK.md](BENCHMARK.md).

## How it decides

- **New user turn:** the proxy sends Jev the current message plus recent conversation (up to 50 turns, about 4200 characters, tool calls and system prompts removed) and asks for one of four tiers: `luna`, `terra`, `sol`, `astra`. The tier is mapped to a model name (configurable) and stored per session.
- **Rest of the turn:** tool-result follow-up requests reuse the stored model, so there is no extra Jev call.
- **Bare continuation words** ("继续", "retry", "continue", ...) reuse the previous tier without asking Jev.
- **Escalation on failure:** if a tool output shows a clear failure (Traceback, non-zero exit code, `command not found`, `N failed`, merge conflict, ...), the proxy asks Jev once more with the task and the error. It switches only if Jev picks a higher tier, never lower, and asks at most once every 20 seconds per session.
- **Fallback:** if Jev returns an unknown tier or the call fails, the request goes to the fallback model (default `gpt-5.6-sol`).
- **Left alone:** Codex helper requests (title generation, memory summaries) and requests whose model differs from the session's baseline model pass through unchanged.
- The session is identified by the `session-id` (or `thread-id`) request header. After a restart the proxy knows no sessions, so requests pass through until the next new turn.

## Extra behavior

- For `POST /responses`, retries up to 2 times on upstream 400/502/503/504 or a connection error, waiting 1 second. This masks flaky upstreams. It cannot recover a stream that is cut after a 200 response.
- Handles `Content-Encoding: zstd` request bodies (decompress, rewrite, recompress). Python 3.14+ uses the standard library; older Python needs `pip install zstandard`.
- Set `JEV_DEBUG=1` to also dump request headers (`req-headers.jsonl`) and full failing 400 requests (`errors-400/`) for debugging. These contain your prompts; they are off by default and git-ignored.
- Every routing decision is appended to `decisions.jsonl` (tier, probabilities, in/out model). This file contains prompt excerpts, so keep it private.

## Setup

1. Python 3.9+ (3.14 for built-in zstd).
2. `cp .env.example .env` and set `TYPESAFE_API_KEY` (your own TypeSafe key).
3. Set the upstream in `.env` if it is not `http://127.0.0.1:15721`:
   `JEV_UPSTREAM=http://your-gateway:port`
4. Run: `python3 jev_router.py`
5. Point Codex at the proxy in `~/.codex/config.toml`:

```toml
model_provider = "jevrouter"

[model_providers.jevrouter]
name = "Jev Router"
base_url = "http://127.0.0.1:8787/v1"
wire_api = "responses"
```

Adjust the provider fields (auth, `requires_openai_auth`, etc.) to match how you reach your upstream. Requests keep whatever headers Codex sends, so authentication is forwarded to the upstream untouched.

To roll back, point `model_provider` back to your original provider. The proxy holds no persistent state.

## Configuration

All settings are environment variables (or lines in `.env`). See `.env.example` for the full list: listen address, upstream, Jev API URL, model name for each tier, fallback model, retry count and delay, log path, and `JEV_REWRITE=0` to turn routing off and only proxy and retry.

## Notes and limits

- Costs one small Jev call per new turn (plus at most one per failing step). Latency is roughly the Jev response time, a few seconds at worst, with a 15 second timeout.
- The tier definitions are in `ask_jev()`. Edit them to match your own work; they currently reflect a coding-agent workload.
- Model names default to `gpt-5.6-luna/terra/sol` and `gpt-6-astra`. Change them to whatever your upstream serves.
- Not affiliated with TypeSafe or OpenAI.

## License

MIT
