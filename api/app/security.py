"""Password hashing (Argon2id) and access tokens (JWT, HS256)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.errors import Unauthorized

_hasher = PasswordHasher()
# Verified when the username does not exist, so that response time does not
# reveal which usernames are registered.
_DUMMY_HASH = _hasher.hash("timing-equaliser")

JWT_ALGORITHM = "HS256"
JWT_AUDIENCE = "jellyrent"


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


@dataclass(frozen=True, slots=True)
class TokenClaims:
    user_id: int
    username: str
    is_admin: bool


def create_access_token(claims: TokenClaims, *, secret: str, ttl: timedelta) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": str(claims.user_id),
        "name": claims.username,
        "adm": claims.is_admin,
        "aud": JWT_AUDIENCE,
        "iat": now,
        "exp": now + ttl,
    }
    return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str, *, secret: str) -> TokenClaims:
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[JWT_ALGORITHM],
            audience=JWT_AUDIENCE,
            options={"require": ["sub", "exp", "aud"]},
        )
        return TokenClaims(
            user_id=int(payload["sub"]),
            username=str(payload.get("name", "")),
            is_admin=bool(payload.get("adm", False)),
        )
    except (jwt.PyJWTError, ValueError) as exc:
        raise Unauthorized("invalid or expired token") from exc
