"""Replay-based DACP compliance tests for the AirPlay handler."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from music_assistant.providers.airplay.provider import AirPlayProvider

from .conftest import make_player
from .helpers import build_dacp_request, replay

pytestmark = pytest.mark.asyncio


async def test_unknown_active_remote_is_ignored(
    airplay_provider: AirPlayProvider, mock_mass: MagicMock
) -> None:
    """A request whose Active-Remote matches no player must be a no-op (and not crash)."""
    make_player(mock_mass, active_remote="123")
    raw = build_dacp_request("/ctrl-int/1/play", active_remote="999")

    await replay(airplay_provider, raw)

    mock_mass.players.cmd_play.assert_not_called()
