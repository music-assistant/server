"""Tests for setting up BluOS players."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

from pyblu.errors import PlayerUnreachableError

from music_assistant.providers.bluesound.player import BluesoundPlayer

if TYPE_CHECKING:
    from music_assistant.providers.bluesound.provider import BluesoundDiscoveryInfo


async def test_a_device_that_misses_its_first_status_is_added_as_unavailable() -> None:
    """A device that does not answer its first status call still gets a player, unavailable."""
    provider = MagicMock()
    provider.player_map = {}
    provider.mass.players.register_or_update = AsyncMock()
    discovery_info: BluesoundDiscoveryInfo = {
        "_objectType": "",
        "ip_address": "192.168.1.61",
        "port": "11000",
        "mac": "00:11:22:aa:bb:cc",
        "model": "NAD C658",
    }
    player = BluesoundPlayer(
        provider, "00:11:22:aa:bb:cc", discovery_info, "Speaker A", "192.168.1.61", 11000
    )

    with patch.object(
        player.client, "status", AsyncMock(side_effect=PlayerUnreachableError("timeout"))
    ):
        await player.setup()

    assert not player.available
    assert player.synced_to is None
    provider.mass.players.register_or_update.assert_awaited_once_with(player)
