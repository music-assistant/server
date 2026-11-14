"""Tests for the MPD player connection handling."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
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


# python-mpd2 hands the raw socket error to commands that were in flight when the
# connection broke, and ConnectionError to the ones issued after it
CONNECTION_ERRORS = [
    pytest.param(MPDConnectionError("Can not send command to disconnected client"), id="mpd"),
    pytest.param(ConnectionResetError(104, "Connection reset by peer"), id="oserror"),
]


@pytest.mark.parametrize("error", CONNECTION_ERRORS)
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
    error: Exception,
) -> None:
    """A command on a dead socket must mark the player unavailable and reconnect."""
    player = make_mpd_player()
    client = _mock_client()
    setattr(client, client_method, AsyncMock(side_effect=error))
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
    monkeypatch.setattr("music_assistant.providers.mpd.player.COMMAND_TIMEOUT", 0.05)
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


def _reconnects_scheduled(player: MPDPlayer) -> int:
    """Return how often a reconnect was (re-)armed for this player."""
    return sum(
        1
        for call in cast("MagicMock", player.mass).call_later.call_args_list
        if call.kwargs.get("task_id") == player._reconnect_task_id
    )


def _published_client(player: MPDPlayer) -> MagicMock | None:
    """Return the command client the player currently exposes to polls and commands."""
    return cast("MagicMock | None", player._client)


def _slow_connect_client(release: asyncio.Event) -> MagicMock:
    """Return a mocked MPDClient whose connect blocks until release is set."""
    client = _mock_client()

    async def _connect(*_args: object) -> None:
        await release.wait()

    client.connect = AsyncMock(side_effect=_connect)
    return client


async def test_poll_during_a_connect_attempt_finds_no_client(
    make_mpd_player: Callable[..., MPDPlayer],
) -> None:
    """
    A poll racing a connect attempt must not see the half-connected client.

    It would report a lost connection, and the reconnect it arms would tear down
    the attempt right after it succeeded, every poll interval.
    """
    player = make_mpd_player()
    player.password = None
    release = asyncio.Event()
    command_client = _slow_connect_client(release)
    idle_client = _mock_client()

    with patch(
        "music_assistant.providers.mpd.player.MPDClient",
        side_effect=[command_client, idle_client],
    ):
        attempt = asyncio.create_task(player._connect())
        await asyncio.sleep(0)
        cast("MagicMock", player.mass).call_later.reset_mock()

        assert _published_client(player) is None
        await player.poll()

        command_client.status.assert_not_called()
        assert _reconnects_scheduled(player) == 0

        release.set()
        await attempt

    assert player._client is command_client
    assert player._idle_client is idle_client
    assert player.available is True


async def test_cancelled_connect_attempt_closes_its_client(
    make_mpd_player: Callable[..., MPDPlayer],
) -> None:
    """A connect attempt aborted by a newer one must not leak its connection."""
    player = make_mpd_player()
    player.password = None
    command_client = _slow_connect_client(asyncio.Event())

    with patch("music_assistant.providers.mpd.player.MPDClient", return_value=command_client):
        attempt = asyncio.create_task(player._connect())
        await asyncio.sleep(0)
        attempt.cancel()
        with pytest.raises(asyncio.CancelledError):
            await attempt

    command_client.disconnect.assert_called_once()
    assert player._client is None


async def test_repeated_loss_does_not_push_back_the_pending_reconnect(
    make_mpd_player: Callable[..., MPDPlayer],
) -> None:
    """Only the first loss arms a reconnect; later ones must not restart its timer."""
    player = make_mpd_player()
    client = _mock_client()
    lost = MPDConnectionError("Connection lost while reading line")
    client.status = AsyncMock(side_effect=lost)
    client.pause = AsyncMock(side_effect=lost)
    player._client = client
    cast("MagicMock", player.mass).call_later.reset_mock()

    await player.poll()
    await player.poll()
    with pytest.raises(PlayerCommandFailed):
        await player.pause()

    assert player.available is False
    assert _reconnects_scheduled(player) == 1


async def test_poll_socket_error_is_treated_as_a_lost_connection(
    make_mpd_player: Callable[..., MPDPlayer],
) -> None:
    """A raw socket error from an in-flight status poll must reconnect too."""
    player = make_mpd_player()
    client = _mock_client()
    client.status = AsyncMock(side_effect=ConnectionResetError(104, "Connection reset by peer"))
    player._client = client
    cast("MagicMock", player.mass).call_later.reset_mock()

    await player.poll()

    assert player.available is False
    _assert_reconnect_scheduled(player)


async def test_idle_loop_socket_error_is_treated_as_a_lost_connection(
    make_mpd_player: Callable[..., MPDPlayer],
) -> None:
    """A raw socket error ending the idle loop must reconnect, like an MPDError does."""
    player = make_mpd_player()

    async def _abort() -> str:
        raise ConnectionAbortedError(103, "Software caused connection abort")

    async def _idle() -> AsyncIterator[str]:
        yield await _abort()

    idle_client = MagicMock()
    idle_client.idle = _idle
    player._idle_client = idle_client
    cast("MagicMock", player.mass).call_later.reset_mock()

    await player._idle_loop()

    assert player.available is False
    _assert_reconnect_scheduled(player)


async def test_bare_connection_error_is_logged_by_name(
    make_mpd_player: Callable[..., MPDPlayer],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """python-mpd2's message-less ConnectionError() must not produce an empty reason."""
    player = make_mpd_player()
    client = _mock_client()
    client.status = AsyncMock(side_effect=MPDConnectionError())
    player._client = client

    with caplog.at_level(logging.WARNING):
        await player.poll()

    assert "Lost connection to MPD at 10.0.0.6:6600: ConnectionError" in caplog.text


