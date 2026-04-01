from __future__ import annotations

import pytest
from fastapi import HTTPException, Request

import auth


@pytest.fixture(autouse=True)
def auth_settings(monkeypatch):
    monkeypatch.setattr(auth.settings, "google_client_id", "google-client-id")
    monkeypatch.setattr(auth.settings, "google_client_secret", "google-client-secret")
    monkeypatch.setattr(auth.settings, "session_secret", "session-secret")
    monkeypatch.setattr(auth.settings, "app_base_url", "https://ebookarr.test")
    monkeypatch.setattr(auth.settings, "session_same_site", "lax")
    monkeypatch.setattr(auth.settings, "session_https_only", False)
    monkeypatch.setattr(auth.settings, "require_verified_email", True)


def _make_request(session: dict | None = None) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": [],
            "client": ("testclient", 50000),
            "server": ("testserver", 443),
            "session": session or {},
        }
    )


@pytest.mark.parametrize(
    ("field_name", "value", "expected_message"),
    [
        ("google_client_id", "", "google_client_id"),
        ("google_client_secret", "", "google_client_secret"),
        ("session_secret", "", "session_secret"),
        ("app_base_url", "not-a-url", "app_base_url"),
    ],
)
def test_validate_auth_settings_requires_required_values(monkeypatch, field_name, value, expected_message):
    monkeypatch.setattr(auth.settings, field_name, value)

    with pytest.raises(ValueError, match=expected_message):
        auth.validate_auth_settings()


def test_validate_auth_settings_rejects_insecure_none_same_site(monkeypatch):
    monkeypatch.setattr(auth.settings, "session_same_site", "none")
    monkeypatch.setattr(auth.settings, "session_https_only", False)

    with pytest.raises(ValueError, match="session_https_only"):
        auth.validate_auth_settings()


def test_build_session_user_keeps_minimal_google_identity():
    session_user = auth.build_session_user(
        {
            "sub": "user-123",
            "email": "reader@example.com",
            "email_verified": "true",
            "name": "Reader Example",
            "picture": "https://example.com/avatar.png",
        }
    )

    assert session_user == {
        "user_id": "user-123",
        "email": "reader@example.com",
        "email_verified": True,
        "name": "Reader Example",
    }


def test_set_authenticated_session_replaces_previous_session_state():
    request = _make_request({"stale": "value"})

    user = auth.set_authenticated_session(
        request,
        {
            "sub": "user-123",
            "email": "reader@example.com",
            "email_verified": True,
            "name": "Reader Example",
        },
    )

    assert user == auth.AuthenticatedUser(
        user_id="user-123",
        email="reader@example.com",
        email_verified=True,
    )
    assert request.session == {
        auth.AUTH_SESSION_USER_KEY: {
            "user_id": "user-123",
            "email": "reader@example.com",
            "email_verified": True,
            "name": "Reader Example",
        }
    }


def test_get_current_user_accepts_verified_session():
    request = _make_request(
        {
            auth.AUTH_SESSION_USER_KEY: {
                "user_id": "user-456",
                "email": "reader@gmail.com",
                "email_verified": True,
                "name": "Test User",
            }
        }
    )

    user = auth.get_current_user(request)

    assert user.email == "reader@gmail.com"
    assert user.email_verified is True


def test_get_current_user_requires_verified_email():
    request = _make_request(
        {
            auth.AUTH_SESSION_USER_KEY: {
                "user_id": "user-123",
                "email": "reader@example.com",
                "email_verified": False,
            }
        }
    )

    with pytest.raises(HTTPException, match="Email verification required"):
        auth.get_current_user(request)


def test_get_current_user_requires_session_cookie():
    with pytest.raises(HTTPException, match="Authentication required"):
        auth.get_current_user(_make_request())


def test_get_session_user_clears_invalid_session_payload():
    request = _make_request(
        {
            auth.AUTH_SESSION_USER_KEY: {
                "user_id": "user-123",
                "email": "reader@example.com",
            }
        }
    )

    assert auth.get_session_user(request) is None
    assert auth.AUTH_SESSION_USER_KEY not in request.session
