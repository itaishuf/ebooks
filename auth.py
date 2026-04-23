from __future__ import annotations

import hmac
import ipaddress
import logging
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, status

from abuse_protection import extract_client_ip
from config import settings

logger = logging.getLogger(__name__)

AUTH_SESSION_USER_KEY = "authenticated_user"


@dataclass(frozen=True)
class AuthenticatedUser:
    user_id: str
    email: str
    email_verified: bool


def _auth_error(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
    )


def get_app_base_url() -> str:
    return settings.app_base_url.rstrip("/")


def get_google_redirect_uri() -> str:
    return f"{get_app_base_url()}/auth/google/callback"


def _claim_is_truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() == "true"
    return False


def validate_auth_settings() -> None:
    if not settings.google_client_id:
        raise ValueError("Google auth is not configured: set `google_client_id`.")
    if not settings.google_client_secret:
        raise ValueError(
            "Google auth is not configured: set `google_client_secret` or `google_client_secret_bw_item_id`."
        )
    if not settings.session_secret:
        raise ValueError(
            "Signed sessions are not configured: set `session_secret` or `session_secret_bw_item_id`."
        )

    parsed = urlsplit(get_app_base_url())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("`app_base_url` must be an absolute http(s) URL.")
    if settings.session_same_site == "none" and not settings.session_https_only:
        raise ValueError("`session_same_site=none` requires `session_https_only=true`.")


def build_authenticated_user(session_user: dict) -> AuthenticatedUser:
    user_id = session_user.get("user_id")
    email = session_user.get("email")
    email_verified = session_user.get("email_verified")

    if not isinstance(user_id, str) or not user_id:
        logger.warning("Rejected session without a valid user id")
        raise _auth_error("Invalid session user")
    if not isinstance(email, str) or not email:
        logger.warning(f"Rejected session for user {user_id} without an email")
        raise _auth_error("Invalid session email")
    if not isinstance(email_verified, bool):
        logger.warning(f"Rejected session for user {user_id} without a valid email verification flag")
        raise _auth_error("Invalid session verification state")

    return AuthenticatedUser(user_id=user_id, email=email, email_verified=email_verified)


def build_session_user(claims: dict) -> dict[str, str | bool]:
    user_id = claims.get("sub")
    email = claims.get("email")
    name = claims.get("name")

    if not isinstance(user_id, str) or not user_id:
        raise ValueError("Google account response did not include a valid subject.")
    if not isinstance(email, str) or not email:
        raise ValueError("Google account response did not include an email address.")

    session_user: dict[str, str | bool] = {
        "user_id": user_id,
        "email": email,
        "email_verified": _claim_is_truthy(claims.get("email_verified")),
    }
    if isinstance(name, str) and name:
        session_user["name"] = name
    return session_user


def set_authenticated_session(request: Request, claims: dict) -> AuthenticatedUser:
    session_user = build_session_user(claims)
    request.session.clear()
    request.session[AUTH_SESSION_USER_KEY] = session_user
    return build_authenticated_user(session_user)


def clear_authenticated_session(request: Request) -> None:
    request.session.clear()


def get_session_user(request: Request) -> AuthenticatedUser | None:
    session_user = request.session.get(AUTH_SESSION_USER_KEY)
    if not isinstance(session_user, dict):
        return None

    try:
        return build_authenticated_user(session_user)
    except HTTPException:
        request.session.pop(AUTH_SESSION_USER_KEY, None)
        return None


def get_session_user_payload(request: Request) -> dict | None:
    session_user = request.session.get(AUTH_SESSION_USER_KEY)
    if not isinstance(session_user, dict):
        return None
    return session_user


_TAILNET_NETWORK = ipaddress.ip_network("100.64.0.0/10")


def _is_tailnet_ip(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip) in _TAILNET_NETWORK
    except ValueError:
        return False


def get_api_token_user(request: Request) -> AuthenticatedUser | None:
    if not settings.api_token:
        return None
    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        return None
    token = auth_header[7:]
    if not hmac.compare_digest(token, settings.api_token):
        return None

    client_ip = extract_client_ip(request, settings.trusted_proxy_ips)
    if not _is_tailnet_ip(client_ip):
        logger.warning(f"Rejected API token request from non-tailnet IP {client_ip}")
        return None

    return AuthenticatedUser(
        user_id="api-token",
        email=settings.api_token_user_email,
        email_verified=True,
    )


def is_api_token_request(request: Request) -> bool:
    if not settings.api_token:
        return False
    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        return False
    return hmac.compare_digest(auth_header[7:], settings.api_token)


def get_current_user(request: Request) -> AuthenticatedUser:
    user = get_api_token_user(request) or get_session_user(request)
    if user is None:
        raise _auth_error("Authentication required")
    if settings.require_verified_email and not user.email_verified:
        logger.info(f"Rejected unverified user {user.user_id}")
        raise _auth_error("Email verification required")
    return user
