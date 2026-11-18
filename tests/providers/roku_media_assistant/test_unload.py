"""Tests for unloading the Media Assistant (Roku) provider and removing its players."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from music_assistant.providers.roku_media_assistant.player import MediaAssistantPlayer
from music_assistant.providers.roku_media_assistant.provider import MediaAssistantprovider
from tests.common import use_real_create_task


def _roku_player(player_id: str) -> MagicMock:
    """Return a connected Roku player stand-in."""
    player = MagicMock()
    player.protocol_id = player_id
    player.name = player_id
    player.lock = asyncio.Lock()
    player.roku.close_session = AsyncMock()
    return player


async def test_unload_disconnects_every_player() -> None:
    """Unloading disconnects all Rokus, although each disconnect forgets its player."""
    provider = MediaAssistantprovider.__new__(MediaAssistantprovider)
    provider.mass = MagicMock()
    use_real_create_task(provider.mass)
    provider.logger = logging.getLogger("test.roku_media_assistant.provider")
    players = [_roku_player("ROKU_A"), _roku_player("ROKU_B")]
    provider.roku_players = {p.protocol_id: p for p in players}  # type: ignore[misc]

    await provider.unload()

    assert provider.roku_players == {}
    for player in players:
        player.roku.close_session.assert_awaited_once()


@pytest.mark.parametrize("connected", [True, False], ids=["connected", "not-connected"])
async def test_removed_player_is_disconnected_and_forgotten(connected: bool) -> None:
    """A removed player closes its connection and leaves the players discovery knows."""
    provider = MediaAssistantprovider.__new__(MediaAssistantprovider)
    provider.logger = logging.getLogger("test.roku_media_assistant.provider")
    player = MediaAssistantPlayer.__new__(MediaAssistantPlayer)
    player._provider = provider
    player._protocol_id = "ROKU_A"
    player._player = None
    player._attr_name = "Roku A"
    player.lock = asyncio.Lock()
    roku = MagicMock()
    roku.close_session = AsyncMock()
    player.roku = roku if connected else None  # type: ignore[assignment]
    other = _roku_player("ROKU_B")
    provider.roku_players = {"ROKU_A": player, "ROKU_B": other}  # type: ignore[misc]

    await player.on_remove()

    assert provider.roku_players == {"ROKU_B": other}
    assert roku.close_session.await_count == (1 if connected else 0)


async def test_removed_player_leaves_a_replacement_found_meanwhile() -> None:
    """A Roku found again while its removed player disconnects keeps its new player."""
    provider = MediaAssistantprovider.__new__(MediaAssistantprovider)
    provider.logger = logging.getLogger("test.roku_media_assistant.provider")
    player = _roku_player("ROKU_A")
    replacement = _roku_player("ROKU_A")

    async def close_session() -> None:
        provider.roku_players["ROKU_A"] = replacement

    player.roku.close_session = AsyncMock(side_effect=close_session)
    provider.roku_players = {"ROKU_A": player}  # type: ignore[misc]

    await provider.forget_player(player)

    assert provider.roku_players == {"ROKU_A": replacement}
