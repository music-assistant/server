"""Tests for what the Snapcast provider cleans up for a removed player."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

from bidict import bidict

from music_assistant.providers.snapcast.player import SnapCastPlayer
from music_assistant.providers.snapcast.provider import SnapCastProvider

if TYPE_CHECKING:
    import pytest

PLAYER_ID = "ma_kitchen"
SNAP_CLIENT_ID = "kitchen"


def _removed_player(delete_result: tuple[bool, str | None]) -> tuple[SnapCastPlayer, AsyncMock]:
    """
    Return a player of a provider whose snapserver answers a client removal as given.

    :param delete_result: What the snapserver returns for the removal of a client.
    """
    provider = SnapCastProvider.__new__(SnapCastProvider)
    provider.logger = logging.getLogger("test.snapcast")
    provider._ids_map = bidict({PLAYER_ID: SNAP_CLIENT_ID})
    provider._snapserver = MagicMock()
    provider._snapserver.delete_client = AsyncMock(return_value=delete_result)
    player = SnapCastPlayer.__new__(SnapCastPlayer)
    player._provider = provider
    player._protocol_id = PLAYER_ID
    return player, provider._snapserver.delete_client


async def test_removed_player_has_its_client_removed_from_the_snapserver() -> None:
    """A removed player is removed from the snapserver as well, under its client id."""
    player, delete_client = _removed_player((True, None))

    await player.on_remove()

    delete_client.assert_awaited_once_with(SNAP_CLIENT_ID)


async def test_a_snapserver_that_refuses_the_removal_is_only_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A client the snapserver does not remove leaves a warning, no error."""
    player, _ = _removed_player((False, "unknown client"))

    with caplog.at_level(logging.WARNING):
        await player.on_remove()

    assert "Unable to remove snapclient ma_kitchen: unknown client" in caplog.text
