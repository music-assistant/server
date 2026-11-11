"""Tests for setting up and attaching a discovered Chromecast."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from music_assistant.providers.chromecast.provider import ChromecastProvider

PROTOCOL_ID = "cast-uuid"


@pytest.mark.parametrize("attaches", [True, False], ids=["attached", "skipped"])
async def test_a_skipped_attach_releases_the_device(attaches: bool) -> None:
    """A device the attach skips (e.g. of a disabled player) is disconnected and gets no bridge."""
    provider = ChromecastProvider.__new__(ChromecastProvider)
    provider.mass = MagicMock()
    provider._pending_discoveries = {PROTOCOL_ID}
    provider.bridge_manager = MagicMock(evaluate_bridge=AsyncMock())
    castplayer = MagicMock(async_setup=AsyncMock(), on_detach=AsyncMock())
    provider.mass.players.attach = AsyncMock()
    provider.mass.players.get_protocol.return_value = castplayer if attaches else None

    with patch(
        "music_assistant.providers.chromecast.provider.ChromecastPlayer", return_value=castplayer
    ):
        await provider._create_and_register_player(PROTOCOL_ID, MagicMock(), MagicMock())

    provider.mass.players.attach.assert_awaited_once_with(castplayer)
    assert castplayer.on_detach.await_count == (0 if attaches else 1)
    assert provider.bridge_manager.evaluate_bridge.await_count == (1 if attaches else 0)
    assert PROTOCOL_ID not in provider._pending_discoveries
