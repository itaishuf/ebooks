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
2. Change the Host to your server's Tailscale IP address
3. Enter your Kindle email address

### Bitwarden

All application secrets are fetched from a Bitwarden vault at startup. No plaintext secrets are stored in `.env`.

1. [Install the Bitwarden CLI](https://bitwarden.com/help/cli/)
2. [Generate a personal API key](https://bitwarden.com/help/personal-api-key/) from your web vault (Account Settings > Security > Keys > API Key)
3. Create the following items in your Bitwarden vault:

| Bitwarden Item Title | Password Field Contains |
|---|---|
| `Ebookarr` | Gmail app password |
| `annas-archive.org (8pz82Gt)` | Anna's Archive API key |
| `Ebookarr API Key` | Server API key for endpoint auth |

4. Copy `.env.example` to `.env` and fill in your Bitwarden credentials:

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

#### 5. Configure the `.env` file

```bash
sudo cp /opt/ebookarr/.env.example /opt/ebookarr/.env
sudo nano /opt/ebookarr/.env          # fill in your values
sudo chown ebookarr:ebookarr /opt/ebookarr/.env
sudo chmod 600 /opt/ebookarr/.env     # secrets — readable only by ebookarr
```

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

If the service fails to start, common causes are:

- **Bitwarden CLI not found** — check the `PATH` in the service unit (step 6).
- **Wrong file permissions** — the `ebookarr` user must own `/opt/ebookarr` and be able to read `.env`.
- **Bitwarden login failure** — run `sudo -u ebookarr env $(cat /opt/ebookarr/.env | xargs) bw login --apikey` to test credentials interactively.
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

### Tailscale

The server binds to `0.0.0.0:19191` by default. Access it via your Tailscale network using the machine's Tailscale IP.

## Configuration

All configuration is managed via a `.env` file (see `.env.example`):

| Variable | Required | Default | Description |
|---|---|---|---|
| `BW_CLIENT_ID` | Yes | - | Bitwarden personal API key client ID |
| `BW_CLIENT_SECRET` | Yes | - | Bitwarden personal API key client secret |
| `BW_MASTER_PASSWORD` | Yes | - | Bitwarden master password (for vault unlock) |
| `GMAIL_ACCOUNT` | Yes | - | Gmail address for sending books |
| `PORT` | No | 19191 | Server port |
| `HOST` | No | 0.0.0.0 | Bind address |
| `DOWNLOAD_DIR` | No | /tmp/ebooks | Selenium download directory |
| `LOG_PATH` | No | ./books.log | Log file path |

Secrets (`GMAIL_PASSWORD`, `ANNAS_ARCHIVE_API_KEY`, `API_KEY`) are fetched from Bitwarden at startup and are never stored on disk.

## Development

```bash
uv sync --dev
uv run pytest
uv run ruff check .
```
