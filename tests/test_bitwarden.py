import json
import subprocess
from unittest.mock import MagicMock, call, patch

import pytest

from bitwarden import fetch_secrets
from exceptions import BitwardenError


def _make_bw_run(session_key: str = "test-session-key", item_password: str = "gmail-pass"):
    """Return a subprocess.run side_effect that simulates a successful Bitwarden CLI interaction."""
    item_json = json.dumps({"login": {"password": item_password}})

    def _run(cmd, **kwargs):
        if cmd[1] == "logout":
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
        if cmd[1] == "config":
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
        if cmd[1] == "login":
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
        if cmd[1] == "unlock":
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=f'export BW_SESSION="{session_key}"', stderr=""
            )
        if cmd[1] == "get":
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=item_json, stderr="")
        if cmd[1] == "lock":
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
        raise ValueError(f"Unexpected bw command: {cmd}")

    return _run


@patch("bitwarden.subprocess.run")
def test_fetch_secrets_full_startup_flow(mock_run):
    """Full Bitwarden startup flow: login → unlock → fetch Gmail password → lock.

    Verifies the important observable outcomes:
    - Gmail password is populated on the settings object
    - Vault is locked after fetching
    - BW_CLIENT_ID/SECRET env vars are translated to BW_CLIENTID/CLIENTSECRET for the CLI
    """
    mock_run.side_effect = _make_bw_run(session_key="session-abc", item_password="gmail-pass")

    mock_settings = MagicMock()
    mock_settings.bw_server_url = "https://vault.example.com/"
    mock_settings.bw_client_id = "client-id"
    mock_settings.bw_client_secret = "client-secret"
    mock_settings.bw_master_password = "master-pass"
    mock_settings.gmail_password_bw_item_id = "gmail-item-id"
    mock_settings.gmail_password = ""

    fetch_secrets(mock_settings)

    assert mock_settings.gmail_password == "gmail-pass"

    login_call = next(c for c in mock_run.call_args_list if c.args[0][1] == "login")
    env = login_call.kwargs["env"]
    assert env["BW_CLIENTID"] == "client-id"
    assert env["BW_CLIENTSECRET"] == "client-secret"

    lock_call = next((c for c in mock_run.call_args_list if c.args[0][1] == "lock"), None)
    assert lock_call is not None, "Vault should be locked after fetching secrets"


@patch("bitwarden.bw_lock")
@patch("bitwarden.bw_get_item_password", side_effect=BitwardenError("fetch failed"))
@patch("bitwarden.bw_unlock", return_value="session-key")
@patch("bitwarden.bw_login")
def test_fetch_secrets_locks_vault_on_failure(mock_login, mock_unlock, mock_get, mock_lock):
    mock_settings = MagicMock()
    mock_settings.gmail_password_bw_item_id = "gmail-item-id"
    mock_settings.gmail_password = ""

    with pytest.raises(BitwardenError):
        fetch_secrets(mock_settings)

    mock_lock.assert_called_once()


@patch("bitwarden.bw_lock")
@patch("bitwarden.bw_get_item_password")
@patch("bitwarden.bw_unlock", return_value="session-key")
@patch("bitwarden.bw_login")
def test_fetch_secrets_skips_bitwarden_when_runtime_secrets_are_already_loaded(
    mock_login, mock_unlock, mock_get, mock_lock
):
    mock_settings = MagicMock()
    mock_settings.gmail_password_bw_item_id = "gmail-item-id"
    mock_settings.gmail_password = "already-set"

    fetch_secrets(mock_settings)

    mock_login.assert_not_called()
    mock_unlock.assert_not_called()
    mock_get.assert_not_called()
    mock_lock.assert_not_called()


@patch("bitwarden.bw_lock")
@patch("bitwarden.bw_get_item_password")
@patch("bitwarden.bw_unlock", return_value="session-key")
@patch("bitwarden.bw_login")
def test_fetch_secrets_skips_empty_item_ids(mock_login, mock_unlock, mock_get, mock_lock):
    mock_get.return_value = "some-value"

    mock_settings = MagicMock()
    mock_settings.gmail_password_bw_item_id = "gmail-item-id"
    mock_settings.gmail_password = ""

    fetch_secrets(mock_settings)

    mock_get.assert_called_once_with("session-key", "gmail-item-id")
    mock_lock.assert_called_once()
