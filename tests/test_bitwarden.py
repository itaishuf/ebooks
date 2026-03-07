import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from bitwarden import bw_get_item_password, bw_lock, bw_login, bw_unlock, fetch_secrets
from exceptions import BitwardenError


@patch("bitwarden.subprocess.run")
def test_bw_login_success(mock_run):
    mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    mock_settings = MagicMock()
    mock_settings.bw_server_url = "https://vault.example.com/"
    mock_settings.bw_client_id = "test-id"
    mock_settings.bw_client_secret = "test-secret"
    bw_login(mock_settings)
    assert mock_run.call_count == 3
    logout_call = mock_run.call_args_list[0]
    assert logout_call[0][0] == ["bw", "logout"]
    config_call = mock_run.call_args_list[1]
    assert config_call[0][0] == ["bw", "config", "server", "https://vault.example.com/"]
    login_call = mock_run.call_args_list[2]
    assert login_call[0][0] == ["bw", "login", "--apikey"]
    env_passed = login_call[1]["env"]
    assert env_passed["BW_CLIENTID"] == "test-id"
    assert env_passed["BW_CLIENTSECRET"] == "test-secret"


@patch("bitwarden.subprocess.run")
def test_bw_login_cli_not_found(mock_run):
    mock_settings = MagicMock()
    mock_settings.bw_server_url = "https://vault.example.com/"
    mock_settings.bw_client_id = ""
    mock_settings.bw_client_secret = ""
    mock_run.side_effect = FileNotFoundError()
    with pytest.raises(BitwardenError, match="not installed"):
        bw_login(mock_settings)


@patch("bitwarden.subprocess.run")
def test_bw_login_failure(mock_run):
    mock_settings = MagicMock()
    mock_settings.bw_server_url = "https://vault.example.com/"
    mock_settings.bw_client_id = ""
    mock_settings.bw_client_secret = ""
    mock_run.side_effect = subprocess.CalledProcessError(1, "bw", stderr="bad credentials")
    with pytest.raises(BitwardenError, match="failed"):
        bw_login(mock_settings)


@patch("bitwarden.subprocess.run")
def test_bw_unlock_returns_session(mock_run):
    mock_settings = MagicMock()
    mock_settings.bw_master_password = "master-pass"
    mock_run.return_value = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout='export BW_SESSION="test-session-key-123"',
        stderr="",
    )
    session = bw_unlock(mock_settings)
    assert session == "test-session-key-123"
    env_passed = mock_run.call_args[1]["env"]
    assert env_passed["BW_MASTER_PASSWORD"] == "master-pass"


@patch("bitwarden.subprocess.run")
def test_bw_unlock_no_session_in_output(mock_run):
    mock_settings = MagicMock()
    mock_settings.bw_master_password = "master-pass"
    mock_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="no session here", stderr=""
    )
    with pytest.raises(BitwardenError, match="parse session key"):
        bw_unlock(mock_settings)


@patch("bitwarden.subprocess.run")
def test_bw_get_item_password_success(mock_run):
    item_json = json.dumps({"login": {"username": "user", "password": "s3cret"}})
    mock_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=item_json, stderr=""
    )
    password = bw_get_item_password("session-key", "item-id-123")
    assert password == "s3cret"
    env_passed = mock_run.call_args[1]["env"]
    assert env_passed["BW_SESSION"] == "session-key"
    assert mock_run.call_args[0][0] == ["bw", "get", "item", "item-id-123"]


@patch("bitwarden.subprocess.run")
def test_bw_get_item_password_no_login(mock_run):
    item_json = json.dumps({"secureNote": {"type": 0}})
    mock_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=item_json, stderr=""
    )
    with pytest.raises(BitwardenError, match="No login password"):
        bw_get_item_password("session-key", "item-id-456")


@patch("bitwarden.subprocess.run")
def test_bw_get_item_password_invalid_json(mock_run):
    mock_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="not json", stderr=""
    )
    with pytest.raises(BitwardenError, match="Invalid JSON"):
        bw_get_item_password("session-key", "item-id-789")


@patch("bitwarden.bw_lock")
@patch("bitwarden.bw_get_item_password")
@patch("bitwarden.bw_unlock", return_value="session-key")
@patch("bitwarden.bw_login")
def test_fetch_secrets_uses_bitwarden_with_bootstrap_credentials(
    mock_login, mock_unlock, mock_get, mock_lock
):
    mock_get.side_effect = lambda session, item_id: {
        "gmail-item-id": "gmail-pass",
    }[item_id]

    mock_settings = MagicMock()
    mock_settings.bw_client_id = "client-id"
    mock_settings.bw_client_secret = "client-secret"
    mock_settings.bw_master_password = "master-password"
    mock_settings.gmail_password_bw_item_id = "gmail-item-id"
    mock_settings.gmail_password = ""

    fetch_secrets(mock_settings)

    mock_login.assert_called_once_with(mock_settings)
    mock_unlock.assert_called_once_with(mock_settings)
    assert mock_get.call_count == 1
    assert mock_settings.gmail_password == "gmail-pass"
    mock_lock.assert_called_once()


@patch("bitwarden.bw_lock")
@patch("bitwarden.bw_get_item_password")
@patch("bitwarden.bw_unlock", return_value="session-key")
@patch("bitwarden.bw_login")
def test_fetch_secrets_populates_settings(mock_login, mock_unlock, mock_get, mock_lock):
    mock_get.side_effect = lambda session, item_id: {
        "gmail-item-id": "gmail-pass",
    }[item_id]

    mock_settings = MagicMock()
    mock_settings.gmail_password_bw_item_id = "gmail-item-id"
    mock_settings.gmail_password = ""

    fetch_secrets(mock_settings)

    assert mock_settings.gmail_password == "gmail-pass"
    mock_lock.assert_called_once()


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
