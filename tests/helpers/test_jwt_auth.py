"""Tests for the JWT auth helper."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import jwt
import pytest
from music_assistant_models.auth import User

from music_assistant.helpers.datetime import utc
from music_assistant.helpers.jwt_auth import JWT_AUDIENCE, JWT_ISSUER, JWTHelper

SECRET = "test-secret-key-that-is-long-enough-for-hs256"


@pytest.fixture
def helper() -> JWTHelper:
    """Return a JWTHelper with a fixed secret."""
    return JWTHelper(SECRET)


def _foreign_token(**claims: Any) -> str:
    """Sign a token with our secret but arbitrary claims."""
    payload = {
        "sub": "user-1",
        "jti": "token-1",
        "exp": int((utc() + timedelta(hours=1)).timestamp()),
        **claims,
    }
    return jwt.encode(payload, SECRET, algorithm="HS256")


def test_encoded_token_carries_issuer_and_audience(helper: JWTHelper) -> None:
    """A freshly minted token has our iss/aud claims and decodes."""
    user = User(user_id="user-1", username="alice", role="admin")
    token = helper.encode_token(user, "token-1", "test", utc() + timedelta(hours=1))
    payload = helper.decode_token(token)
    assert payload["iss"] == JWT_ISSUER
    assert payload["aud"] == JWT_AUDIENCE


@pytest.mark.parametrize(
    "claims",
    [
        {"iss": JWT_ISSUER, "aud": "someone-else"},
        {"iss": "someone-else", "aud": JWT_AUDIENCE},
        {"aud": JWT_AUDIENCE},
        {"iss": JWT_ISSUER},
    ],
    ids=["wrong-audience", "wrong-issuer", "audience-only", "issuer-only"],
)
def test_token_not_issued_for_us_is_rejected(helper: JWTHelper, claims: dict[str, str]) -> None:
    """A token whose issuer or audience is missing or foreign is rejected."""
    with pytest.raises(jwt.InvalidTokenError):
        helper.decode_token(_foreign_token(**claims))


def test_token_without_issuer_and_audience_accepted(helper: JWTHelper) -> None:
    """A token issued before the claims existed still decodes."""
    assert helper.decode_token(_foreign_token())["jti"] == "token-1"
