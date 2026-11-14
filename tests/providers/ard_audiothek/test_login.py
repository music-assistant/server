"""Test the ARD Audiothek account login."""

from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.errors import LoginFailed

from music_assistant.providers.ard_audiothek import _login


def _session(*payloads: dict[str, Any]) -> Mock:
    """Return a client session whose posts answer with the given JSON payloads in order."""
    responses = [Mock(json=AsyncMock(return_value=payload)) for payload in payloads]
    return Mock(post=AsyncMock(side_effect=responses))


async def test_login_uses_sign_in_response() -> None:
    """Take token, user id and display name from the sign-in response alone."""
    session = _session(
        {
            "idToken": "token",
            "localId": "uid",
            "displayName": "Jane",
            "email": "jane@example.com",
        }
    )

    assert await _login(session, "jane@example.com", "secret") == ("token", "uid", "Jane")
    assert session.post.await_count == 1


async def test_login_unknown_error_raises_login_failed() -> None:
    """Raise LoginFailed for an error message the provider does not map."""
    session = _session({"error": {"code": 400, "message": "TOO_MANY_ATTEMPTS_TRY_LATER"}})

    with pytest.raises(LoginFailed, match="TOO_MANY_ATTEMPTS_TRY_LATER"):
        await _login(session, "jane@example.com", "secret")
