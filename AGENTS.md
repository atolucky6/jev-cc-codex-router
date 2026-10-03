# Repository Guidelines

## Project Structure & Module Organization

- `jev_router.py` contains the HTTP proxy, per-session model routing, reasoning-effort selection, prompt optimization, SQLite logging, dashboard API, and embedded fallback UI.
- `dashboard.html` provides the standalone dashboard with inline CSS and JavaScript. Keep UI changes here unless the embedded fallback also needs updating.
- `settings.json` stores persisted runtime settings; `.env.example` documents environment configuration.
- `README.md` and `BENCHMARK.md` describe usage and measured results; corresponding `.zh-CN.md` files provide Chinese documentation.
- There are currently no separate source, asset, or test directories and no package manifest.

## Build, Test, and Development Commands

Use Python 3.9+ from the repository root. No build step is required.

- `Copy-Item .env.example .env` — create local configuration in PowerShell; fill in credentials and upstream settings before starting.
- `python -m pip install zstandard` — enable compressed request handling on Python versions before 3.14; Python 3.14+ includes support.
- `python jev_router.py` — start the proxy, normally at `127.0.0.1:8787`. Open `http://127.0.0.1:8787/dashboard` to inspect routing.
- `python -m py_compile jev_router.py` — check Python syntax without starting the server.

## Coding Style & Naming Conventions

Match existing four-space indentation. Use `snake_case` for Python functions and variables, `UPPER_SNAKE_CASE` for constants, and type annotations consistent with nearby code. Dashboard JavaScript uses `camelCase`. Preserve intentional multilingual routing keywords and prompts. No formatter or linter is configured; avoid unrelated formatting changes and unnecessary dependencies.

## Testing Guidelines

No automated test framework or coverage threshold is configured. Run the syntax check and document manual verification. For routing changes, check new-turn selection, continuation reuse, escalation, fallback, and unchanged passthrough requests using a controlled upstream. For dashboard changes, verify the affected tabs and settings flow. If adding automated tests, prefer standard-library `unittest`, name files `tests/test_*.py`, and run `python -m unittest discover -s tests`.

## Commit & Pull Request Guidelines

Recent commits use `feat: <description>`; older commits use imperative summaries. Prefer concise, scoped messages such as `fix: preserve model on continuation`. PRs should explain the behavior change, link relevant issues, list verification performed, and include screenshots for dashboard changes. Update affected documentation when changing configuration or routing behavior.

## Security & Configuration

Keep credentials in local environment configuration. Inspect `settings.json` diffs for raw keys before committing. Never commit `.env`, decision databases, debug dumps, or prompt-bearing logs. Keep the dashboard bound to localhost during development.
