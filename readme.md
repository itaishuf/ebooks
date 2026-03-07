## Ebookarr - iOS Shortcut + FastAPI eBook Downloader

Receives a Goodreads URL from an iOS Shortcut, finds the book on LibGen / Anna's Archive, downloads it, and emails it to your Kindle.

iOS shortcut available [here](https://www.icloud.com/shortcuts/66149e9ecd5c4ce1b9d4a50abcd03045)

## Prerequisites

- [uv](https://docs.astral.sh/uv/) (Python package manager)
- Python 3.11+ (installed automatically by uv if missing)
- Firefox (for Selenium)
- [Bitwarden CLI](https://bitwarden.com/help/cli/) (`bw`) installed and in PATH

## Setup

### Shortcut

1. Download the 'Actions' app on your iPhone
2. Change the Host to your server's Tailscale IP address (the endpoint is `/download`)
3. Enter your Kindle email address

### Bitwarden

Application secrets are fetched from a Bitwarden vault at startup. In production, `.env` is only a minimal bootstrap file for the Bitwarden CLI credentials.

1. [Install the Bitwarden CLI](https://bitwarden.com/help/cli/)
2. [Generate a personal API key](https://bitwarden.com/help/personal-api-key/) from your web vault (Account Settings > Security > Keys > API Key)
3. Create the following items in your Bitwarden vault:

| Bitwarden Item Title | Password Field Contains |
|---|---|
| `Ebookarr` | Gmail app password |
| `Ebookarr API Key` | Server API key for endpoint auth |

4. Copy `.env.example` to `.env` and fill in only your Bitwarden bootstrap credentials:

```bash
cp .env.example .env
nano .env
```

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

Do not store `GMAIL_PASSWORD` or `API_KEY` in this file. Those are loaded from Bitwarden at startup.

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
- **Missing Bitwarden items** — make sure the `Ebookarr` and `Ebookarr API Key` items exist in your vault.
- **Missing Python packages** — make sure you ran `uv sync` in `/opt/ebookarr` (step 4).

#### Running without systemd

If you just want to run the server directly for testing:

```bash
cd /opt/ebookarr
uv run service.py
```

### Server (Docker)

```bash
cp .env.example .env
nano .env
docker build -t ebookarr .
docker run -d --name ebookarr --env-file .env -p 19191:19191 ebookarr
```

For Docker, use the same minimal `.env` bootstrap pattern and let Bitwarden provide `GMAIL_PASSWORD` and `API_KEY` at runtime.

### Tailscale

The server binds to `0.0.0.0:19191` by default. Access it via your Tailscale network using the machine's Tailscale IP.

## Configuration

Production configuration uses three layers:

1. `config.py` defaults for normal non-secret settings
2. Optional environment overrides when you need host-specific behavior
3. Bitwarden for runtime application secrets

The production `.env` file is intentionally minimal and only bootstraps Bitwarden:

| Variable | Required | Default | Description |
|---|---|---|---|
| `BW_CLIENT_ID` | Yes | - | Bitwarden personal API key client ID |
| `BW_CLIENT_SECRET` | Yes | - | Bitwarden personal API key client secret |
| `BW_MASTER_PASSWORD` | Yes | - | Bitwarden master password (for vault unlock) |

Defaults in `config.py` cover:

| Setting | Default |
|---|---|
| `GMAIL_ACCOUNT` | `itaishuf@gmail.com` |
| `PORT` | `19191` |
| `HOST` | `0.0.0.0` |
| `DOWNLOAD_DIR` | `/tmp/ebooks` |
| `LOG_PATH` | `./books.log` |
| `TEST_GOODREADS_URL` | empty |
| `TEST_KINDLE_EMAIL` | empty |

Runtime secrets are fetched from Bitwarden at startup and should not be stored on disk:

| Secret | Bitwarden Item |
|---|---|
| `GMAIL_PASSWORD` | `Ebookarr` |
| `API_KEY` | `Ebookarr API Key` |

For local development and E2E tests, you can still override non-secret settings or test-only values with environment variables if needed.

## Development

```bash
uv sync --dev
uv run pytest
uv run ruff check .
```
