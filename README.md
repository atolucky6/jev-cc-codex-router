**English** | [简体中文](README.zh-CN.md)

# jev-cc-codex-router

A single-file Python proxy that routes each Codex turn to the cheapest model tier that can handle it.

It sits between Codex and any OpenAI **Responses API**-compatible upstream (a local gateway such as cc-switch, or a remote endpoint). On every new user turn it asks [Jev](https://docs.typesafe.ai) (TypeSafe System One) which tier the task needs, rewrites the request's `model` field, and keeps that choice for the rest of the turn.

> Status: early. It works on the author's setup, but the tier prompts are still being tuned. One small self-measured cost estimate, with its conditions and limits, is in [BENCHMARK.md](BENCHMARK.md). Do not expect a specific saving.

## How it decides

- **New user turn:** the proxy sends Jev the current message plus recent conversation (up to 50 turns, about 4200 characters, tool calls and system prompts removed) and asks for one of four tiers: `luna`, `terra`, `sol`, `astra`. The tier is mapped to a model name (configurable) and stored per session.
- **Rest of the turn:** tool-result follow-up requests reuse the stored model, so there is no extra Jev call.
- **Bare continuation words** ("继续", "retry", "continue", ...) reuse the previous tier without asking Jev.
- **Escalation on failure:** if a tool output shows a clear failure (Traceback, non-zero exit code, `command not found`, `N failed`, merge conflict, ...), the proxy asks Jev once more with the task and the error. It switches only if Jev picks a higher tier, never lower, and asks at most once every 20 seconds per session.
- **Fallback:** if Jev returns an unknown tier or the call fails, the request goes to the fallback model (default `gpt-5.6-sol`).
- **Left alone:** Codex helper requests (title generation, memory summaries) and requests whose model differs from the session's baseline model pass through unchanged.
- The session is identified by the `session-id` (or `thread-id`) request header. After a restart the proxy knows no sessions, so requests pass through until the next new turn.

## Extra behavior

- For `POST /responses`, retries up to 2 times on transient upstream 502/503/504 or a connection error, using exponential delays starting at 1 second (capped at 10 seconds). Invalid requests and `auth_unavailable` are not retried. It cannot recover a stream cut after a 200 response.
- On `model_not_found` (400/404) or `auth_unavailable` (503), a routed new Responses turn can try the configured fallback model. Successful fallback updates the session lease. Passthrough requests, continuation turns, compressed requests, provider conversation state and tool-call histories are excluded. Fallback does not recurse; transient errors on the fallback still use the normal bounded retry policy.
- Classification failures use the configured fallback model. Settings reject blank or non-string model mappings.
- Handles `Content-Encoding: zstd` request bodies (decompress, rewrite, recompress). Python 3.14+ uses the standard library; older Python needs `pip install zstandard`.
- **Debug output is off by default.** Set `JEV_DEBUG=1` to write `decisions.jsonl` (tier, probabilities, in/out model per request), `req-headers.jsonl` (request headers) and `errors-400/` (full failing 400 requests). These files contain prompt excerpts, so keep them private. They are git-ignored. With debug off, the proxy writes no files.

## Setup

1. Python 3.9+ (3.14 for built-in zstd).
2. `cp .env.example .env` and set `TYPESAFE_API_KEY` (your own TypeSafe key).
3. Set the upstream in `.env` if it is not `http://127.0.0.1:15721`: `JEV_UPSTREAM=http://your-gateway:port`
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

## Dashboard

Open `http://127.0.0.1:8787/dashboard` while the router is running.

- **Overview:** inspect the latest 50 routing records, search or filter by tier, and select a request to see its optimization status, routing decision, model, and reasoning effort. Live activity refreshes every three seconds and can be paused. Model distribution uses all routing records, including continuations.
- **Prompt audit:** compare original and optimized prompts, with unified diff and side-by-side views.
- **Playground:** test optimization, routing, or the full pipeline against your configured services.
- **Settings:** configure tier mappings, connections, routing rules, and optimizer options, then save to apply changes.

The dashboard adapts to mobile screens and reports connection failures while retaining the last successful activity update.

## CI/CD

Pull requests and pushes to `main` run syntax checks and isolated smoke tests in
GitHub Actions. The production host can automatically deploy CI-approved `main`
commits with health checks and code rollback. See [deployment setup and operations](deploy/README.md).

## Configuration

All settings are environment variables (or lines in `.env`). See `.env.example` for the full list: listen address, upstream, Jev API URL, model name for each tier, fallback model, retry count and delay, log path, and `JEV_REWRITE=0` to turn routing off and only proxy and retry.

## Notes and limits

- Costs one small Jev call per new turn (plus at most one per failing step). Latency is roughly the Jev response time, a few seconds at worst, with a 15 second timeout.
- The tier definitions are in `ask_jev()`. Edit them to match your own work; they currently reflect a coding-agent workload. Some prompt text and the continuation-word list are in Chinese on purpose (the author works in Chinese); they still work for English input.
- Model names default to `gpt-5.6-luna/terra/sol` and `gpt-6-astra`. Change them to whatever your upstream serves.
- Not affiliated with TypeSafe or OpenAI.

## License

MIT
