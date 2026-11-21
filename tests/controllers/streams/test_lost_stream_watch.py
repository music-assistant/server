"""
Tests for a player that closes a stream it plays and never asks for it again.

Some receivers reset the connection of a stream they play and keep reporting that they
play. A stream that would not have ended by itself (a flow stream, a radio or live item)
and that the player closed is watched: unless the player asks for a stream of the
session again within the grace period, the queue restarts where it plays now.
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import PlayerUnavailableError

from music_assistant.controllers.streams import controller as controller_mod
from music_assistant.controllers.streams.constants import LOST_STREAM_GRACE
from music_assistant.controllers.streams.controller import StreamsController
from tests.controllers.streams.test_realtime_sources import (
    _FakeStreamResponse,
    _flow_handler,
    _single_item_handler,
)

SESSION_ID = "session-1"
QUEUE_ID = "queue-1"
PLAYER_ID = "player-1"


def _playing_player(controller: Any) -> MagicMock:
    """Return the output protocol of a handler, as a player that plays and was not stopped."""
    protocol = cast("MagicMock", controller.mass.players.get_output_protocol.return_value)
    protocol.player.stop_called = False
    queue_data = controller.mass.player_queues.queue_data.return_value
    queue_data.queue = controller.mass.player_queues.get.return_value
    return protocol


def _encoded_audio(request: MagicMock, *, quiet_end: bool = False) -> Any:
    """
    Return a stand-in for the encode ffmpeg that yields three chunks.

    :param request: The stream request.
    :param quiet_end: Close the connection of the request before the stream ends, without a
        write error.
    """

    async def _stream(**_kwargs: Any) -> Any:
        for _ in range(3):
            yield b"a" * 1000
        if quiet_end:
            request.transport = None

    return _stream


def _connection_reset_on_second_write(monkeypatch: pytest.MonkeyPatch, request: MagicMock) -> None:
    """Make the player reset its connection after it got its first chunk."""
    writes = {"count": 0}

    async def _write(_self: Any, _chunk: bytes) -> None:
        writes["count"] += 1
        if writes["count"] > 1:
            request.transport = None
            raise ConnectionResetError

    monkeypatch.setattr(_FakeStreamResponse, "write", _write)


@pytest.mark.parametrize("quiet_end", [False, True], ids=["connection_reset", "quiet_end"])
async def test_a_closed_flow_stream_starts_the_wait(
    monkeypatch: pytest.MonkeyPatch, quiet_end: bool
) -> None:
    """A player that closed its flow stream gets the grace period to ask for it again."""
    controller, request, _ = _flow_handler(monkeypatch, is_realtime=False)
    _playing_player(controller)
    monkeypatch.setattr(
        controller_mod, "get_ffmpeg_stream", _encoded_audio(request, quiet_end=quiet_end)
    )
    if not quiet_end:
        _connection_reset_on_second_write(monkeypatch, request)

    await controller.serve_queue_flow_stream(request)

    controller.mass.call_later.assert_called_once_with(
        LOST_STREAM_GRACE,
        controller._restart_lost_stream_unless_reopened,
        QUEUE_ID,
        SESSION_ID,
        PLAYER_ID,
        None,
        task_id=f"lost_stream_{QUEUE_ID}_{PLAYER_ID}",
    )


@pytest.mark.parametrize(
    "condition", ["connected", "another_session", "stop_called", "flow_fully_generated"]
)
async def test_a_flow_stream_that_was_not_lost_starts_no_wait(
    monkeypatch: pytest.MonkeyPatch, condition: str
) -> None:
    """A stream that ends while the player stays connected, moved on or stopped is no loss."""
    controller, request, _ = _flow_handler(monkeypatch, is_realtime=False)
    protocol = _playing_player(controller)
    queue_data = controller.mass.player_queues.queue_data.return_value

    async def _stream(**_kwargs: Any) -> Any:
        yield b"a" * 1000
        if condition != "connected":
            request.transport = None
        if condition == "another_session":
            queue_data.session_id = "session-2"
        elif condition == "stop_called":
            protocol.player.stop_called = True
        elif condition == "flow_fully_generated":
            controller.mass.player_queues.flow_stream_finished.return_value = True

    monkeypatch.setattr(controller_mod, "get_ffmpeg_stream", _stream)

    await controller.serve_queue_flow_stream(request)

    controller.mass.call_later.assert_not_called()


@pytest.mark.parametrize(
    ("media_type", "watched"),
    [(MediaType.RADIO, True), (MediaType.AUDIO_SOURCE, True), (MediaType.TRACK, False)],
    ids=["radio", "audio_source", "track"],
)
async def test_only_a_lost_item_that_never_ends_starts_the_wait(
    monkeypatch: pytest.MonkeyPatch, media_type: MediaType, watched: bool
) -> None:
    """A track ends by itself, so a player that closes its stream may just hold all of it."""
    controller, request, _ = _single_item_handler(is_realtime=True, capture_ffmpeg=monkeypatch)
    controller._active_output_streams = 0
    controller.mass.player_queues.get_item.return_value.media_type = media_type
    _playing_player(controller)
    monkeypatch.setattr(controller_mod, "get_ffmpeg_stream", _encoded_audio(request))
    _connection_reset_on_second_write(monkeypatch, request)

    await controller.serve_queue_item_stream(request)

    if watched:
        controller.mass.call_later.assert_called_once()
        assert controller.mass.call_later.call_args.kwargs == {
            "task_id": f"lost_stream_{QUEUE_ID}_{PLAYER_ID}"
        }
        queue_item = controller.mass.player_queues.get_item.return_value
        assert controller.mass.call_later.call_args.args[-1] == queue_item.queue_item_id
    else:
        controller.mass.call_later.assert_not_called()


@pytest.mark.parametrize("route", ["flow", "single"])
async def test_a_probe_starts_no_wait(monkeypatch: pytest.MonkeyPatch, route: str) -> None:
    """A HEAD request a renderer probes the stream with and closes is no loss."""
    if route == "flow":
        controller, request, _ = _flow_handler(monkeypatch, is_realtime=False)
    else:
        controller, request, _ = _single_item_handler(is_realtime=True, capture_ffmpeg=monkeypatch)
        controller.mass.player_queues.get_item.return_value.media_type = MediaType.RADIO
    _playing_player(controller)
    request.method = "HEAD"
    request.transport = None

    if route == "flow":
        await controller.serve_queue_flow_stream(request)
    else:
        await controller.serve_queue_item_stream(request)

    controller.mass.call_later.assert_not_called()


async def test_a_lost_item_stream_that_ended_by_itself_starts_no_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A radio stream the station ended is no loss of the player."""
    controller, request, _ = _single_item_handler(is_realtime=True, capture_ffmpeg=monkeypatch)
    controller._active_output_streams = 0
    controller.mass.player_queues.get_item.return_value.media_type = MediaType.RADIO
    _playing_player(controller)
    monkeypatch.setattr(
        controller_mod, "get_ffmpeg_stream", _encoded_audio(request, quiet_end=True)
    )

    await controller.serve_queue_item_stream(request)

    controller.mass.call_later.assert_not_called()


