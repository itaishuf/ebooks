# Ebookarr — Project Instructions

Always-on project rules for this repo. Skill-based reference (debug logs, frontend design, plan-driven implementation, E2E tests, Alpine.js job status, auth patterns, Docker/Tailscale Funnel, Selenium downloads) lives in `.opencode/skills/`.

## What this project does

**Ebookarr** is a FastAPI + Alpine.js app that searches for ebooks, downloads them via Selenium/Firefox, and sends them to a Kindle email via Gmail SMTP. It runs via Docker Compose behind a dedicated Tailscale Funnel node (`itai-books`).

## Key files and their roles

| File | Role |
|------|------|
| `service.py` | FastAPI app, security headers, auth/public routes, in-memory `jobs` store |
| `auth.py` | Google OAuth handling, signed session-cookie parsing, API token validation with tailnet IP gating |
| `download_flow.py` | Core pipeline: `get_book_info` (ISBN+title from Goodreads), `search_aa_all_formats` (title-based AA search → per-format MD5 dict), `ebook_download`, `ebook_download_by_md5`, `search_books` |
| `download_with_libgen.py` | Selenium/Firefox download from LibGen (`choose_libgen_mirror`, `get_libgen_link`, `download_book_using_selenium`) |
| `download_with_annas_archive.py` | FlareSolverr-based slow-download path for Anna's Archive |
| `download_proxy.py` | Download proxy helper (fallback routing, resilient downloads) |
| `config.py` | Pydantic-settings `Settings` class for server, Google OAuth, Bitwarden, paths, and mirrors |
| `bitwarden.py` | Bitwarden CLI bootstrap that fills runtime secrets still missing from env |
| `runtime_bootstrap.py` | Startup helpers such as Anna's Archive mirror selection |
| `static/index.html` | Alpine.js SPA with Google OAuth session handling and authenticated fetches |
| `docker-compose.yml` / `tailscale/bootstrap.sh` | Optional Docker + Tailscale Funnel deployment using the `itai-books` node |
| `tests/test_e2e.py` | Real E2E tests — no mocks, require live network + credentials |
| `tests/test_auth.py` / `tests/test_service_auth.py` | Focused auth and protected-route coverage |
| `books.log` | Structured log file including request job/user context |

## Auth and routes

Two authentication paths:
1. **Google OAuth sessions** (primary, web UI) — `/auth/google/login` → Google → `/auth/google/callback` stores a signed session cookie; subsequent requests are authenticated via the cookie.
2. **Static API token** (programmatic clients like iOS Shortcuts) — `Authorization: Bearer <token>`, validated against the `api_token` setting and restricted to Tailscale CGNAT IPs (`100.64.0.0/10`). Query-param auth is rejected everywhere.

- Public routes: `/`, `/health`, `/auth/session`, `/auth/google/login`, `/auth/google/callback`
- Protected read routes: `GET /search`, `GET /jobs/{job_id}`
- Protected write routes: `POST /download`, `POST /download/isbn`, `POST /download/md5`
- `POST /auth/logout` clears the session
- Protected routes use `get_current_user()` (API token first, then session cookie)
- Jobs store `owner_user_id` and `owner_email`; foreign or unknown job lookups return `404`

## Download pipeline status values

`queued` → `fetching_isbn` → `searching` → `downloading` → `sending` → `done`

On failure: `error`

These values are also used in the Alpine.js `steps` array in `static/index.html`. Keep backend and frontend in sync.

## Tech stack

- Python runtime: `uv` (always use `uv run`, never bare `python` or `pytest`)
- Web framework: FastAPI + uvicorn
- HTML/JS: Alpine.js (CDN), no build step
- Browser automation: Selenium + Firefox (geckodriver)
- Auth: Google OAuth + signed session cookies; static API token for programmatic clients
- HTTP client: `aiohttp`
- HTML parsing: BeautifulSoup4
- Config: pydantic-settings + minimal `.env` + Bitwarden bootstrap
- Tests: pytest with `e2e` marker; `uv run pytest -m e2e`

## Use `uv run` for all commands

This project uses `uv` as the Python runtime. Always prefix commands:

```bash
# ✅ Correct
uv run service.py
uv run pytest -v
uv run pytest -m e2e
uv run pytest -m "not e2e"

# ❌ Wrong
python service.py
pytest -v
```

| Task | Command |
|------|---------|
| Start server | `uv run service.py` (port 19191) |
| All tests | `uv run pytest -v` |
| Unit tests only | `uv run pytest -m "not e2e"` |
| E2E tests only | `uv run pytest -m e2e` |
| Health check | `curl -sf http://localhost:19191/health` |

## No unsolicited processes

Never start any of the following unless the user **explicitly** asks:
- The FastAPI server (`uv run service.py`)
- E2E or integration tests (they hit real external services and Gmail)
- Any `curl` requests against the live service

**Why**: The server runs 24/7 via Docker Compose. Starting a second instance causes a port conflict. Sending test emails uses real Gmail credentials and clutters the user's Kindle inbox.

When debugging startup issues or logging, prefer reading `books.log` and suggesting commands for the user to run rather than running the service yourself.

## Accessing production logs

- `docker compose exec ebookarr sh -lc 'ls -lah /data && tail -n 100 /data/books.log'`
- For live follow: `docker compose logs -f ebookarr`

See the `debug-logs` skill in `.opencode/skills/` for interpreting the log format and diagnosing stuck/failed jobs.
