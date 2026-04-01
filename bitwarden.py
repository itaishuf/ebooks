from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from contextlib import suppress
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config import Settings

from exceptions import BitwardenError

logger = logging.getLogger(__name__)


_BW_TIMEOUT = 5  # seconds


def _run_bw(*args: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    cmd_label = f"bw {' '.join(args[:2])}"
    logger.debug(f"Running: {cmd_label}")
    env = {**os.environ, **(extra_env or {})}
    try:
        result = subprocess.run(
            ["bw", *args],
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=_BW_TIMEOUT,
        )
        logger.debug(f"Completed: {cmd_label}")
        return result
    except FileNotFoundError:
        raise BitwardenError("Bitwarden CLI ('bw') is not installed or not in PATH") from None
    except subprocess.TimeoutExpired as exc:
        raise BitwardenError(f"{cmd_label} timed out after {_BW_TIMEOUT}s") from exc
    except subprocess.CalledProcessError as exc:
        raise BitwardenError(f"{cmd_label} failed: {exc.stderr.strip()}") from exc


def bw_login(settings: Settings) -> None:
    """Log in to Bitwarden using API key credentials from settings.

    The .env file uses BW_CLIENT_ID / BW_CLIENT_SECRET, but the Bitwarden CLI
    expects BW_CLIENTID / BW_CLIENTSECRET (no underscore). We bridge that here.
    Logs out first to clear any stale session that would block API key login.
    """
    with suppress(BitwardenError):
        _run_bw("logout")
    logger.info(f"Configuring Bitwarden server to {settings.bw_server_url}")
    _run_bw("config", "server", settings.bw_server_url)
    _run_bw("login", "--apikey", extra_env={
        "BW_CLIENTID": settings.bw_client_id,
        "BW_CLIENTSECRET": settings.bw_client_secret,
    })


def bw_unlock(settings: Settings) -> str:
    """Unlock the vault using the master password from settings.

    Returns the session key.
    """
    result = _run_bw("unlock", "--passwordenv", "BW_MASTER_PASSWORD",
                     extra_env={"BW_MASTER_PASSWORD": settings.bw_master_password})
    match = re.search(r'BW_SESSION="([^"]+)"', result.stdout)
    if not match:
        raise BitwardenError("Failed to parse session key from 'bw unlock' output")
    return match.group(1)


def bw_get_item_password(session: str, item_id: str) -> str:
    """Fetch the login password for a vault item by its Bitwarden item ID.

    The session key is passed via the BW_SESSION env var to avoid
    leaking it through the process argument list.
    """
    logger.info(f"Fetching password for Bitwarden item id '{item_id}'")
    result = _run_bw("get", "item", item_id, extra_env={"BW_SESSION": session})
    try:
        item = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BitwardenError(f"Invalid JSON from 'bw get item {item_id}'") from exc

    try:
        password = item["login"]["password"]
    except (KeyError, TypeError) as exc:
        raise BitwardenError(f"No login password found in Bitwarden item '{item_id}'") from exc
    logger.info(f"Successfully retrieved password for item id '{item_id}'")
    return password


def bw_lock() -> None:
    _run_bw("lock")


def fetch_secrets(settings: Settings) -> None:
    """Log in to Bitwarden, fetch all application secrets, and lock the vault."""
    secret_mappings = [
        ("gmail_password", settings.gmail_password_bw_item_id),
        ("google_client_secret", settings.google_client_secret_bw_item_id),
        ("session_secret", settings.session_secret_bw_item_id),
    ]
    secrets_needed = [
        (attr, item_id) for attr, item_id in secret_mappings
        if item_id and not getattr(settings, attr)
    ]
    if not secrets_needed:
        logger.info("All secrets already set from environment, skipping Bitwarden")
        return

    logger.info("Fetching secrets from Bitwarden vault")
    bw_login(settings)
    session = bw_unlock(settings)

    try:
        for attr, item_id in secrets_needed:
            value = bw_get_item_password(session, item_id)
            setattr(settings, attr, value)
            logger.info(f"Loaded secret '{attr}' from Bitwarden item id '{item_id}'")
    finally:
        bw_lock()
        logger.info("Bitwarden vault locked")