# -- the end of the wait --


def _waiting_controller(*, needs_poll: bool = False) -> Any:
    """Return a streams controller whose player lost the stream of session-1."""
    controller = cast("Any", object.__new__(StreamsController))
    controller.mass = MagicMock()
    controller.logger = MagicMock()
    controller._open_item_streams = {}
    protocol = MagicMock(display_name="Player", needs_poll=needs_poll)
    protocol.poll = AsyncMock()
    controller.mass.players.get_output_protocol.return_value = protocol
    return controller


def _open_request(
    *,
    session_id: str = SESSION_ID,
    player_id: str = PLAYER_ID,
    method: str = "GET",
    connected: bool = True,
) -> tuple[str, MagicMock]:
    request = MagicMock(method=method, match_info={"player_id": player_id})
    if not connected:
        request.transport = None
    return session_id, request


async def _end_wait(controller: Any) -> None:
    await controller._restart_lost_stream_unless_reopened(QUEUE_ID, SESSION_ID, PLAYER_ID)


async def test_a_player_that_did_not_ask_again_restarts_its_queue() -> None:
    """The queue restarts, as long as its player still plays it."""
    controller = _waiting_controller()

    await _end_wait(controller)

    controller.mass.player_queues.restart_lost_flow_stream.assert_called_once_with(
        QUEUE_ID, SESSION_ID, only_while_playing=True, queue_item_id=None
    )
    controller.mass.players.get_output_protocol.return_value.poll.assert_not_awaited()


