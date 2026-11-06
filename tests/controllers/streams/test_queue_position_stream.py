"""Tests for stream requests that address a queue item by its queue position."""

from __future__ import annotations

from types import MethodType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from aiohttp import web
from music_assistant_models.enums import MediaType
from music_assistant_models.player import PlayerMedia

from music_assistant.controllers.streams.controller import StreamsController

QUEUE_ID = "q1"
SESSION_ID = "sess1"
ITEM_ID = "item1"
PLAYER_ID = "player1"


def _make_request(method: str = "GET", **address: str) -> MagicMock:
    """Build a request for a single-item stream route, addressed by position or by item id."""
    request = MagicMock()
    request.method = method
    request.match_info = {
        "queue_id": QUEUE_ID,
        "player_id": PLAYER_ID,
        "session_id": SESSION_ID,
        "fmt": "flac",
        **address,
    }
    return request


def _make_controller() -> MagicMock:
    """Build a bare streams controller wired up to just past the request validation."""
    ctrl = MagicMock()
    ctrl.logger = Mock()
    ctrl._log_request = Mock()
    ctrl._open_item_streams = {}
    ctrl._ended_item_streams = {}
    for helper in ("_requested_item_ids", "_ended_item_ids", "_release_abandoned_item_sources"):
        setattr(ctrl, helper, MethodType(getattr(StreamsController, helper), ctrl))
    ctrl.mass = MagicMock()

    queue = MagicMock()
    queue.queue_id = QUEUE_ID
    ctrl.mass.player_queues.get.return_value = queue
    pq_data = MagicMock()
    pq_data.session_id = SESSION_ID
    pq_data.last_served_item_id = None
    ctrl.mass.player_queues.queue_data.return_value = pq_data

    queue_item = MagicMock()
    queue_item.queue_item_id = ITEM_ID
    queue_item.media_item = None
    queue_item.streamdetails = None
    # the item sits at position 2; by its id it is found too
    ctrl.mass.player_queues.get_item.side_effect = lambda _queue_id, index_or_id: (
        queue_item if index_or_id in (2, ITEM_ID) else None
    )

    # first dependency past the resolution: failing it proves the request got that far
    ctrl.audio = MagicMock()
    ctrl.audio.get_stream_details = AsyncMock(side_effect=RuntimeError("stopped past resolution"))
    return ctrl


async def test_a_position_request_is_served_the_item_sitting_there_now() -> None:
    """A position resolves to the item the queue holds there now."""
    ctrl = _make_controller()
    request = _make_request(queue_index="2")

    with pytest.raises(web.HTTPNotFound) as exc:
        await StreamsController.serve_queue_item_stream(ctrl, request)

    assert "No streamdetails" in str(exc.value.reason)
    ctrl.mass.player_queues.get_item.assert_called_once_with(QUEUE_ID, 2)
    player = ctrl.mass.players.get_player.return_value
    player.on_queue_position_served.assert_called_once_with(2, ITEM_ID)
    # the sweeps over open requests read the item from the request itself
    request.__setitem__.assert_called_once_with("queue_item_id", ITEM_ID)


async def test_a_position_past_the_end_of_the_queue_is_refused() -> None:
    """A position the queue no longer reaches gets a 404."""
    ctrl = _make_controller()

    with pytest.raises(web.HTTPNotFound) as exc:
        await StreamsController.serve_queue_item_stream(ctrl, _make_request(queue_index="7"))

    assert "No queue item at position 7" in str(exc.value.reason)
    ctrl.audio.get_stream_details.assert_not_awaited()
    ctrl.mass.players.get_player.return_value.on_queue_position_served.assert_not_called()


async def test_an_item_addressed_by_id_is_served_without_a_position_check() -> None:
    """Any item the queue holds is served when asked for by id, wherever it sits."""
    ctrl = _make_controller()

    with pytest.raises(web.HTTPNotFound) as exc:
        await StreamsController.serve_queue_item_stream(ctrl, _make_request(queue_item_id=ITEM_ID))

    assert "No streamdetails" in str(exc.value.reason)


async def test_an_unknown_item_id_is_refused() -> None:
    """An item the queue does not hold at all is the one thing an id request is refused."""
    ctrl = _make_controller()

    with pytest.raises(web.HTTPNotFound) as exc:
        await StreamsController.serve_queue_item_stream(ctrl, _make_request(queue_item_id="gone"))

    assert "Unknown Queue item: gone" in str(exc.value.reason)


def _url_controller() -> Any:
    """Return a streams controller with one gapless, non-flow HTTP player."""
    controller: Any = StreamsController.__new__(StreamsController)
    controller._base_url = "http://mass:8097"
    controller._flow_mode_owners = {}
    controller._boundary_mix_owners = {}
    controller.mass = mass = MagicMock()
    player = MagicMock()
    player.config.get_value.side_effect = lambda _key, default=None: default
    player.flow_mode = False
    player.supports_gapless = True
    mass.players.get_player.return_value = player
    mass.player_queues.get.return_value = SimpleNamespace(
        queue_id="queue-1",
        crossfade_enabled=False,
        overlay_enabled=False,
        overlay_source=None,
    )
    return controller


def _media() -> PlayerMedia:
    """Return queue media with the data needed to resolve its stream."""
    return PlayerMedia(
        uri="library://track/1",
        media_type=MediaType.TRACK,
        source_id="queue-1",
        queue_item_id="item-1",
        queue_session_id="session-1",
    )


async def test_a_queue_position_resolves_to_the_position_url() -> None:
    """Media resolved for a queue position gets a URL that names the position, not the item."""
    controller = _url_controller()

    url = await controller.resolve_stream_url("player-1", _media(), queue_index=3)

    assert url == "http://mass:8097/single/session-1/queue-1/pos/3/player-1.flac"


async def test_a_flow_mode_queue_ignores_the_queue_position() -> None:
    """A queue played as one stream has no per-item fetches, so its URL stays the flow URL."""
    controller = _url_controller()
    controller.set_flow_mode_required("queue-1", "ai_radio", True)

    url = await controller.resolve_stream_url("player-1", _media(), queue_index=3)

    assert url == "http://mass:8097/flow/session-1/queue-1/item-1/player-1.flac"


async def test_a_head_probe_does_not_bind_the_position() -> None:
    """A HEAD probe is speculative, so it does not bind the position to what the player plays."""
    ctrl = _make_controller()

    with pytest.raises(web.HTTPNotFound):
        await StreamsController.serve_queue_item_stream(
            ctrl, _make_request("HEAD", queue_index="2")
        )

    ctrl.mass.players.get_player.return_value.on_queue_position_served.assert_not_called()


def test_the_sweeps_read_a_flow_request_by_the_item_its_url_names() -> None:
    """A flow request, registered without a resolved item, counts by the item in its URL."""
    ctrl = _make_controller()
    request = MagicMock()
    request.get.return_value = None
    request.method = "GET"
    request.match_info = {"queue_item_id": ITEM_ID}
    ctrl._open_item_streams[QUEUE_ID] = [(SESSION_ID, request)]

    assert ctrl._requested_item_ids(QUEUE_ID, SESSION_ID) == {ITEM_ID}
