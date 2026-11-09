"""Tests for setting up and registering a discovered Chromecast."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from music_assistant.providers.chromecast.provider import ChromecastProvider

PLAYER_ID = "cast-uuid"


@pytest.mark.parametrize("registers", [True, False], ids=["registered", "skipped"])
async def test_a_skipped_registration_releases_the_device(registers: bool) -> None:
    """A device that registration skips is disconnected again and gets no bridge."""
    provider = ChromecastProvider.__new__(ChromecastProvider)
    provider.mass = MagicMock()
    provider._pending_discoveries = {PLAYER_ID}
    provider.bridge_manager = MagicMock(evaluate_bridge=AsyncMock())
    castplayer = MagicMock(async_setup=AsyncMock(), on_unload=AsyncMock())
    provider.mass.players.register_or_update = AsyncMock()
    provider.mass.players.get_player.return_value = castplayer if registers else None

    with patch(
        "music_assistant.providers.chromecast.provider.ChromecastPlayer", return_value=castplayer
    ):
        await provider._create_and_register_player(PLAYER_ID, MagicMock(), MagicMock())

    assert castplayer.on_unload.await_count == (0 if registers else 1)
    assert provider.bridge_manager.evaluate_bridge.await_count == (1 if registers else 0)
    assert PLAYER_ID not in provider._pending_discoveries
