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
    assert payload["sub"] == "user-1"


def test_wrong_audience_rejected(helper: JWTHelper) -> None:
    """A token for another audience is rejected."""
    with pytest.raises(jwt.InvalidTokenError):
        helper.decode_token(_foreign_token(iss=JWT_ISSUER, aud="someone-else"))


def test_wrong_issuer_rejected(helper: JWTHelper) -> None:
    """A token from another issuer is rejected."""
    with pytest.raises(jwt.InvalidTokenError):
        helper.decode_token(_foreign_token(iss="someone-else", aud=JWT_AUDIENCE))


def test_token_without_issuer_and_audience_accepted(helper: JWTHelper) -> None:
    """A token without iss/aud claims still decodes."""
    payload = helper.decode_token(_foreign_token())
    assert payload["jti"] == "token-1"


def test_list_audience_containing_ours_accepted(helper: JWTHelper) -> None:
    """A list aud claim is accepted when it contains our audience."""
    payload = helper.decode_token(_foreign_token(aud=["other", JWT_AUDIENCE]))
    assert JWT_AUDIENCE in payload["aud"]


def test_list_audience_without_ours_rejected(helper: JWTHelper) -> None:
    """A list aud claim without our audience is rejected."""
    with pytest.raises(jwt.InvalidAudienceError):
        helper.decode_token(_foreign_token(aud=["other"]))
