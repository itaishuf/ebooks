import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from bitwarden import bw_get_item_password, bw_lock, bw_login, bw_unlock, fetch_secrets
from exceptions import BitwardenError


@patch("bitwarden.subprocess.run")
def test_bw_login_success(mock_run):
    mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    bw_login()
    mock_run.assert_called_once()
    args = mock_run.call_args
    assert args[0][0] == ["bw", "login", "--apikey"]


@patch("bitwarden.subprocess.run")
def test_bw_login_cli_not_found(mock_run):
    mock_run.side_effect = FileNotFoundError()
    with pytest.raises(BitwardenError, match="not installed"):
        bw_login()


@patch("bitwarden.subprocess.run")
def test_bw_login_failure(mock_run):
    mock_run.side_effect = subprocess.CalledProcessError(1, "bw", stderr="bad credentials")
    with pytest.raises(BitwardenError, match="failed"):
        bw_login()


@patch("bitwarden.subprocess.run")
def test_bw_unlock_returns_session(mock_run):
    mock_run.return_value = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout='export BW_SESSION="test-session-key-123"',
        stderr="",
    )
    session = bw_unlock()
    assert session == "test-session-key-123"


@patch("bitwarden.subprocess.run")
def test_bw_unlock_no_session_in_output(mock_run):
    mock_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="no session here", stderr=""
    )
    with pytest.raises(BitwardenError, match="parse session key"):
        bw_unlock()


@patch("bitwarden.subprocess.run")
def test_bw_get_item_password_success(mock_run):
    item_json = json.dumps({"login": {"username": "user", "password": "s3cret"}})
    mock_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=item_json, stderr=""
    )
    password = bw_get_item_password("session-key", "My Item")
    assert password == "s3cret"
    env_passed = mock_run.call_args[1]["env"]
    assert env_passed["BW_SESSION"] == "session-key"


@patch("bitwarden.subprocess.run")
def test_bw_get_item_password_no_login(mock_run):
    item_json = json.dumps({"secureNote": {"type": 0}})
    mock_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=item_json, stderr=""
    )
    with pytest.raises(BitwardenError, match="No login password"):
        bw_get_item_password("session-key", "Secure Note")


@patch("bitwarden.subprocess.run")
def test_bw_get_item_password_invalid_json(mock_run):
    mock_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="not json", stderr=""
    )
    with pytest.raises(BitwardenError, match="Invalid JSON"):
        bw_get_item_password("session-key", "Bad Item")


@patch("bitwarden.bw_lock")
@patch("bitwarden.bw_get_item_password")
@patch("bitwarden.bw_unlock", return_value="session-key")
@patch("bitwarden.bw_login")
def test_fetch_secrets_populates_settings(mock_login, mock_unlock, mock_get, mock_lock):
    mock_get.side_effect = lambda session, title: {
        "Ebookarr": "gmail-pass",
        "annas-archive.org (8pz82Gt)": "annas-key",
        "Ebookarr API Key": "api-key-123",
    }[title]

    settings = MagicMock()
    settings.gmail_password_bw_item_title = "Ebookarr"
    settings.annas_archive_api_key_bw_item_title = "annas-archive.org (8pz82Gt)"
    settings.api_key_bw_item_title = "Ebookarr API Key"

    fetch_secrets(settings)

    assert settings.gmail_password == "gmail-pass"
    assert settings.annas_archive_api_key == "annas-key"
    assert settings.api_key == "api-key-123"
    mock_lock.assert_called_once()


@patch("bitwarden.bw_lock")
@patch("bitwarden.bw_get_item_password", side_effect=BitwardenError("fetch failed"))
@patch("bitwarden.bw_unlock", return_value="session-key")
@patch("bitwarden.bw_login")
def test_fetch_secrets_locks_vault_on_failure(mock_login, mock_unlock, mock_get, mock_lock):
    settings = MagicMock()
    settings.gmail_password_bw_item_title = "Ebookarr"
    settings.annas_archive_api_key_bw_item_title = ""
    settings.api_key_bw_item_title = ""

    with pytest.raises(BitwardenError):
        fetch_secrets(settings)

    mock_lock.assert_called_once()


@patch("bitwarden.bw_lock")
@patch("bitwarden.bw_get_item_password")
@patch("bitwarden.bw_unlock", return_value="session-key")
@patch("bitwarden.bw_login")
def test_fetch_secrets_skips_empty_titles(mock_login, mock_unlock, mock_get, mock_lock):
    mock_get.return_value = "some-value"

    settings = MagicMock()
    settings.gmail_password_bw_item_title = "Ebookarr"
    settings.annas_archive_api_key_bw_item_title = ""
    settings.api_key_bw_item_title = ""

    fetch_secrets(settings)

    mock_get.assert_called_once_with("session-key", "Ebookarr")
    mock_lock.assert_called_once()
