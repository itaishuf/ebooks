from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

import auth


@pytest.fixture
def rsa_key_pair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


@pytest.fixture(autouse=True)
def auth_settings(monkeypatch):
    monkeypatch.setattr(auth.settings, "supabase_url", "https://example.supabase.co")
    monkeypatch.setattr(auth.settings, "supabase_issuer", "")
    monkeypatch.setattr(auth.settings, "supabase_jwks_url", "")
    monkeypatch.setattr(auth.settings, "supabase_jwt_audience", "authenticated")
    monkeypatch.setattr(auth.settings, "require_verified_email", True)
    if hasattr(auth._get_jwks_client, "cache_clear"):
        auth._get_jwks_client.cache_clear()
    yield
    if hasattr(auth._get_jwks_client, "cache_clear"):
        auth._get_jwks_client.cache_clear()


def _make_token(private_key, **overrides):
    now = datetime.now(timezone.utc)
    payload = {
        "sub": "user-123",
        "email": "reader@example.com",
        "aud": "authenticated",
        "iss": "https://example.supabase.co/auth/v1",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
        "email_verified": True,
    }
    payload.update(overrides)
    return jwt.encode(payload, private_key, algorithm="RS256")


def test_verify_access_token_accepts_valid_token(monkeypatch, rsa_key_pair):
    private_key, public_key = rsa_key_pair
    token = _make_token(private_key)
    monkeypatch.setattr(
        auth,
        "_get_jwks_client",
        lambda: SimpleNamespace(get_signing_key_from_jwt=lambda _token: SimpleNamespace(key=public_key)),
    )

    claims = auth.verify_access_token(token)

    assert claims["sub"] == "user-123"
    assert claims["email"] == "reader@example.com"


def test_verify_access_token_rejects_expired_token(monkeypatch, rsa_key_pair):
    private_key, public_key = rsa_key_pair
    token = _make_token(private_key, exp=int((datetime.now(timezone.utc) - timedelta(minutes=1)).timestamp()))
    monkeypatch.setattr(
        auth,
        "_get_jwks_client",
        lambda: SimpleNamespace(get_signing_key_from_jwt=lambda _token: SimpleNamespace(key=public_key)),
    )

    with pytest.raises(HTTPException, match="Token expired"):
        auth.verify_access_token(token)


def test_verify_access_token_rejects_wrong_issuer(monkeypatch, rsa_key_pair):
    private_key, public_key = rsa_key_pair
    token = _make_token(private_key, iss="https://attacker.example.com/auth/v1")
    monkeypatch.setattr(
        auth,
        "_get_jwks_client",
        lambda: SimpleNamespace(get_signing_key_from_jwt=lambda _token: SimpleNamespace(key=public_key)),
    )

    with pytest.raises(HTTPException, match="Invalid token issuer"):
        auth.verify_access_token(token)


def test_verify_access_token_rejects_wrong_audience(monkeypatch, rsa_key_pair):
    private_key, public_key = rsa_key_pair
    token = _make_token(private_key, aud="public")
    monkeypatch.setattr(
        auth,
        "_get_jwks_client",
        lambda: SimpleNamespace(get_signing_key_from_jwt=lambda _token: SimpleNamespace(key=public_key)),
    )

    with pytest.raises(HTTPException, match="Invalid token audience"):
        auth.verify_access_token(token)


def test_verify_access_token_rejects_invalid_token(monkeypatch, rsa_key_pair):
    _, public_key = rsa_key_pair
    monkeypatch.setattr(
        auth,
        "_get_jwks_client",
        lambda: SimpleNamespace(get_signing_key_from_jwt=lambda _token: SimpleNamespace(key=public_key)),
    )

    with pytest.raises(HTTPException, match="Invalid token"):
        auth.verify_access_token("not-a-jwt")


def test_get_current_user_requires_verified_email(monkeypatch):
    monkeypatch.setattr(
        auth,
        "verify_access_token",
        lambda _token: {
            "sub": "user-123",
            "email": "reader@example.com",
            "email_verified": False,
        },
    )

    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="token")

    with pytest.raises(HTTPException, match="Email verification required"):
        auth.get_current_user(credentials)


def test_get_current_user_requires_authorization_header():
    with pytest.raises(HTTPException, match="Authentication required"):
        auth.get_current_user(None)
