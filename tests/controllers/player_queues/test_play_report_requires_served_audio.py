"""Tests that only an item whose audio actually reached the player is reported or ends the queue."""

from __future__ import annotations

from collections.abc import Coroutine
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from music_assistant_models.enums import PlaybackState
from music_assistant_models.media_items import ProviderMapping, Track
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData

QUEUE_ID = "sonos-1"
PROVIDER = "spotify--1"
DURATION = 200


def _track(item_id: str) -> QueueItem:
    return QueueItem(
        queue_id=QUEUE_ID,
        queue_item_id=item_id,
        name=item_id,
        duration=DURATION,
        media_item=Track(
            item_id=item_id,
            provider=PROVIDER,
            name=item_id,
            duration=DURATION,
            provider_mappings={
                ProviderMapping(
                    item_id=item_id, provider_domain="spotify", provider_instance=PROVIDER
                )
            },
        ),
    )


def _controller(items: list[QueueItem]) -> PlayerQueuesController:
    """Build a bare controller holding the tracks, with everything beyond the tracker stubbed."""
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.logger = MagicMock()
    queue = PlayerQueue(
        queue_id=QUEUE_ID, active=True, display_name="Sonos", available=True, items=len(items)
    )
    ctrl._queue_data = {QUEUE_ID: PlayerQueueData(queue=queue, items=items)}
    ctrl.signal_update = Mock()  # type: ignore[method-assign]
    ctrl._preload_next_item = Mock()  # type: ignore[method-assign]
    ctrl._cleanup_stale_queue_buffers = Mock()  # type: ignore[method-assign]
    ctrl._load_item = AsyncMock()  # type: ignore[method-assign]
    ctrl._check_player_permission = Mock()  # type: ignore[method-assign]
    ctrl._set_transitioning = Mock()  # type: ignore[method-assign]
    ctrl.player_media_from_queue_item = AsyncMock()  # type: ignore[method-assign]
    ctrl.mark_ended = Mock()  # type: ignore[method-assign]
    ctrl.mass = MagicMock()
    ctrl.mass.streams.is_smart_fades_active.return_value = False
    ctrl.mass.players.play_media = AsyncMock()
    return ctrl


def _player_reports(
    ctrl: PlayerQueuesController, state: PlaybackState, item_id: str, elapsed: float
) -> None:
    """Feed a player update naming the item it plays and its position in it."""
    player = SimpleNamespace(
        player_id=QUEUE_ID,
        active_output_protocol=None,
        current_media=SimpleNamespace(source_id=QUEUE_ID, queue_item_id=item_id, uri=None),
        state=SimpleNamespace(
            name="Sonos",
            available=True,
            playback_state=state,
            corrected_elapsed_time=elapsed,
            group_members=[],
        ),
    )
    ctrl._update_queue_from_player(player)  # type: ignore[arg-type]


def _reported(ctrl: PlayerQueuesController) -> list[tuple[str, bool]]:
    """Return the (item id, fully played) pairs handed to the music controller."""
    mark_item_played = cast("Mock", ctrl.mass.music.mark_item_played)
    return [
        (call.args[0].item_id, call.kwargs["fully_played"])
        for call in mark_item_played.call_args_list
    ]


def _settle_task(ctrl: PlayerQueuesController) -> Coroutine[Any, Any, None] | None:
    """Return the end-of-queue settle task the tracker scheduled, if any."""
    for call in cast("Mock", ctrl.mass.create_task).call_args_list:
        if call.kwargs.get("task_name") == f"settle_or_resume_{QUEUE_ID}":
            return cast("Coroutine[Any, Any, None]", call.args[0])
    return None


