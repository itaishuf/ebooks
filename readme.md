## Ebookarr - iOS Shortcut + FastAPI eBook Downloader

Receives a Goodreads URL from an iOS Shortcut, finds the book on LibGen / Anna's Archive, downloads it, and emails it to your Kindle.

iOS shortcut available [here](https://www.icloud.com/shortcuts/66149e9ecd5c4ce1b9d4a50abcd03045)

## Prerequisites

- Python 3.11+
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

```bash
# Install Python 3.11+, Firefox, and the Bitwarden CLI
sudo apt install python3 python3-venv firefox-esr
sudo snap install bw

# Create a venv and install dependencies
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Edit .env with your Bitwarden credentials
cp .env.example .env
nano .env

# Run directly
python service.py

# Or install as a systemd service
sudo cp ebookarr.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ebookarr
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
pip install -e ".[dev]"
pytest
ruff check .
```