async def test_a_player_that_asked_again_restarts_nothing() -> None:
    """An open GET of the same player in the same session streams again."""
    controller = _waiting_controller()
    controller._open_item_streams[QUEUE_ID] = [_open_request()]

    await _end_wait(controller)

    controller.mass.player_queues.restart_lost_flow_stream.assert_not_called()


@pytest.mark.parametrize(
    "request_entry",
    [
        _open_request(player_id="player-2"),
        _open_request(session_id="session-0"),
        _open_request(method="HEAD"),
        _open_request(connected=False),
    ],
    ids=["another_player", "another_session", "head_probe", "closed_meanwhile"],
)
async def test_a_request_that_streams_nothing_to_the_player_still_restarts(
    request_entry: tuple[str, MagicMock],
) -> None:
    """Only an open GET of the same player in the same session counts as asking again."""
    controller = _waiting_controller()
    controller._open_item_streams[QUEUE_ID] = [request_entry]

    await _end_wait(controller)

    controller.mass.player_queues.restart_lost_flow_stream.assert_called_once()


async def test_a_lost_item_restarts_only_as_that_item() -> None:
    """The queue restarts the lost single item only while that item is still its current one."""
    controller = _waiting_controller()

    await controller._restart_lost_stream_unless_reopened(QUEUE_ID, SESSION_ID, PLAYER_ID, "item-1")

    controller.mass.player_queues.restart_lost_flow_stream.assert_called_once_with(
        QUEUE_ID, SESSION_ID, only_while_playing=True, queue_item_id="item-1"
    )


async def test_a_polled_player_is_polled_before_the_queue_decides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stop at the device of a player without state events is seen before the restart."""
    monkeypatch.setattr(controller_mod, "LOST_STREAM_POLL_SETTLE", 0)
    controller = _waiting_controller(needs_poll=True)
    protocol = controller.mass.players.get_output_protocol.return_value
    order: list[str] = []
    protocol.poll.side_effect = lambda: order.append("poll")
    controller.mass.player_queues.restart_lost_flow_stream.side_effect = lambda *_args, **_kwargs: (
        order.append("restart")
    )

    await _end_wait(controller)

    controller.mass.players.get_output_protocol.assert_called_once_with(PLAYER_ID)
    assert order == ["poll", "restart"]


async def test_a_player_that_asks_again_while_it_is_polled_restarts_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stream the player opens while it is polled counts as asking for it again."""
    monkeypatch.setattr(controller_mod, "LOST_STREAM_POLL_SETTLE", 0)
    controller = _waiting_controller(needs_poll=True)
    controller.mass.players.get_output_protocol.return_value.poll.side_effect = lambda: (
        controller._open_item_streams.setdefault(QUEUE_ID, []).append(_open_request())
    )

    await _end_wait(controller)

    controller.mass.player_queues.restart_lost_flow_stream.assert_not_called()


@pytest.mark.parametrize(
    ("error", "log_level"),
    [(PlayerUnavailableError("gone"), "debug"), (RuntimeError("bug"), "warning")],
    ids=["unavailable", "unexpected"],
)
async def test_a_polled_player_that_does_not_answer_restarts_nothing(
    error: Exception, log_level: str
) -> None:
    """A player that can not tell whether it still plays is not restarted."""
    controller = _waiting_controller(needs_poll=True)
    controller.mass.players.get_output_protocol.return_value.poll.side_effect = error

    await _end_wait(controller)

    controller.mass.player_queues.restart_lost_flow_stream.assert_not_called()
    getattr(controller.logger, log_level).assert_called_once()