def test_item_the_player_named_but_never_received_is_not_reported() -> None:
    """A track the player reports from its cached queue without getting its audio earns no play."""
    ctrl = _controller([_track("a"), _track("b"), _track("c")])
    # the player fetches the first track and plays it to near its end
    ctrl.track_loaded_in_buffer(QUEUE_ID, "a")
    _player_reports(ctrl, PlaybackState.PLAYING, "a", 0)
    _player_reports(ctrl, PlaybackState.PLAYING, "a", 190)
    # its request for the next track is refused, yet it names that track as playing (at a
    # position carried over from before) and then gives up
    _player_reports(ctrl, PlaybackState.PLAYING, "b", 195)
    _player_reports(ctrl, PlaybackState.IDLE, "b", 195)

    assert _reported(ctrl) == [("a", True)]
    assert not any(
        call.kwargs["object_id"] == "spotify--1://track/b"
        for call in cast("Mock", ctrl.mass.signal_event).call_args_list
    )


def test_served_item_is_reported_when_the_player_stops_at_its_end() -> None:
    """A track whose audio went out is still credited when the player stops after it."""
    ctrl = _controller([_track("a"), _track("b")])
    ctrl.track_loaded_in_buffer(QUEUE_ID, "a")
    _player_reports(ctrl, PlaybackState.PLAYING, "a", 0)
    _player_reports(ctrl, PlaybackState.PLAYING, "a", 195)
    _player_reports(ctrl, PlaybackState.IDLE, "a", 195)

    assert _reported(ctrl) == [("a", True)]


async def test_a_new_load_keeps_the_playing_item_eligible() -> None:
    """The track still playing when another one is started is reported once the player leaves it."""
    ctrl = _controller([_track("a"), _track("b"), _track("c")])
    ctrl.track_loaded_in_buffer(QUEUE_ID, "a")
    _player_reports(ctrl, PlaybackState.PLAYING, "a", 0)
    _player_reports(ctrl, PlaybackState.PLAYING, "a", 190)
    # the player also fetched the next track ahead of time
    ctrl.track_loaded_in_buffer(QUEUE_ID, "b")

    await ctrl.play_index(QUEUE_ID, 2)

    # only the track the player is still playing survives the new load
    assert ctrl._queue_data[QUEUE_ID].served_item_ids == {"a"}
    ctrl.track_loaded_in_buffer(QUEUE_ID, "c")
    _player_reports(ctrl, PlaybackState.PLAYING, "c", 0)
    assert _reported(ctrl) == [("a", True)]


def test_never_received_last_item_does_not_end_the_queue() -> None:
    """A refused last track the player names before stopping leaves the queue where it is."""
    ctrl = _controller([_track("a"), _track("b")])
    ctrl.track_loaded_in_buffer(QUEUE_ID, "a")
    _player_reports(ctrl, PlaybackState.PLAYING, "a", 0)
    _player_reports(ctrl, PlaybackState.PLAYING, "a", 190)
    # the request for the last track is refused, yet the player names it (at a position carried
    # over from before) and then stops
    _player_reports(ctrl, PlaybackState.PLAYING, "b", 195)
    _player_reports(ctrl, PlaybackState.IDLE, "b", 195)

    assert _settle_task(ctrl) is None
    cast("Mock", ctrl.mark_ended).assert_not_called()


async def test_served_last_item_played_to_its_end_ends_the_queue() -> None:
    """The queue still ends after a last track whose audio was served played out."""
    ctrl = _controller([_track("a"), _track("b")])
    ctrl.track_loaded_in_buffer(QUEUE_ID, "a")
    _player_reports(ctrl, PlaybackState.PLAYING, "a", 0)
    _player_reports(ctrl, PlaybackState.PLAYING, "a", 190)
    ctrl.track_loaded_in_buffer(QUEUE_ID, "b")
    _player_reports(ctrl, PlaybackState.PLAYING, "b", 0)
    _player_reports(ctrl, PlaybackState.PLAYING, "b", 196)
    _player_reports(ctrl, PlaybackState.IDLE, "b", 196)

    settle = _settle_task(ctrl)
    assert settle is not None
    with patch(
        "music_assistant.controllers.player_queues.playback_tracker.asyncio.sleep", AsyncMock()
    ):
        await settle
    cast("Mock", ctrl.mark_ended).assert_called_once_with(QUEUE_ID)
