from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config import Settings

from exceptions import BitwardenError

logger = logging.getLogger(__name__)


def _run_bw(*args: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, **(extra_env or {})}
    try:
        return subprocess.run(
            ["bw", *args],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError:
        raise BitwardenError("Bitwarden CLI ('bw') is not installed or not in PATH")
    except subprocess.CalledProcessError as exc:
        raise BitwardenError(f"bw {' '.join(args[:2])} failed: {exc.stderr.strip()}") from exc


def bw_login() -> None:
    """Log in to Bitwarden using API key credentials from environment variables.

    Expects BW_CLIENTID and BW_CLIENTSECRET to be set in the environment.
    """
    _run_bw("login", "--apikey")


def bw_unlock() -> str:
    """Unlock the vault using the master password from the BW_MASTER_PASSWORD env var.

    Returns the session key.
    """
    result = _run_bw("unlock", "--passwordenv", "BW_MASTER_PASSWORD")
    match = re.search(r'BW_SESSION="([^"]+)"', result.stdout)
    if not match:
        raise BitwardenError("Failed to parse session key from 'bw unlock' output")
    return match.group(1)


def bw_get_item_password(session: str, item_title: str) -> str:
    """Fetch the login password for a vault item by its title.

    The session key is passed via the BW_SESSION env var to avoid
    leaking it through the process argument list.
    """
    result = _run_bw("get", "item", item_title, extra_env={"BW_SESSION": session})
    try:
        item = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BitwardenError(f"Invalid JSON from 'bw get item {item_title}'") from exc

    try:
        return item["login"]["password"]
    except (KeyError, TypeError) as exc:
        raise BitwardenError(f"No login password found in Bitwarden item '{item_title}'") from exc


def bw_lock() -> None:
    _run_bw("lock")


def fetch_secrets(settings: Settings) -> None:
    """Log in to Bitwarden, fetch all application secrets, and lock the vault."""
    logger.info("Fetching secrets from Bitwarden vault")

    bw_login()
    session = bw_unlock()

    try:
        secret_mappings = [
            ("gmail_password", settings.gmail_password_bw_item_title),
            ("annas_archive_api_key", settings.annas_archive_api_key_bw_item_title),
            ("api_key", settings.api_key_bw_item_title),
        ]
        for attr, item_title in secret_mappings:
            if not item_title:
                continue
            value = bw_get_item_password(session, item_title)
            setattr(settings, attr, value)
            logger.info(f"Loaded secret '{attr}' from Bitwarden item '{item_title}'")
    finally:
        bw_lock()
        logger.info("Bitwarden vault locked")
