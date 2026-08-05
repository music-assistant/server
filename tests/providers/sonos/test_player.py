"""Tests for the Sonos player connection/reconnect handling."""

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import ConnectionTimeoutError
from aiosonos.exceptions import CannotConnect

from music_assistant.providers.sonos.player import SonosPlayer


def _make_player() -> tuple[SonosPlayer, MagicMock]:
    """Create a SonosPlayer with mocked connection dependencies."""
    player = SonosPlayer.__new__(SonosPlayer)
    mass = MagicMock()
    mass.closing = False
    mass.players.get_player.return_value = MagicMock()
    player.mass = mass
    player.logger = logging.getLogger("test.sonos.player")
    player._player_id = "sonos_player"
    player._listen_task = None
    player.connected = False
    player.client = MagicMock()
    player.update_state = MagicMock()  # type: ignore[misc, method-assign]
    return player, mass


@pytest.mark.asyncio
async def test_connect_timeout_reschedules_reconnect() -> None:
    """Test a blackholed connection (timeout, not refused) still schedules a retry."""
    player, mass = _make_player()
    player.client.connect = AsyncMock(  # type: ignore[method-assign]
        side_effect=ConnectionTimeoutError("Connection timeout to host https://x:1443")
    )

    await player._connect(retry_on_fail=30)

    assert player._attr_available is False
    mass.call_later.assert_called_once()
    args, _ = mass.call_later.call_args
    assert args[0] == min(30 + 30, 3600)
    assert args[1] == player._connect


@pytest.mark.asyncio
async def test_connect_timeout_without_retry_raises() -> None:
    """Test a connection failure without retry_on_fail still propagates."""
    player, mass = _make_player()
    player.client.connect = AsyncMock(  # type: ignore[method-assign]
        side_effect=ConnectionTimeoutError("Connection timeout to host https://x:1443")
    )

    with pytest.raises(ConnectionTimeoutError):
        await player._connect(retry_on_fail=0)

    mass.call_later.assert_not_called()


@pytest.mark.asyncio
async def test_connect_websocket_handshake_failure_reschedules_reconnect() -> None:
    """Test a websocket handshake failure also reschedules a retry."""
    player, mass = _make_player()
    player.client.connect = AsyncMock(  # type: ignore[method-assign]
        side_effect=CannotConnect(OSError("handshake failed"))
    )

    await player._connect(retry_on_fail=30)

    assert player._attr_available is False
    mass.call_later.assert_called_once()
