"""Tests for the cast dashboard helper."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import patch

import pytest

from music_assistant.helpers.cast_dashboard import CAST_VIEWER_USERNAME, get_cast_code
from music_assistant.mass import MusicAssistant


@pytest.fixture(autouse=True)
def _use_ephemeral_ports() -> Generator[None]:
    """
    Bind the webserver and streamserver to OS-assigned ephemeral ports.

    Avoids clashing with a Music Assistant instance already running on the
    default ports (8095/8097) on the developer's machine. Autouse ensures the
    patch is active before the `mass` fixture boots the server.
    """
    with (
        patch("music_assistant.controllers.webserver.controller.DEFAULT_SERVER_PORT", 0),
        patch("music_assistant.controllers.streams.controller.DEFAULT_PORT", 0),
    ):
        yield


async def test_get_cast_code_creates_viewer_user_and_code(mass: MusicAssistant) -> None:
    """A cast code can be exchanged for a token belonging to the cast viewer user."""
    code = await get_cast_code(mass)

    assert code
    result = await mass.webserver.auth.exchange_join_code(code)

    assert result["success"] is True
    user = await mass.webserver.auth.authenticate_with_token(result["access_token"])
    assert user is not None
    assert user.username == CAST_VIEWER_USERNAME
