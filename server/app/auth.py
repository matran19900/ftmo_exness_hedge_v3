"""Password hashing (bcrypt) and JWT access-token helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import jwt

ALGORITHM = "HS256"
TOKEN_TYPE = "access"


def hash_password(plain: str) -> str:
    """Return a bcrypt hash of `plain` using a fresh salt at cost 12."""
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """Constant-time check of `plain` against an existing bcrypt hash."""
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        # Malformed hash → treat as failed auth, never raise to caller.
        return False


def create_access_token(subject: str, secret: str, expires_minutes: int) -> str:
    """Sign an HS256 access token carrying `subject` in the `sub` claim."""
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": subject,
        "iat": now,
        "exp": now + timedelta(minutes=expires_minutes),
        "type": TOKEN_TYPE,
    }
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def decode_access_token(token: str, secret: str) -> dict[str, Any]:
    """Decode and validate an access token. Raises pyjwt errors on failure."""
    decoded: dict[str, Any] = jwt.decode(
        token,
        secret,
        algorithms=[ALGORITHM],
        options={"require": ["exp", "sub", "iat"]},
    )
    return decoded
