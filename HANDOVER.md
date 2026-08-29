# Handover: Multi-Key API Authentication for ebookarr

## Background

The ebookarr service (FastAPI, current directory) currently has a **single shared API token** for all programmatic users. It's fetched from Bitwarden at startup and stored in `settings.api_token`. The token is gated to Tailscale CGNAT IPs (`100.64.0.0/10`) via `_is_tailnet_client()` in `auth.py`.

**Problem:** All API users get `owner_user_id="api-token"` — you can't tell who did what. And the owner wants to share the service with friends/family outside the tailnet.

## What exists today

| File | Relevant code |
|---|---|
| `config.py` | `api_token: str`, `api_token_bw_item_id`, `api_token_user_email` — single token |
| `auth.py` | `get_api_token_user()` — validates token + checks `_is_tailnet_client()`. Returns `AuthenticatedUser(user_id="api-token", email=settings.api_token_user_email)` |
| `abuse_protection.py` | `SlidingWindowRateLimiter` (exists, used for per-IP/per-user concurrent limits). `enforce_job_admission()` — limits concurrent jobs, not daily totals |
| `service.py` | `_make_job()` stores `owner_user_id`, `owner_email`, `client_ip` per job. `_log_access()` logs method/path/status/IP per request |
| `bitwarden.py` | `fetch_secrets()` loads `api_token` from Bitwarden item |
| `books.log` | Structured logs with `job=UUID, user=USER_ID` — but all API users show as `api-token` |

## Goal

Replace single shared token with **per-user API keys** where each key has:
- A label (who it belongs to)
- An optional email allowlist (which Kindle addresses it can send to)
- A daily download quota (default: 15/day)
- Removal of the tailnet IP restriction (keys authenticate from anywhere)

## Implementation plan

### Step 1: Create `api_keys.py` — the key store

New file. Responsibilities:
- Define `ApiKeyRecord` dataclass: `key_id: str`, `key_hash: str` (SHA-256 of the raw key), `label: str`, `allowed_emails: list[str]` (empty = any), `daily_quota: int` (default 15), `enabled: bool`
- Define `ApiKeyStore` class:
  - Loads keys from a JSON file (`api_keys.json` in project root, path configurable via `Settings.api_keys_file`)
  - `lookup(raw_key: str) -> ApiKeyRecord | None` — hashes the key, searches store
  - `is_email_allowed(record, kindle_email) -> bool` — checks allowlist (empty list = allow all)
  - `to_user(record) -> AuthenticatedUser` — creates `AuthenticatedUser(user_id=f"api:{record.key_id}", email=record.label, email_verified=True)`
  - `list_keys() -> list[ApiKeyRecord]` — for admin inspection
  - `add_key(...) -> str` — generates a new random key, hashes it, adds to store, returns the raw key (shown once)
  - `revoke_key(key_id)` — removes from store
- The JSON file format:
  ```json
  {
    "keys": {
      "abc123": {
        "label": "Dad",
        "key_hash": "sha256...",
        "allowed_emails": [],
        "daily_quota": 15,
        "enabled": true
      }
    }
  }
  ```
- The raw API key format: `ebk_` prefix + 32 random chars (e.g. `ebk_a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6`)

### Step 2: Update `config.py` — add key store path

Add to `Settings`:
```python
api_keys_file: str = ""  # path to api_keys.json; empty = disabled (fall back to single api_token)
```

No changes to Bitwarden loading — the key store is a local file, not a Bitwarden secret.

### Step 3: Update `auth.py` — multi-key validation

Modify `get_api_token_user()`:
1. If `settings.api_keys_file` is set and the file exists, use `ApiKeyStore` to validate
2. Otherwise, fall back to the current single-token behavior (backward compatible)
3. Remove the `_is_tailnet_client()` gate — API keys authenticate from anywhere
4. The returned `AuthenticatedUser` gets `user_id=f"api:{key_id}"` instead of `"api-token"`

Modify `is_api_token_request()` similarly — check against the key store.

Keep `_is_tailnet_client()` as a utility but stop requiring it for API auth.

### Step 4: Update `abuse_protection.py` — daily download quota

Add a `DailyQuotaTracker` class:
- Track downloads per `user_id` per calendar day (UTC)
- Uses a dict of `user_id -> deque[timestamp]` with 24h window
- `check(user_id, limit) -> (allowed: bool, remaining: int, retry_after_seconds: int)`
- Thread-safe with a Lock (same pattern as `SlidingWindowRateLimiter`)

### Step 5: Update `service.py` — enforce quotas, log key usage

In the download endpoints (`download_from_goodreads`, `download_from_metadata`, `download_by_md5`):
1. After authentication, check if user is API-key user (`user.user_id.startswith("api:")`)
2. If so, look up the key record to get `daily_quota` and `allowed_emails`
3. Enforce email allowlist: if `kindle_email` is set and not in `allowed_emails`, reject with 403
4. Enforce daily quota via the new `DailyQuotaTracker`
5. The `_make_job()` call already stores `owner_user_id` — now it will be `"api:abc123"` instead of `"api-token"` ✅

Add response header `X-Daily-Remaining: N` on successful download creation so clients know their quota.

### Step 6: Create `manage_keys.py` — CLI tool for key management

Simple CLI (run with `uv run manage_keys.py`):
```
uv run manage_keys.py list              # show all keys (masked)
uv run manage_keys.py add --label "Dad" --email dad@kindle.com
uv run manage_keys.py add --label "Yossi"  # no email restriction
uv run manage_keys.py revoke <key_id>
uv run manage_keys.py quota <key_id> [--set 20]
```

On `add`, prints the raw key once and reminds the user to save it.

### Step 7: Update tests

- Unit tests for `ApiKeyStore` (lookup, hash, email allowlist, daily quota)
- Unit test that auth falls back to single token when `api_keys_file` is empty
- Integration test for daily quota enforcement

## Commit strategy

1. **`feat: add multi-key API key store`** — `api_keys.py` + `config.py` change + `manage_keys.py` CLI
2. **`feat: multi-key auth with daily quotas`** — `auth.py` + `abuse_protection.py` + `service.py` changes
3. **`test: multi-key auth and daily quota tests`** — new test files

## Constraints

- Use `uv run` for all commands (PEP 668, no system pip)
- Run tests with `uv run pytest -m "not e2e"` (E2E tests hit live services)
- Python 3.14, FastAPI, pydantic-settings
- The single-token fallback must keep working when `api_keys_file` is not set (backward compat for iOS Shortcuts)
- Don't break the Google OAuth flow — it stays untouched
- Log key IDs (not raw keys) in `books.log`

## Files to modify
- `config.py` — add `api_keys_file`
- `auth.py` — multi-key validation, remove tailnet gate
- `abuse_protection.py` — daily quota tracker
- `service.py` — enforce quotas, log key IDs, email allowlist check

## Files to create
- `api_keys.py` — key store module
- `manage_keys.py` — CLI key management
- `tests/test_api_keys.py` — unit tests
- `tests/test_daily_quota.py` — quota tests

## Files NOT to touch
- `download_flow.py` — download pipeline unchanged
- `download_with_annas_archive.py` — download pipeline unchanged
- `download_with_libgen.py` — download pipeline unchanged
- `bitwarden.py` — single token loading stays for backward compat
- `static/index.html` — web UI unchanged (Google OAuth users unaffected)
