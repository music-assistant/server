"""Tests for refusing single-item stream requests that fell out of the queue's window."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from aiohttp import web

from music_assistant.controllers.streams.controller import StreamsController

QUEUE_ID = "q1"
SESSION_ID = "sess1"
ITEM_ID = "item1"
PLAYER_ID = "player1"


def _make_request() -> MagicMock:
    """Build a GET request for the single-item stream route."""
    request = MagicMock()
    request.method = "GET"
    request.match_info = {
        "queue_id": QUEUE_ID,
        "player_id": PLAYER_ID,
        "session_id": SESSION_ID,
        "queue_item_id": ITEM_ID,
        "fmt": "flac",
    }
    return request


def _make_controller(*, strict_player: bool, item_in_window: bool) -> MagicMock:
    """Build a bare streams controller wired up to just past the request validation."""
    ctrl = MagicMock()
    ctrl.logger = Mock()
    ctrl._log_request = Mock()
    ctrl._open_item_streams = {}
    ctrl.mass = MagicMock()

    queue = MagicMock()
    queue.queue_id = QUEUE_ID
    ctrl.mass.player_queues.get.return_value = queue
    pq_data = MagicMock()
    pq_data.session_id = SESSION_ID
    ctrl.mass.player_queues.queue_data.return_value = pq_data
    ctrl.mass.player_queues.is_current_window_item.return_value = item_in_window

    player = MagicMock()
    player.strict_queue_item_requests = strict_player
    ctrl.mass.players.get_player.return_value = player

    queue_item = MagicMock()
    queue_item.media_item = None
    queue_item.streamdetails = None
    ctrl.mass.player_queues.get_item.return_value = queue_item

    # first dependency past the gate: failing it proves the gate let the request through
    ctrl.audio = MagicMock()
    ctrl.audio.get_stream_details = AsyncMock(side_effect=RuntimeError("stopped past the gate"))
    return ctrl


async def test_stale_item_request_is_refused_for_a_strict_player() -> None:
    """A strict player asking for a track away from the playhead gets a 404, untouched state."""
    ctrl = _make_controller(strict_player=True, item_in_window=False)

    with pytest.raises(web.HTTPNotFound) as exc:
        await StreamsController.serve_queue_item_stream(ctrl, _make_request())

    assert "not up next" in str(exc.value.reason)
    # refused before anything was registered or claimed
    assert ctrl._open_item_streams == {}
    ctrl.audio.get_stream_details.assert_not_awaited()
    ctrl.mass.player_queues.track_loaded_in_buffer.assert_not_called()


async def test_window_item_request_passes_the_gate() -> None:
    """A strict player asking for a track at the playhead is served."""
    ctrl = _make_controller(strict_player=True, item_in_window=True)

    with pytest.raises(web.HTTPNotFound) as exc:
        await StreamsController.serve_queue_item_stream(ctrl, _make_request())

    assert "No streamdetails" in str(exc.value.reason)


async def test_relaxed_player_is_served_without_a_window_check() -> None:
    """Players that do not opt in keep the old behaviour: any known item is served."""
    ctrl = _make_controller(strict_player=False, item_in_window=False)

    with pytest.raises(web.HTTPNotFound) as exc:
        await StreamsController.serve_queue_item_stream(ctrl, _make_request())

    assert "No streamdetails" in str(exc.value.reason)
    ctrl.mass.player_queues.is_current_window_item.assert_not_called()
