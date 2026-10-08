"""Tests for resuming a queue item that a player re-requests while it is still streaming it."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import cast
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from music_assistant_models.enums import CrossfadeMode, MediaType

from music_assistant.controllers.streams.controller import StreamsController

QUEUE_ID = "q1"
SESSION_ID = "sess1"
ITEM_ID = "item1"
PLAYER_ID = "player1"
START_SEEK = 8
LIVE_ELAPSED = 150.4


def _make_request(session_id: str = SESSION_ID) -> MagicMock:
    """Build a GET request for the single-item stream route."""
    request = MagicMock()
    request.method = "GET"
    request.match_info = {
        "queue_id": QUEUE_ID,
        "player_id": PLAYER_ID,
        "session_id": session_id,
        "queue_item_id": ITEM_ID,
        "fmt": "flac",
    }
    return request


def _make_controller() -> StreamsController:
    """Build a streams controller with just enough state to serve one queue item."""
    ctrl = object.__new__(StreamsController)
    ctrl.logger = Mock()
    ctrl._open_item_streams = {}
    ctrl._active_output_streams = 0
    ctrl.get_crossfade_mode = Mock(return_value=CrossfadeMode.DISABLED)  # type: ignore[method-assign]
    ctrl._update_audio_processing_context = Mock()  # type: ignore[method-assign]
    ctrl.mass = MagicMock()
    ctrl.mass.config.get_raw_core_config_value.return_value = 8

    queue_item = MagicMock()
    queue_item.queue_item_id = ITEM_ID
    queue_item.name = "Track"
    queue_item.media_item = None
    queue_item.media_type = MediaType.TRACK
    queue_item.duration = 300
    queue_item.extra_attributes = {}
    queue_item.streamdetails = MagicMock(
        seek_position=START_SEEK, duration=300, is_realtime=False, stream_error=None
    )
    queue_item.streamdetails.seconds_streamed = 0
    ctrl.mass.player_queues.get_item.return_value = queue_item
    ctrl.mass.player_queues.get_next_item.return_value = None

    queue = MagicMock()
    queue.queue_id = QUEUE_ID
    queue.display_name = "Living room"
    queue.overlay_enabled = False
    queue.current_item = queue_item
    queue.corrected_elapsed_time = LIVE_ELAPSED
    ctrl.mass.player_queues.get.return_value = queue
    ctrl.mass.player_queues.queue_data.return_value = MagicMock(session_id=SESSION_ID)

    player = MagicMock()
    player.strict_queue_item_requests = False
    player.get_config_value.return_value = "default"
    ctrl.mass.players.get_player.return_value = player

    ctrl.audio = MagicMock()
    ctrl.audio.select_pcm_format = AsyncMock(return_value=MagicMock())
    ctrl.audio.get_output_format = AsyncMock(return_value=MagicMock(output_format_str="flac"))
    ctrl.audio.get_player_output_plan.return_value = MagicMock(filter_params=[])
    return ctrl


async def _no_audio(*_args: object, **_kwargs: object) -> AsyncGenerator[bytes]:
    """Yield no audio at all."""
    chunks: tuple[bytes, ...] = ()
    for chunk in chunks:
        yield chunk


async def _serve(ctrl: StreamsController, request: MagicMock) -> int:
    """Serve the request and return the seek position the item stream was built from."""
    response = MagicMock()
    response.prepare = AsyncMock()
    with (
        patch("aiohttp.web.StreamResponse", return_value=response),
        patch("music_assistant.controllers.streams.controller.get_ffmpeg_stream", _no_audio),
    ):
        await ctrl.serve_queue_item_stream(request)
    audio = cast("MagicMock", ctrl.audio)
    seek_position: int = audio.get_queue_item_stream.call_args.kwargs["seek_position"]
    return seek_position


def _open_stream(ctrl: StreamsController, session_id: str = SESSION_ID) -> None:
    """Register a still-open GET for the same item, as the player's first connection."""
    first = _make_request(session_id)
    ctrl._open_item_streams[QUEUE_ID] = [(session_id, first)]


async def test_rerequest_of_the_playing_item_resumes_at_the_live_position() -> None:
    """A second GET while the first is still open picks up where playback is now."""
    ctrl = _make_controller()
    _open_stream(ctrl)

    assert await _serve(ctrl, _make_request()) == int(LIVE_ELAPSED)

    queue_item = cast("MagicMock", ctrl.mass).player_queues.get_item.return_value
    assert queue_item.streamdetails.seek_position == int(LIVE_ELAPSED)


async def test_repeat_of_the_same_item_starts_from_its_start_offset() -> None:
    """With the same item up next (repeat one), the second GET is the repeat, not a resume."""
    ctrl = _make_controller()
    _open_stream(ctrl)
    player_queues = cast("MagicMock", ctrl.mass).player_queues
    player_queues.get_next_item.return_value = player_queues.get_item.return_value
    queue_item = player_queues.get_item.return_value

    assert await _serve(ctrl, _make_request()) == START_SEEK
    assert queue_item.streamdetails.seek_position == START_SEEK


async def test_fresh_request_starts_from_its_start_offset() -> None:
    """Without another open stream for the item, the request is served from its start offset."""
    ctrl = _make_controller()

    assert await _serve(ctrl, _make_request()) == START_SEEK


async def test_open_stream_of_another_session_is_not_a_rerequest() -> None:
    """A lingering response of a previous session does not make the new one resume."""
    ctrl = _make_controller()
    _open_stream(ctrl, session_id="old-session")

    assert await _serve(ctrl, _make_request()) == START_SEEK
