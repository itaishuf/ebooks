## Ebookarr - Google Sign-In + FastAPI eBook Downloader

A FastAPI + Alpine.js browser app that signs readers in with Google, searches for ebooks, downloads them from LibGen / Anna's Archive, and emails them to Kindle.

Current auth is browser-only. Open the app in a browser, sign in with Google, and use the protected search/download flow there. Direct API clients such as iOS Shortcuts or scripts are not supported by the v1 cookie-session flow.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) (Python package manager)
- Python 3.11+ (installed automatically by uv if missing)
- Firefox (for Selenium)
- [Bitwarden CLI](https://bitwarden.com/help/cli/) (`bw`) installed and in PATH

## Setup

### Browser Access

1. Open your Ebookarr origin in a browser (for example `http://localhost:19191` locally or `https://itai-books.<tailnet>.ts.net` through Funnel).
2. Sign in with Google from the left sidebar.
3. Enter your Kindle email address and use the search / download flow from the web app.

### Bitwarden

Application secrets are fetched from a Bitwarden vault at startup. In production, `.env` stays small and only needs Bitwarden bootstrap credentials plus the non-secret auth settings that define your public app origin and Google client ID.

1. [Install the Bitwarden CLI](https://bitwarden.com/help/cli/)
2. [Generate a personal API key](https://bitwarden.com/help/personal-api-key/) from your web vault (Account Settings > Security > Keys > API Key)
3. Create the following items in your Bitwarden vault:

| Bitwarden Item | Password Field Contains |
|---|---|
| `Ebookarr` | Gmail app password |
| `Ebookarr Google OAuth` | Google OAuth client secret |
| `Ebookarr Session Secret` | Random session signing secret |

4. Copy `.env.example` to `.env` and fill in your Bitwarden bootstrap credentials plus the non-secret auth settings for this deployment:

```bash
cp .env.example .env
nano .env
```

### Google OAuth

Sign-in now uses server-owned Google OAuth plus a signed cookie session. Before deploying you must configure Google Cloud and point the app at the correct public origin.

#### 1. Create a Google OAuth client

1. Go to [Google Cloud Console → Credentials](https://console.cloud.google.com/apis/credentials) and create an **OAuth 2.0 Client ID** (Application type: **Web application**).
2. Under **Authorized redirect URIs**, add the app callback URL for each origin you will use:
   - `https://itai-books.<tailnet>.ts.net/auth/google/callback`
   - `http://localhost:19191/auth/google/callback`
3. Save. Google gives you a **Client ID** and **Client Secret**.

#### 2. Configure Ebookarr

1. Set `GOOGLE_CLIENT_ID` to the client ID from step 1.
2. Set `APP_BASE_URL` to the exact browser origin for this deployment:
   - `https://itai-books.<tailnet>.ts.net` for Funnel
   - `http://localhost:19191` for local development
3. Store the Google client secret in Bitwarden and set `GOOGLE_CLIENT_SECRET_BW_ITEM_ID`.
4. Store a random session-signing secret in Bitwarden and set `SESSION_SECRET_BW_ITEM_ID`.
5. For HTTPS deployments such as Funnel, set `SESSION_HTTPS_ONLY=true`.

### Gmail Configuration

In your Google account, [create an App Password](https://myaccount.google.com/apppasswords) and store it as the password of the `Ebookarr` item in your Bitwarden vault.

### Server (systemd)

#### 1. Install system dependencies

```bash
sudo apt update
sudo apt install firefox-esr unzip
```

Install [uv](https://docs.astral.sh/uv/getting-started/installation/):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Install the [Bitwarden CLI](https://bitwarden.com/help/cli/) binary:

```bash
curl -fLo /tmp/bw-linux.zip "https://vault.bitwarden.com/download/?app=cli&platform=linux"
unzip -o /tmp/bw-linux.zip -d /tmp
sudo install -m 755 /tmp/bw /usr/local/bin/bw
rm /tmp/bw-linux.zip /tmp/bw
```

Verify everything is installed:

```bash
which bw          # should print /usr/local/bin/bw
bw --version
uv --version
```

#### 2. Create a dedicated service user

The included `ebookarr.service` unit runs as a dedicated `ebookarr` user. Create it with no login shell and no home directory:

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin ebookarr
```

#### 3. Deploy the application to `/opt/ebookarr`

```bash
sudo mkdir -p /opt/ebookarr
sudo cp -r . /opt/ebookarr/
sudo chown -R ebookarr:ebookarr /opt/ebookarr
```

#### 4. Install dependencies

```bash
sudo -u ebookarr uv sync --project /opt/ebookarr
```

This creates a `.venv` inside `/opt/ebookarr` and installs all dependencies from `pyproject.toml`.

#### 5. Configure the bootstrap `.env` file

```bash
sudo cp /opt/ebookarr/.env.example /opt/ebookarr/.env
sudo nano /opt/ebookarr/.env          # add only the BW_* values
sudo chown ebookarr:ebookarr /opt/ebookarr/.env
sudo chmod 600 /opt/ebookarr/.env     # contains the Bitwarden master password
```

Keep this file minimal in production:

```dotenv
BW_CLIENT_ID=...
BW_CLIENT_SECRET=...
BW_MASTER_PASSWORD=...
```

Do not store `GMAIL_PASSWORD` in this file. It is loaded from Bitwarden at startup using the configured item ID in `config.py`.

#### 6. Make sure the Bitwarden CLI is in the service's `PATH`

The service unit sets `PATH` explicitly so systemd can find the `bw` binary. Open `ebookarr.service` and verify the `Environment="PATH=..."` line includes the directory from `which bw` (step 1). The default already includes `/usr/local/bin`, which is where the install command above places `bw`.

```ini
Environment="PATH=/usr/local/bin:/usr/bin:/bin"
```

#### 7. Install and start the service

```bash
sudo cp /opt/ebookarr/ebookarr.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ebookarr
```

#### 8. Verify it is running

```bash
sudo systemctl status ebookarr
sudo journalctl -u ebookarr -f        # follow live logs
```

#### 9. Health check

```bash
curl http://localhost:19191/health     # → {"status":"ok"}
```

No API key required. Use this URL in your monitoring setup to confirm the service is up.

If the service fails to start, common causes are:

- **Bitwarden CLI not found** — check the `PATH` in the service unit (step 6).
- **Wrong file permissions** — the `ebookarr` user must own `/opt/ebookarr` and be able to read the bootstrap `.env`.
- **Bitwarden login failure** — run `sudo -u ebookarr env $(cat /opt/ebookarr/.env | xargs) bw login --apikey` to test credentials interactively.
- **Wrong Bitwarden item IDs** — verify the configured item IDs in `config.py` still point at the right vault items.
- **Missing Python packages** — make sure you ran `uv sync` in `/opt/ebookarr` (step 4).

#### Running without systemd

If you just want to run the server directly for testing:

```bash
cd /opt/ebookarr
uv run service.py
```

### Server (Docker Compose + Tailscale)

This deployment adds a dedicated Tailscale sidecar node named `itai-books`. That gives Ebookarr its own tailnet identity, so Funnel uses the container node name instead of your host machine name.

#### 1. Create the app bootstrap env file

```bash
cp .env.example .env
nano .env
```

Keep this file minimal and only set:

```dotenv
BW_CLIENT_ID=...
BW_CLIENT_SECRET=...
BW_MASTER_PASSWORD=...
```

#### 2. Create the Tailscale auth env file

```bash
cp .tailscale.env.example .tailscale.env
nano .tailscale.env
```

Set:

```dotenv
TS_AUTHKEY=tskey-auth-...
```

This file is kept separate from `.env` so the Tailscale container receives only Tailscale credentials, while the app container receives only the Bitwarden bootstrap settings it actually needs.

Use a reusable auth key if you want the `tailscale-state` volume to survive container recreation cleanly.

#### 3. Start the stack

```bash
docker compose up -d --build
```

The Compose stack does the following:

- builds Ebookarr from the included `Dockerfile`
- installs Firefox and Bitwarden CLI inside the app container
- runs the app with plain Python instead of `uv` at runtime
- stores downloads and logs in the `ebookarr-data` volume
- stores Tailscale node state in the `tailscale-state` volume
- joins your tailnet as a separate node named `itai-books`
- enables Funnel automatically against the app port defined in `docker-compose.yml`

#### 4. Configure the app port in Compose

The deployment port lives in `docker-compose.yml`, not in the image. Both the app and the Funnel bootstrap script read the same Compose-defined value:

```yaml
x-ebookarr-port: &ebookarr_port "19191"
```

If you want a different internal app port, change that anchor in `docker-compose.yml` and redeploy.

#### 5. Verify Funnel and the node name

To inspect the node and confirm its MagicDNS name:

```bash
docker compose exec tailscale tailscale status --self
docker compose exec tailscale tailscale funnel status
```

The resulting public HTTPS URL will be the `itai-books` node on your tailnet, typically:

```text
https://itai-books.<tailnet>.ts.net
```

That keeps the public URL tied to the `itai-books` node instead of your host's Tailscale hostname.

#### 6. Logs and health endpoint

```bash
docker compose logs -f ebookarr
docker compose exec ebookarr sh -c 'curl -fsS "http://127.0.0.1:${PORT}/health"'
```

Compose overrides these app paths:

- `DOWNLOAD_DIR=/data/downloads`
- `LOG_PATH=/data/books.log`

Do not store `GMAIL_PASSWORD`, `GOOGLE_CLIENT_SECRET`, or `SESSION_SECRET` in `.env`. The app fetches those runtime secrets from Bitwarden at startup.

## Configuration

Production configuration uses three layers:

1. `config.py` defaults for normal non-secret settings
2. Optional environment overrides when you need host-specific behavior
3. Bitwarden for runtime application secrets

The production `.env` file is intentionally small and typically contains Bitwarden bootstrap credentials plus deployment-specific non-secret auth settings:

| Variable | Required | Default | Description |
|---|---|---|---|
| `BW_CLIENT_ID` | Yes | - | Bitwarden personal API key client ID |
| `BW_CLIENT_SECRET` | Yes | - | Bitwarden personal API key client secret |
| `BW_MASTER_PASSWORD` | Yes | - | Bitwarden master password (for vault unlock) |
| `APP_BASE_URL` | Yes | `http://localhost:19191` | Public browser origin used for OAuth callback and same-origin checks |
| `GOOGLE_CLIENT_ID` | Yes | - | Google OAuth client ID |
| `GOOGLE_CLIENT_SECRET_BW_ITEM_ID` | Yes unless `GOOGLE_CLIENT_SECRET` is set directly | - | Bitwarden item ID containing the Google OAuth client secret |
| `SESSION_SECRET_BW_ITEM_ID` | Yes unless `SESSION_SECRET` is set directly | - | Bitwarden item ID containing the signed-session secret |
| `SESSION_HTTPS_ONLY` | No | `false` | Set to `true` for HTTPS deployments such as Tailscale Funnel |

Defaults in `config.py` cover:

| Setting | Default |
|---|---|
| `GMAIL_ACCOUNT` | `itaishuf@gmail.com` |
| `PORT` | `19191` |
| `HOST` | `0.0.0.0` |
| `DOWNLOAD_DIR` | `/tmp/ebooks` |
| `LOG_PATH` | `./books.log` |
| `SESSION_COOKIE_NAME` | `ebookarr_session` |
| `SESSION_SAME_SITE` | `lax` |
| `SESSION_MAX_AGE_SECONDS` | `604800` |
| `TEST_GOODREADS_URL` | empty |
| `TEST_KINDLE_EMAIL` | empty |

Runtime secrets are fetched from Bitwarden at startup and should not be stored on disk:

| Secret | Config Setting | Current Vault Item |
|---|---|---|
| `GMAIL_PASSWORD` | `gmail_password_bw_item_id` | `Ebookarr` |
| `GOOGLE_CLIENT_SECRET` | `google_client_secret_bw_item_id` | `Ebookarr Google OAuth` |
| `SESSION_SECRET` | `session_secret_bw_item_id` | `Ebookarr Session Secret` |

For local development and E2E tests, you can still override non-secret settings or test-only values with environment variables if needed. The browser flow remains same-origin, so local auth expects `APP_BASE_URL=http://localhost:19191`.

## Development

```bash
uv sync --dev
uv run pytest
uv run ruff check .
```
