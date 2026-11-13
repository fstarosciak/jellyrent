from datetime import timedelta

import jwt
import pytest

from app.errors import Unauthorized
from app.security import (
    TokenClaims,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)

SECRET = "x" * 40


def test_password_hash_roundtrip() -> None:
    hashed = hash_password("correct horse battery")
    assert hashed.startswith("$argon2id$")
    assert verify_password(hashed, "correct horse battery")
    assert not verify_password(hashed, "wrong")


def test_unknown_user_never_verifies() -> None:
    assert not verify_password(None, "timing-equaliser")


def test_token_roundtrip() -> None:
    claims = TokenClaims(user_id=7, username="alice", is_admin=True)
    token = create_access_token(claims, secret=SECRET, ttl=timedelta(minutes=5))
    assert decode_access_token(token, secret=SECRET) == claims


def test_expired_token_is_rejected() -> None:
    token = create_access_token(
        TokenClaims(1, "a", False), secret=SECRET, ttl=timedelta(seconds=-1)
    )
    with pytest.raises(Unauthorized):
        decode_access_token(token, secret=SECRET)


def test_token_signed_with_other_secret_is_rejected() -> None:
    token = create_access_token(TokenClaims(1, "a", False), secret="y" * 40, ttl=timedelta(minutes=5))
    with pytest.raises(Unauthorized):
        decode_access_token(token, secret=SECRET)


def test_alg_none_is_rejected() -> None:
    token = jwt.encode({"sub": "1", "aud": "jellyrent", "exp": 9999999999}, key=None, algorithm="none")
    with pytest.raises(Unauthorized):
        decode_access_token(token, secret=SECRET)
