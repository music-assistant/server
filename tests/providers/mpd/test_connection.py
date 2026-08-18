"""Tests for the MPD player connection handling."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

from mpd import CommandError
from mpd import ConnectionError as MPDConnectionError

from music_assistant.providers.mpd.player import MPDPlayer


def _mock_client() -> MagicMock:
    """Return a mocked MPDClient that connects successfully."""
    client = MagicMock()
    client.connect = AsyncMock()
    client.password = AsyncMock()
    client.status = AsyncMock(return_value={"state": "stop"})
    return client


async def test_failed_authentication_closes_the_connection(
    make_mpd_player: Callable[..., MPDPlayer],
) -> None:
    """A rejected password must not leave the connected client behind."""
    player = make_mpd_player()
    player.password = "wrong"
    client = _mock_client()
    client.password = AsyncMock(side_effect=CommandError("[3@0] {password} incorrect password"))

    with patch("music_assistant.providers.mpd.player.MPDClient", return_value=client):
        await player._connect()

    client.disconnect.assert_called_once()
    assert player._client is None
    assert player.needs_setup is True


async def test_failed_idle_connection_closes_the_command_connection(
    make_mpd_player: Callable[..., MPDPlayer],
) -> None:
    """When the idle connection fails, the command connection must be closed too."""
    player = make_mpd_player()
    player.password = None
    command_client = _mock_client()
    idle_client = _mock_client()
    idle_client.connect = AsyncMock(side_effect=MPDConnectionError("connection lost"))

    with patch(
        "music_assistant.providers.mpd.player.MPDClient",
        side_effect=[command_client, idle_client],
    ):
        await player._connect()

    command_client.disconnect.assert_called_once()
    idle_client.disconnect.assert_called_once()
    assert player._client is None
    assert player._idle_client is None
    assert player.available is False


async def test_disconnect_cancels_a_pending_reconnect(
    make_mpd_player: Callable[..., MPDPlayer],
) -> None:
    """Unloading a player must not leave a reconnect attempt armed."""
    player = make_mpd_player()
    cast("MagicMock", player.mass.get_task).return_value = None

    await player._disconnect()

    cast("MagicMock", player.mass.cancel_timer).assert_called_once_with(player._reconnect_task_id)
    cast("MagicMock", player.mass.cancel_task).assert_not_called()


async def test_disconnect_stops_a_connect_attempt_in_flight(
    make_mpd_player: Callable[..., MPDPlayer],
) -> None:
    """A connect attempt that already started must not outlive the teardown."""
    player = make_mpd_player()
    in_flight = asyncio.create_task(asyncio.sleep(30))
    cast("MagicMock", player.mass.get_task).return_value = in_flight

    await player._disconnect()

    cast("MagicMock", player.mass.cancel_task).assert_called_once_with(player._reconnect_task_id)
    in_flight.cancel()


async def test_disconnect_never_cancels_its_own_connect_attempt(
    make_mpd_player: Callable[..., MPDPlayer],
) -> None:
    """The teardown at the start of a connect attempt must not cancel that attempt."""
    player = make_mpd_player()
    cast("MagicMock", player.mass.get_task).return_value = asyncio.current_task()

    await player._disconnect()

    cast("MagicMock", player.mass.cancel_task).assert_not_called()
