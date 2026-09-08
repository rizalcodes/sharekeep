"""Password hashing, token issuing, and cookie handling (Architecture.md §4.0).

Nothing in this module ever logs, returns, or embeds a plaintext password or a raw
token in an error message (Rules.md §3).
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Response

from app.core.config import settings

ACCESS_COOKIE_NAME = "fileshare_access"
REFRESH_COOKIE_NAME = "fileshare_refresh"

# Argon2id with library defaults, which track current guidance.
_hasher = PasswordHasher()

# Upper bound matters: an unbounded input to a memory-hard hash is a DoS vector.
PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_LENGTH = 128

# A dummy hash to verify against when no user matched, so a login attempt for an
# unknown email costs the same time as one for a known email. Without this, response
# timing tells an attacker which addresses are registered.
_DUMMY_HASH = _hasher.hash("dummy-password-for-constant-time-comparison")


class TokenError(Exception):
    """Raised when a token is missing, malformed, expired, or of the wrong type."""


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, hashed: str | None) -> bool:
    """Constant-ish time verification that never reveals whether the user exists."""
    try:
        _hasher.verify(hashed or _DUMMY_HASH, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    return hashed is not None


def needs_rehash(hashed: str) -> bool:
    try:
        return _hasher.check_needs_rehash(hashed)
    except InvalidHashError:
        return True


def create_access_token(user_id: uuid.UUID) -> tuple[str, datetime]:
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=settings.access_token_ttl_seconds)
    payload = {
        "sub": str(user_id),
        "type": "access",
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": secrets.token_urlsafe(16),
    }
    token = jwt.encode(
        payload,
        settings.jwt_secret.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )
    return token, expires_at


def decode_access_token(token: str) -> uuid.UUID:
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
        )
    except jwt.PyJWTError as exc:
        raise TokenError("invalid or expired access token") from exc

    # An attacker must not be able to present a refresh-shaped JWT as an access token.
    if payload.get("type") != "access":
        raise TokenError("wrong token type")
    try:
        return uuid.UUID(str(payload["sub"]))
    except (KeyError, ValueError) as exc:
        raise TokenError("malformed token subject") from exc


def generate_opaque_token() -> tuple[str, str]:
    """Return (raw_token, sha256_hash).

    Used for refresh tokens and invitation tokens. The raw value is shown once and
    never stored; only the hash is persisted, so a database leak yields nothing usable.
    """
    raw = secrets.token_urlsafe(48)
    return raw, hash_token(raw)


def hash_token(raw: str) -> str:
    """SHA-256, not Argon2: these are 48 bytes of CSPRNG output, not human-chosen
    secrets, so there is nothing to brute force and lookup must stay indexable."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _set_cookie(
    response: Response,
    name: str,
    value: str,
    max_age: int,
    path: str,
) -> None:
    response.set_cookie(
        key=name,
        value=value,
        max_age=max_age,
        path=path,
        httponly=True,
        secure=settings.cookie_secure,
        # Lax, not Strict: a Strict cookie is withheld on the cross-site navigation
        # that clicking an emailed invitation link produces (Architecture.md §4.0).
        samesite="lax",
        domain=settings.cookie_domain,
    )


def set_auth_cookies(response: Response, access_token: str, refresh_token: str) -> None:
    _set_cookie(
        response,
        ACCESS_COOKIE_NAME,
        access_token,
        settings.access_token_ttl_seconds,
        path="/",
    )
    # Scoped to the refresh endpoint so the long-lived credential is not attached to
    # every ordinary API call.
    _set_cookie(
        response,
        REFRESH_COOKIE_NAME,
        refresh_token,
        settings.refresh_token_ttl_seconds,
        path=f"{settings.api_v1_prefix}/auth",
    )


def clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(ACCESS_COOKIE_NAME, path="/", domain=settings.cookie_domain)
    response.delete_cookie(
        REFRESH_COOKIE_NAME,
        path=f"{settings.api_v1_prefix}/auth",
        domain=settings.cookie_domain,
    )


def validate_password_strength(password: str) -> str | None:
    """Return an error message, or None if acceptable.

    Length only, no composition rules — NIST-aligned, and composition rules mostly
    produce predictable substitutions rather than stronger passwords.
    """
    if len(password) < PASSWORD_MIN_LENGTH:
        return f"Password must be at least {PASSWORD_MIN_LENGTH} characters."
    if len(password) > PASSWORD_MAX_LENGTH:
        return f"Password must be at most {PASSWORD_MAX_LENGTH} characters."
    return None


TokenType = Literal["access", "refresh"]


def access_token_expiry(token: str) -> datetime | None:
    """Expiry of an access token already in hand, without minting a new one."""
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
        )
    except jwt.PyJWTError:
        return None
    exp = payload.get("exp")
    return datetime.fromtimestamp(exp, tz=UTC) if exp else None
