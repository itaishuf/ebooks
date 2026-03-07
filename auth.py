from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from config import settings

logger = logging.getLogger(__name__)

_bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class AuthenticatedUser:
    user_id: str
    email: str
    email_verified: bool


def _auth_error(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _normalized_supabase_base_url() -> str:
    return settings.supabase_url.rstrip("/")


def get_supabase_issuer() -> str:
    if settings.supabase_issuer:
        return settings.supabase_issuer.rstrip("/")
    base_url = _normalized_supabase_base_url()
    if not base_url:
        return ""
    return f"{base_url}/auth/v1"


def get_supabase_jwks_url() -> str:
    if settings.supabase_jwks_url:
        return settings.supabase_jwks_url
    base_url = _normalized_supabase_base_url()
    if not base_url:
        return ""
    return f"{base_url}/auth/v1/.well-known/jwks.json"


def validate_auth_settings() -> None:
    if not get_supabase_issuer():
        raise ValueError("Supabase auth is not configured: set `supabase_url` or `supabase_issuer`.")
    if not get_supabase_jwks_url():
        raise ValueError("Supabase auth is not configured: set `supabase_url` or `supabase_jwks_url`.")
    if not settings.supabase_jwt_audience:
        raise ValueError("Supabase auth is not configured: set `supabase_jwt_audience`.")


@lru_cache(maxsize=1)
def _get_jwks_client() -> jwt.PyJWKClient:
    validate_auth_settings()
    return jwt.PyJWKClient(get_supabase_jwks_url())


def _claim_is_truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() == "true"
    return False


def _is_email_verified(claims: dict) -> bool:
    if _claim_is_truthy(claims.get("email_verified")):
        return True
    if claims.get("email_confirmed_at") or claims.get("confirmed_at"):
        return True
    # Supabase puts email_verified inside user_metadata for OAuth providers (e.g. Google)
    user_metadata = claims.get("user_metadata") or {}
    if isinstance(user_metadata, dict) and _claim_is_truthy(user_metadata.get("email_verified")):
        return True
    return False


def verify_access_token(token: str) -> dict:
    try:
        signing_key = _get_jwks_client().get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256", "ES256"],
            audience=settings.supabase_jwt_audience,
            issuer=get_supabase_issuer(),
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        logger.info("Rejected expired bearer token")
        raise _auth_error("Token expired") from exc
    except jwt.InvalidIssuerError as exc:
        logger.warning("Rejected bearer token with invalid issuer")
        raise _auth_error("Invalid token issuer") from exc
    except jwt.InvalidAudienceError as exc:
        logger.warning("Rejected bearer token with invalid audience")
        raise _auth_error("Invalid token audience") from exc
    except jwt.PyJWKClientError as exc:
        logger.error(f"Failed to fetch signing key for bearer token: {exc}")
        raise _auth_error("Authentication service unavailable") from exc
    except jwt.InvalidTokenError as exc:
        logger.warning(f"Rejected invalid bearer token: {exc.__class__.__name__}")
        raise _auth_error("Invalid token") from exc


def build_authenticated_user(claims: dict) -> AuthenticatedUser:
    user_id = claims.get("sub")
    email = claims.get("email")

    if not isinstance(user_id, str) or not user_id:
        logger.warning("Rejected bearer token without a valid subject claim")
        raise _auth_error("Invalid token subject")
    if not isinstance(email, str) or not email:
        logger.warning(f"Rejected bearer token for user {user_id} without an email claim")
        raise _auth_error("Invalid token email")

    return AuthenticatedUser(
        user_id=user_id,
        email=email,
        email_verified=_is_email_verified(claims),
    )


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> AuthenticatedUser:
    if credentials is None:
        raise _auth_error("Authentication required")
    if credentials.scheme.lower() != "bearer":
        raise _auth_error("Bearer authentication required")

    claims = verify_access_token(credentials.credentials)
    user = build_authenticated_user(claims)
    if settings.require_verified_email and not user.email_verified:
        logger.info(f"Rejected unverified user {user.user_id}")
        raise _auth_error("Email verification required")
    return user
