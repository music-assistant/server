"""Tests for the MPD player connection handling."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mpd import CommandError
from mpd import ConnectionError as MPDConnectionError
from music_assistant_models.enums import PlaybackState
from music_assistant_models.errors import PlayerCommandFailed

from music_assistant.providers.mpd.constants import (
    ELAPSED_POLL_INTERVAL,
    HEALTHCHECK_POLL_INTERVAL,
    RECONNECT_DELAY,
)
from music_assistant.providers.mpd.player import MPDPlayer


def _mock_client() -> MagicMock:
    """Return a mocked MPDClient that connects successfully."""
    client = MagicMock()
    client.connect = AsyncMock()
    client.password = AsyncMock()
    client.status = AsyncMock(return_value={"state": "stop"})
    return client


def _assert_reconnect_scheduled(player: MPDPlayer) -> None:
    """Assert a reconnect attempt was armed for this player."""
    cast("MagicMock", player.mass).call_later.assert_any_call(
        RECONNECT_DELAY, player._connect, task_id=player._reconnect_task_id
    )


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
    """Disconnecting must disarm a reconnect that is still waiting on its timer."""
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


@pytest.mark.parametrize(
    ("method", "client_method", "args"),
    [
        ("play", "pause", ()),
        ("pause", "pause", ()),
        ("stop", "stop", ()),
        ("volume_set", "setvol", (42,)),
        ("seek", "seekcur", (30,)),
    ],
)
async def test_command_failure_reconnects_instead_of_staying_available(
    make_mpd_player: Callable[..., MPDPlayer],
    method: str,
    client_method: str,
    args: tuple[int, ...],
) -> None:
    """A command on a dead socket must mark the player unavailable and reconnect."""
    player = make_mpd_player()
    client = _mock_client()
    setattr(
        client,
        client_method,
        AsyncMock(side_effect=MPDConnectionError("Can not send command to disconnected client")),
    )
    player._client = client
    cast("MagicMock", player.mass).call_later.reset_mock()

    with pytest.raises(PlayerCommandFailed):
        await getattr(player, method)(*args)

    assert player.available is False
    _assert_reconnect_scheduled(player)


async def test_play_media_failure_reconnects_instead_of_staying_available(
    make_mpd_player: Callable[..., MPDPlayer],
) -> None:
    """A failed play_media must not leave the player available-but-dead."""
    player = make_mpd_player()
    client = _mock_client()
    player._client = client
    mass = cast("MagicMock", player.mass)
    mass.streams.resolve_stream_url = AsyncMock(return_value="http://10.0.0.6:8097/stream")
    mass.call_later.reset_mock()

    with (
        patch.object(
            client,
            "clear",
            AsyncMock(
                side_effect=MPDConnectionError("Can not send command to disconnected client")
            ),
        ),
        pytest.raises(PlayerCommandFailed),
    ):
        await player.play_media(MagicMock())

    assert player.available is False
    _assert_reconnect_scheduled(player)


async def test_poll_failure_reconnects(
    make_mpd_player: Callable[..., MPDPlayer],
) -> None:
    """A status probe on a dead socket must trigger a reconnect."""
    player = make_mpd_player()
    client = _mock_client()
    client.status = AsyncMock(side_effect=MPDConnectionError("Connection lost while reading line"))
    player._client = client
    cast("MagicMock", player.mass).call_later.reset_mock()

    await player.poll()

    assert player.available is False
    _assert_reconnect_scheduled(player)


async def test_idle_player_is_still_polled(
    make_mpd_player: Callable[..., MPDPlayer],
) -> None:
    """An idle player must still be probed, otherwise a dead socket goes unnoticed."""
    player = make_mpd_player()

    assert player.needs_poll is True
    assert player.poll_interval == HEALTHCHECK_POLL_INTERVAL

    player._attr_playback_state = PlaybackState.PLAYING

    assert player.poll_interval == ELAPSED_POLL_INTERVAL


async def test_command_ack_keeps_a_healthy_player(
    make_mpd_player: Callable[..., MPDPlayer],
) -> None:
    """An MPD ACK (CommandError) is not a lost connection and must not reconnect."""
    player = make_mpd_player()
    client = _mock_client()
    client.setvol = AsyncMock(side_effect=CommandError("[2@0] {setvol} No mixer configured"))
    player._client = client
    mass = cast("MagicMock", player.mass)
    mass.call_later.reset_mock()

    with pytest.raises(PlayerCommandFailed):
        await player.volume_set(42)

    assert player.available is True
    mass.call_later.assert_not_called()


async def test_poll_timeout_is_treated_as_a_lost_connection(
    make_mpd_player: Callable[..., MPDPlayer],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A status poll that never answers must not stall the poll loop forever."""
    monkeypatch.setattr("music_assistant.providers.mpd.player.STATUS_TIMEOUT", 0.05)
    player = make_mpd_player()
    client = _mock_client()

    def _hang() -> object:
        return asyncio.sleep(30)

    client.status = _hang
    player._client = client
    cast("MagicMock", player.mass).call_later.reset_mock()

    await asyncio.wait_for(player.poll(), timeout=5)

    assert player.available is False
    _assert_reconnect_scheduled(player)


async def test_poll_command_error_keeps_the_player(
    make_mpd_player: Callable[..., MPDPlayer],
) -> None:
    """A status command rejected by a live server is not a lost connection either."""
    player = make_mpd_player()
    client = _mock_client()
    client.status = AsyncMock(side_effect=CommandError("[2@0] {status} unknown command"))
    player._client = client
    mass = cast("MagicMock", player.mass)
    mass.call_later.reset_mock()

    await player.poll()

    assert player.available is True
    mass.call_later.assert_not_called()
