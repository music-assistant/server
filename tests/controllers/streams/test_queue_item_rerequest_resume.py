"""Tests for resuming a queue item that a player re-requests while it is still playing it."""

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
    ctrl._started_item_streams = {}
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
        seek_position=START_SEEK,
        duration=300,
        fade_in=True,
        is_realtime=False,
        stream_error=None,
        seconds_streamed=0,
    )
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


def _queue_item(ctrl: StreamsController) -> MagicMock:
    """Return the queue item the controller serves."""
    return cast("MagicMock", ctrl.mass).player_queues.get_item.return_value  # type: ignore[no-any-return]


async def _serve(
    ctrl: StreamsController, request: MagicMock, chunks: tuple[bytes, ...] = ()
) -> int:
    """Serve the request and return the seek position the item stream was built from."""

    async def _audio(*_args: object, **_kwargs: object) -> AsyncGenerator[bytes]:
        for chunk in chunks:
            yield chunk

    response = MagicMock()
    response.prepare = AsyncMock()
    response.write = AsyncMock()
    with (
        patch("aiohttp.web.StreamResponse", return_value=response),
        patch("music_assistant.controllers.streams.controller.get_ffmpeg_stream", _audio),
    ):
        await ctrl.serve_queue_item_stream(request)
    audio = cast("MagicMock", ctrl.audio)
    seek_position: int = audio.get_queue_item_stream.call_args.kwargs["seek_position"]
    return seek_position


async def _play_first_response(ctrl: StreamsController, session_id: str = SESSION_ID) -> None:
    """Serve and finish the player's first response for the item in the given session."""
    cast("MagicMock", ctrl.mass).player_queues.queue_data.return_value.session_id = session_id
    assert await _serve(ctrl, _make_request(session_id), chunks=(b"audio",)) == START_SEEK


async def test_rerequest_after_the_first_response_finished_resumes_at_the_live_position() -> None:
    """A second GET for an item already played in this session picks up where playback is now."""
    ctrl = _make_controller()
    await _play_first_response(ctrl)

    assert await _serve(ctrl, _make_request()) == int(LIVE_ELAPSED)

    streamdetails = _queue_item(ctrl).streamdetails
    assert streamdetails.seek_position == int(LIVE_ELAPSED)
    assert streamdetails.fade_in is False
    player_queues = cast("MagicMock", ctrl.mass).player_queues
    player_queues.begin_stream_resume.assert_called_once_with(
        QUEUE_ID, ITEM_ID, START_SEEK, int(LIVE_ELAPSED)
    )


async def test_repeat_of_the_same_item_starts_from_its_start_offset() -> None:
    """With the same item up next (repeat one), the second GET is the repeat, not a resume."""
    ctrl = _make_controller()
    await _play_first_response(ctrl)
    player_queues = cast("MagicMock", ctrl.mass).player_queues
    player_queues.get_next_item.return_value = _queue_item(ctrl)

    assert await _serve(ctrl, _make_request()) == START_SEEK
    player_queues.begin_stream_resume.assert_not_called()


async def test_request_in_a_new_session_starts_from_its_start_offset() -> None:
    """A user seek rotates the session, so its request keeps the offset it was loaded with."""
    ctrl = _make_controller()
    await _play_first_response(ctrl, session_id="old-session")
    cast("MagicMock", ctrl.mass).player_queues.queue_data.return_value.session_id = SESSION_ID

    assert await _serve(ctrl, _make_request()) == START_SEEK


async def test_rerequest_right_after_the_start_keeps_the_start_offset() -> None:
    """A player opening the url twice right after a track start gets the start offset both times."""
    ctrl = _make_controller()
    await _play_first_response(ctrl)
    queue = cast("MagicMock", ctrl.mass).player_queues.get.return_value
    queue.corrected_elapsed_time = START_SEEK + 4.9
    # the first connection is still open when the second request arrives
    ctrl._open_item_streams[QUEUE_ID] = [(SESSION_ID, _make_request())]

    assert await _serve(ctrl, _make_request()) == START_SEEK
    cast("MagicMock", ctrl.mass).player_queues.begin_stream_resume.assert_not_called()
