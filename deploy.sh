#!/usr/bin/env bash
set -euo pipefail

SRC=/home/itais/projects/ebooks
DEST=/opt/ebookarr
PORT=${PORT:-19191}

echo "==> Copying updated files to $DEST..."
sudo cp "$SRC/service.py"         "$DEST/service.py"
sudo cp "$SRC/bitwarden.py"       "$DEST/bitwarden.py"
sudo cp "$SRC/ebookarr.service"   "$DEST/ebookarr.service"
sudo cp "$SRC/readme.md"          "$DEST/readme.md"
sudo chown ebookarr:ebookarr \
    "$DEST/service.py" \
    "$DEST/bitwarden.py" \
    "$DEST/ebookarr.service" \
    "$DEST/readme.md"

# The existing .venv was built with uv's managed Python (3.14) stored under
# /home/itais/.local, which the ebookarr user cannot access (home dir is 750).
# Rebuild with the system Python 3.12, which is world-accessible.
echo "==> Downgrading Bitwarden CLI to v2025.11.1 (v2026.1.0 has a regression with API key login)..."
curl -fLo /tmp/bw-linux.zip "https://github.com/bitwarden/clients/releases/download/cli-v2025.11.0/bw-linux-2025.11.0.zip"
unzip -o /tmp/bw-linux.zip -d /tmp bw
sudo install -m 755 /tmp/bw /usr/local/bin/bw
rm /tmp/bw-linux.zip /tmp/bw
bw --version

echo "==> Rebuilding .venv with system Python..."
sudo apt-get install -y python3.12-venv -qq
sudo rm -rf "$DEST/.venv"
sudo -u ebookarr python3 -m venv "$DEST/.venv"
sudo -u ebookarr "$DEST/.venv/bin/pip" install --quiet -r "$DEST/requirements.txt"

echo "==> Installing updated systemd unit..."
sudo cp "$DEST/ebookarr.service" /etc/systemd/system/ebookarr.service
sudo systemctl daemon-reload

echo "==> Restarting service..."
sudo systemctl restart ebookarr

echo "==> Waiting for service to become active..."
for i in $(seq 1 15); do
    STATUS=$(systemctl is-active ebookarr 2>/dev/null || true)
    if [ "$STATUS" = "active" ]; then
        break
    fi
    sleep 2
done

echo "==> systemctl status:"
sudo systemctl status ebookarr --no-pager

echo ""
echo "==> Health check..."
curl -sf "http://localhost:$PORT/health" && echo "" && echo "Service is healthy."