async def _never_answers(*_args: object) -> None:
    """Stand in for an MPD command on a half-open socket."""
    await asyncio.sleep(30)


@pytest.mark.parametrize(
    ("method", "client_method", "args"),
    [
        ("play_media", "clear", (MagicMock(),)),
        ("play", "pause", ()),
        ("pause", "pause", ()),
        ("stop", "stop", ()),
        ("volume_set", "setvol", (42,)),
        ("seek", "seekcur", (30,)),
    ],
)
async def test_unanswered_command_is_treated_as_a_lost_connection(
    make_mpd_player: Callable[..., MPDPlayer],
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    client_method: str,
    args: tuple[object, ...],
) -> None:
    """A command on a half-open socket must give up instead of waiting for TCP."""
    monkeypatch.setattr("music_assistant.providers.mpd.player.COMMAND_TIMEOUT", 0.05)
    player = make_mpd_player()
    client = _mock_client()
    setattr(client, client_method, AsyncMock(side_effect=_never_answers))
    player._client = client
    mass = cast("MagicMock", player.mass)
    mass.streams.resolve_stream_url = AsyncMock(return_value="http://10.0.0.6:8097/stream")
    mass.call_later.reset_mock()

    with pytest.raises(PlayerCommandFailed, match="no reply within"):
        await asyncio.wait_for(getattr(player, method)(*args), timeout=5)

    assert player.available is False
    _assert_reconnect_scheduled(player)


async def test_hanging_connect_attempt_is_given_up_and_retried(
    make_mpd_player: Callable[..., MPDPlayer],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connect into a blackhole must time out, close its client and retry."""
    monkeypatch.setattr("music_assistant.providers.mpd.player.CONNECT_TIMEOUT", 0.05)
    player = make_mpd_player()
    player.password = None
    client = _mock_client()
    client.connect = AsyncMock(side_effect=_never_answers)
    cast("MagicMock", player.mass).call_later.reset_mock()

    with patch("music_assistant.providers.mpd.player.MPDClient", return_value=client):
        await asyncio.wait_for(player._connect(), timeout=5)

    client.disconnect.assert_called_once()
    assert player._client is None
    assert player.available is False
    _assert_reconnect_scheduled(player)
