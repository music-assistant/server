"""
Tests for a player that reconnects to the flow stream it lost.

A Cast receiver that loses its flow stream asks for it again with a Range request for
the bytes after those it holds. A new flow stream can not continue those bytes, so the
request is refused and the queue restarts at the position that plays now.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

from music_assistant.controllers.streams import controller as controller_mod
from tests.controllers.streams.test_audio_processing import (
    _native_stream_handler_context,
    _OutputPlanRequested,
)
from tests.controllers.streams.test_realtime_sources import _FakeStreamResponse, _flow_handler

FLOW_PATH = "/flow/session-1/queue-1/item-1/protocol-1.flac"
BYTES_SENT = 19_902_401


def _reconnect(
    monkeypatch: pytest.MonkeyPatch,
    *,
    range_start: int | None,
    path: str = FLOW_PATH,
    sent: tuple[str, int] = (FLOW_PATH, BYTES_SENT),
    method: str = "GET",
) -> tuple[Any, MagicMock]:
    """
    Return a flow handler and a request of a player that got part of its flow stream.

    :param monkeypatch: The pytest monkeypatch fixture.
    :param range_start: Start of the Range the request asks for, None for no Range.
    :param path: Path of the request.
    :param sent: The flow stream the player got and the bytes it was sent of it.
    :param method: HTTP method of the request.
    """
    controller, request, _ = _native_stream_handler_context(monkeypatch)
    controller._flow_bytes_sent = {"protocol-1": sent}
    request.method = method
    request.path = path
    if range_start is not None:
        request.headers = {"Range": f"bytes={range_start}-"}
        request.http_range = slice(range_start, None)
    return controller, request


async def test_a_continuation_of_a_lost_flow_stream_is_refused_and_the_queue_restarts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The player is told the stream ends there and gets a new one from the queue."""
    controller, request = _reconnect(monkeypatch, range_start=BYTES_SENT)
    restart = controller.mass.player_queues.restart_lost_flow_stream
    restart.return_value = True

    with pytest.raises(web.HTTPRequestRangeNotSatisfiable) as exc_info:
        await controller.serve_queue_flow_stream(request)

    assert exc_info.value.headers["Content-Range"] == f"bytes */{BYTES_SENT}"
    restart.assert_called_once_with("queue-1", "session-1")
    controller.audio.select_flow_pcm_format.assert_not_awaited()


async def test_past_the_restart_limit_a_continuation_gets_a_new_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queue that does not restart serves the request as a new flow stream."""
    controller, request = _reconnect(monkeypatch, range_start=BYTES_SENT - 1000)
    restart = controller.mass.player_queues.restart_lost_flow_stream
    restart.return_value = False

    with pytest.raises(_OutputPlanRequested):
        await controller.serve_queue_flow_stream(request)

    restart.assert_called_once_with("queue-1", "session-1")


@pytest.mark.parametrize(
    ("range_start", "sent"),
    [
        (None, (FLOW_PATH, BYTES_SENT)),
        (0, (FLOW_PATH, BYTES_SENT)),
        # more than the player was ever sent, such as a probe of the end of a declared length
        (BYTES_SENT + 1, (FLOW_PATH, BYTES_SENT)),
        # the player got another stream than the one it asks for
        (1000, ("/flow/session-1/queue-1/item-0/protocol-1.flac", BYTES_SENT)),
    ],
    ids=["no_range", "range_from_start", "beyond_bytes_sent", "another_stream"],
)
async def test_a_request_that_continues_no_stream_the_player_got_is_served(
    monkeypatch: pytest.MonkeyPatch, range_start: int | None, sent: tuple[str, int]
) -> None:
    """Only a continuation of the stream the player got restarts the queue."""
    controller, request = _reconnect(monkeypatch, range_start=range_start, sent=sent)

    with pytest.raises(_OutputPlanRequested):
        await controller.serve_queue_flow_stream(request)

    controller.mass.player_queues.restart_lost_flow_stream.assert_not_called()


async def test_a_flow_stream_the_player_never_got_is_served(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A player without a flow stream before gets a new one, whatever it asks for."""
    controller, request = _reconnect(monkeypatch, range_start=1000)
    controller._flow_bytes_sent = {}

    with pytest.raises(_OutputPlanRequested):
        await controller.serve_queue_flow_stream(request)

    controller.mass.player_queues.restart_lost_flow_stream.assert_not_called()


