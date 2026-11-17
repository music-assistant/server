"""Tests that a track shorter than the 5 second report floor is counted when it plays to its end."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, Mock

from music_assistant_models.enums import PlaybackState
from music_assistant_models.media_items import ProviderMapping, Track
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.helpers import CompareState
from music_assistant.controllers.player_queues.state import PlayerQueueData

QUEUE_ID = "squeezelite-1"
PROVIDER = "filesystem_local--1"
SHORT = 4
LONG = 180


def _track(item_id: str, duration: int = SHORT) -> QueueItem:
    return QueueItem(
        queue_id=QUEUE_ID,
        queue_item_id=item_id,
        name=item_id,
        duration=duration,
        media_item=Track(
            item_id=item_id,
            provider=PROVIDER,
            name=item_id,
            duration=duration,
            provider_mappings={
                ProviderMapping(
                    item_id=item_id, provider_domain="filesystem_local", provider_instance=PROVIDER
                )
            },
        ),
    )


def _controller(items: list[QueueItem]) -> PlayerQueuesController:
    """Build a bare controller holding the tracks, with everything beyond the tracker stubbed."""
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.logger = MagicMock()
    queue = PlayerQueue(
        queue_id=QUEUE_ID, active=True, display_name="Kitchen", available=True, items=len(items)
    )
    # the session the items were loaded in
    ctrl._queue_data = {
        QUEUE_ID: PlayerQueueData(
            queue=queue, items=items, session_id="s0", started_session_id="s0"
        )
    }
    ctrl.signal_update = Mock()  # type: ignore[method-assign]
    ctrl._preload_next_item = Mock()  # type: ignore[method-assign]
    ctrl._cleanup_stale_queue_buffers = Mock()  # type: ignore[method-assign]
    ctrl._load_item = AsyncMock()  # type: ignore[method-assign]
    ctrl._check_player_permission = Mock()  # type: ignore[method-assign]
    ctrl._set_transitioning = Mock()  # type: ignore[method-assign]
    ctrl.player_media_from_queue_item = AsyncMock()  # type: ignore[method-assign]
    ctrl.mark_ended = Mock()  # type: ignore[method-assign]
    ctrl.mass = MagicMock()
    ctrl.mass.players.get_player.return_value.offline_by_power_control = False
    ctrl.mass.players.get_player.return_value.extra_data = {}
    ctrl.mass.streams.is_smart_fades_active.return_value = False
    ctrl.mass.players.play_media = AsyncMock()
    ctrl.mass.players._handle_cmd_stop = AsyncMock()
    # the tasks the tracker starts (end of queue settling, cleanups) are not run
    ctrl.mass.create_task.side_effect = lambda target, *_args, **_kwargs: (
        target.close() if hasattr(target, "close") else None
    )
    return ctrl


def _player_reports(
    ctrl: PlayerQueuesController, state: PlaybackState, item_id: str, elapsed: float
) -> None:
    """Feed a player update naming the item it plays and its position in it."""
    player = SimpleNamespace(
        player_id=QUEUE_ID,
        resolve_output_protocol=lambda: SimpleNamespace(
            current_media=SimpleNamespace(source_id=QUEUE_ID, queue_item_id=item_id, uri=None)
        ),
        name="Kitchen",
        available=True,
        playback_state=state,
        corrected_elapsed_time=elapsed,
        group_members=[],
    )
    ctrl._update_queue_from_player(player)  # type: ignore[arg-type]


def _plays(ctrl: PlayerQueuesController, item_id: str, *positions: float) -> None:
    """Have the player fetch an item and report it playing at the given positions."""
    ctrl.track_loaded_in_buffer(QUEUE_ID, item_id)
    for position in positions:
        _player_reports(ctrl, PlaybackState.PLAYING, item_id, position)


def _reported(ctrl: PlayerQueuesController) -> list[tuple[str, bool]]:
    """Return the (item id, fully played) pairs handed to the music controller."""
    mark_item_played = cast("Mock", ctrl.mass.music.mark_item_played)
    return [
        (call.args[0].item_id, call.kwargs["fully_played"])
        for call in mark_item_played.call_args_list
    ]


def test_short_track_at_the_end_of_the_queue_is_counted() -> None:
    """A 4 s track whose last report was 3 s counts as fully played when the queue ends."""
    ctrl = _controller([_track("a")])
    _plays(ctrl, "a", 0, 3)
    _player_reports(ctrl, PlaybackState.IDLE, "a", 3)

    assert _reported(ctrl) == [("a", True)]


async def test_short_track_at_the_end_is_counted_once_when_another_item_starts() -> None:
    """The next item starting reports the ended track again, which is neither recounted nor demoted."""
    ctrl = _controller([_track("a")])
    _plays(ctrl, "a", 0, 3)
    _player_reports(ctrl, PlaybackState.IDLE, "a", 3)
    queue_data = ctrl._queue_data[QUEUE_ID]
    queue_data.items.append(_track("b", LONG))

    await ctrl.play_index(QUEUE_ID, 1)
    _plays(ctrl, "b", 0)

    assert _reported(ctrl) == [("a", True)]


def test_short_track_mid_queue_is_counted_when_the_queue_advances() -> None:
    """A 4 s track the queue advances past on its own at 3 s counts as fully played."""
    ctrl = _controller([_track("a"), _track("b", LONG)])
    _plays(ctrl, "a", 0, 3)
    _plays(ctrl, "b", 0)

    assert _reported(ctrl) == [("a", True)]


async def test_short_track_cut_off_by_loading_another_item_is_not_counted() -> None:
    """A 4 s track another item is started over at 2 s did not play to its end."""
    ctrl = _controller([_track("a"), _track("b"), _track("c", LONG)])
    _plays(ctrl, "a", 0, 2)

    await ctrl.play_index(QUEUE_ID, 2)
    _plays(ctrl, "c", 0)

    assert _reported(ctrl) == []


async def test_short_track_stopped_by_command_is_not_counted() -> None:
    """A 4 s track stopped at 3 s is not counted, also not once another item starts."""
    ctrl = _controller([_track("a"), _track("b", LONG)])
    _plays(ctrl, "a", 0, 3)

    await ctrl.stop(QUEUE_ID)
    _player_reports(ctrl, PlaybackState.IDLE, "a", 3)
    await ctrl.play_index(QUEUE_ID, 1)
    _plays(ctrl, "b", 0)

    assert _reported(ctrl) == []


def test_zero_second_bounce_of_a_long_track_is_not_counted() -> None:
    """A long track reported with 0 s as the previous item is not recorded."""
    ctrl = _controller([_track("a", LONG), _track("b", LONG)])
    _plays(ctrl, "a", 0)
    _plays(ctrl, "b", 0)

    assert _reported(ctrl) == []


async def test_long_track_skipped_early_is_not_counted() -> None:
    """A long track skipped at 3 s is not recorded."""
    ctrl = _controller([_track("a", LONG), _track("b", LONG)])
    _plays(ctrl, "a", 0, 3)

    await ctrl.play_index(QUEUE_ID, 1)
    _plays(ctrl, "b", 0)

    assert _reported(ctrl) == []


def test_short_track_crossed_in_a_flow_stream_is_counted() -> None:
    """A flow stream crossing from a 4 s track into the next one counts it as fully played."""

    def state(item: QueueItem, elapsed: int) -> CompareState:
        return CompareState(
            queue_id=QUEUE_ID,
            state=PlaybackState.PLAYING,
            current_item_id=item.queue_item_id,
            next_item_id=None,
            current_item=item,
            elapsed_time=elapsed,
            last_playing_elapsed_time=elapsed,
            stream_title=None,
            codec_type=None,
            output_player_ids=None,
        )

    items = [_track("a"), _track("b", LONG)]
    ctrl = _controller(items)
    queue = ctrl._queue_data[QUEUE_ID].queue
    queue.flow_mode = True
    queue.state = PlaybackState.PLAYING
    ctrl.mark_item_served(QUEUE_ID, "a")
    ctrl.mark_item_served(QUEUE_ID, "b")

    ctrl._handle_playback_progress_report(queue, state(items[0], 3), state(items[1], 0))

    assert _reported(ctrl) == [("a", True)]


def test_short_track_with_a_late_last_report_is_counted() -> None:
    """A 4 s track whose player last reported 2 s still counts when the queue advances."""
    ctrl = _controller([_track("a"), _track("b", LONG)])
    _plays(ctrl, "a", 0, 2)
    _plays(ctrl, "b", 0)

    assert _reported(ctrl) == [("a", True)]


def test_short_track_cut_off_by_clearing_the_queue_is_not_counted() -> None:
    """A 4 s track whose queue is cleared at 2 s did not play to its end."""
    ctrl = _controller([_track("a")])
    ctrl.store_sources = Mock()  # type: ignore[method-assign]
    ctrl.update_items = Mock()  # type: ignore[method-assign]
    _plays(ctrl, "a", 0, 2)

    ctrl._clear(QUEUE_ID)
    _player_reports(ctrl, PlaybackState.IDLE, "a", 2)

    assert _reported(ctrl) == []


async def test_short_track_played_twice_in_a_row_is_counted_twice() -> None:
    """A 4 s track started over after it ended is another play of it."""
    ctrl = _controller([_track("a")])
    _plays(ctrl, "a", 0, 3)
    _player_reports(ctrl, PlaybackState.IDLE, "a", 3)

    await ctrl.play_index(QUEUE_ID, 0)
    _plays(ctrl, "a", 0, 3)
    _player_reports(ctrl, PlaybackState.IDLE, "a", 3)

    assert _reported(ctrl) == [("a", True), ("a", True)]