async def test_a_probe_of_a_lost_flow_stream_restarts_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A HEAD request with a Range only gets the headers."""
    controller, request = _reconnect(monkeypatch, range_start=BYTES_SENT, method="HEAD")

    await controller.serve_queue_flow_stream(request)

    controller.mass.player_queues.restart_lost_flow_stream.assert_not_called()


async def test_a_continuation_of_another_session_restarts_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stream of a session the queue moved past is unknown, also with a Range."""
    controller, request = _reconnect(monkeypatch, range_start=BYTES_SENT)
    controller.mass.player_queues.queue_data.return_value.session_id = "session-2"

    with pytest.raises(web.HTTPNotFound):
        await controller.serve_queue_flow_stream(request)

    controller.mass.player_queues.restart_lost_flow_stream.assert_not_called()


async def test_a_flow_stream_records_every_byte_it_sends_its_player(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The record holds the audio and the ICY metadata sent, by the player it went to."""
    controller, request, _ = _flow_handler(monkeypatch, is_realtime=False)
    controller._audio_analysis = MagicMock()
    controller._audio_analysis.shown_stream_metadata.return_value = None
    controller.mass.config.get_protocol_value.return_value = "full"
    request.headers = {"Icy-MetaData": "1"}
    request.path = "/flow/session-1/queue-1/item-1/player-1.flac"
    written: list[bytes] = []

    async def _encoded_audio(**_kwargs: Any) -> Any:
        for _ in range(3):
            yield b"a" * 100_000

    async def _record(_self: Any, chunk: bytes) -> None:
        written.append(chunk)

    monkeypatch.setattr(controller_mod, "get_ffmpeg_stream", _encoded_audio)
    monkeypatch.setattr(_FakeStreamResponse, "write", _record)

    await controller.serve_queue_flow_stream(request)

    total = sum(len(chunk) for chunk in written)
    assert total > 300_000
    assert controller._flow_bytes_sent == {"player-1": (request.path, total)}


@pytest.mark.parametrize(
    ("stream_url", "expected_call"),
    [
        ("http://192.168.1.10:8097/flow/session-1/queue-1/item-1/player-1.flac", True),
        ("http://192.168.1.10:8097/single/session-1/queue-1/item-1/player-1.flac", False),
        ("http://192.168.1.99:8097/flow/session-1/queue-1/item-1/player-1.flac", False),
        ("http://192.168.1.10:8097/flow/session-1/queue-1.flac", False),
    ],
    ids=["flow", "single_item", "another_server", "not_a_flow_url"],
)
def test_a_lost_stream_url_restarts_the_queue_of_its_flow_session(
    stream_url: str, expected_call: bool
) -> None:
    """A player's report of a lost flow stream URL restarts the session in that URL."""
    controller = MagicMock()
    controller.base_url = "http://192.168.1.10:8097"
    restart = controller.mass.player_queues.restart_lost_flow_stream
    restart.return_value = True

    restarted = controller_mod.StreamsController.restart_lost_stream(controller, stream_url)

    assert restarted is expected_call
    if expected_call:
        restart.assert_called_once_with("queue-1", "session-1")
    else:
        restart.assert_not_called()


async def test_a_lost_flow_stream_is_found_again_after_its_response_ended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The record outlives the response that sent it.

    The reconnect of a receiver arrives while the dropped response may still be tearing
    down, or after it ended.
    """
    controller, request, _ = _flow_handler(monkeypatch, is_realtime=False)
    request.path = "/flow/session-1/queue-1/item-1/player-1.flac"

    async def _encoded_audio(**_kwargs: Any) -> Any:
        yield b"a" * 50_000

    monkeypatch.setattr(controller_mod, "get_ffmpeg_stream", _encoded_audio)
    monkeypatch.setattr(
        controller_mod,
        "web",
        SimpleNamespace(
            StreamResponse=_FakeStreamResponse,
            HTTPNotFound=web.HTTPNotFound,
            HTTPRequestRangeNotSatisfiable=web.HTTPRequestRangeNotSatisfiable,
        ),
    )
    await controller.serve_queue_flow_stream(request)
    controller.mass.player_queues.restart_lost_flow_stream.return_value = True
    request.headers = {"Range": "bytes=50000-"}
    request.http_range = slice(50_000, None)
    controller.audio.select_flow_pcm_format = AsyncMock()

    with pytest.raises(web.HTTPRequestRangeNotSatisfiable):
        await controller.serve_queue_flow_stream(request)

    controller.mass.player_queues.restart_lost_flow_stream.assert_called_once_with(
        "queue-1", "session-1"
    )
    controller.audio.select_flow_pcm_format.assert_not_awaited()
