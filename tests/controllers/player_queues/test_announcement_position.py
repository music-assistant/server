"""
The position a queue reports around an announcement that stops it.

The default announcement stops the queue's playback and plays the clip instead. For the length of
the clip the queue keeps its playing state, holds its position at the point it resumes from and
carries a marker for clients, and once the announcement is over it follows its player again.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any, NamedTuple, cast
from unittest.mock import AsyncMock, MagicMock, Mock, call

import pytest
from music_assistant_models.enums import (
    EventType,
    MediaType,
    PlaybackState,
    PlayerType,
    SleepTimerMode,
)
from music_assistant_models.errors import PlayerCommandFailed
from music_assistant_models.player_queue import PlayerQueue, PlayLogEntry
from music_assistant_models.queue_item import QueueItem

from music_assistant.constants import (
    ATTR_ANNOUNCEMENT_HELD_QUEUE,
    ATTR_ANNOUNCEMENT_IN_PROGRESS,
)
from music_assistant.controllers.player_queues.controller import PlayerQueuesController
from music_assistant.controllers.player_queues.helpers import CompareState
from music_assistant.controllers.player_queues.state import PlayerQueueData, SleepTimerTarget
from music_assistant.controllers.players import PlayerController
from music_assistant.controllers.players.helpers import AnnounceData

QUEUE_ID = "player_1"
TRACK_A = "track-a"
TRACK_B = "track-b"
SAVED_POSITION = 65


class _Update(NamedTuple):
    """What one QUEUE_UPDATED event said about the queue."""

    state: PlaybackState
    elapsed_time: float
    held: bool


def _item(item_id: str) -> QueueItem:
    """Return a queue item."""
    return QueueItem(queue_id=QUEUE_ID, queue_item_id=item_id, name=item_id, duration=200)


def _queue(state: PlaybackState = PlaybackState.PLAYING) -> PlayerQueue:
    """Return a queue in the middle of its second item."""
    queue = PlayerQueue(
        queue_id=QUEUE_ID, active=True, display_name="Player 1", available=True, items=2
    )
    queue.state = state
    queue.flow_mode = True
    queue.current_index = 1
    queue.current_item = _item(TRACK_B)
    queue.elapsed_time = SAVED_POSITION
    queue.elapsed_time_last_updated = time.time()
    return queue


def _player(*, state: PlaybackState = PlaybackState.PLAYING, announcement: bool = True) -> Any:
    """Return the queue's player."""
    player = MagicMock()
    player.player_id = QUEUE_ID
    player.name = "Player 1"
    player.available = True
    player.type = PlayerType.PLAYER
    player.playback_state = state
    player.active_source = QUEUE_ID
    player.fake_paused = False
    player.corrected_elapsed_time = 0.0
    player.group_members = []
    player.offline_by_power_control = False
    player.synced_to = None
    player.active_group = None
    player.resolve_output_protocol = Mock(return_value=None)
    player.extra_data = {ATTR_ANNOUNCEMENT_IN_PROGRESS: announcement}
    return player


def _mass(player: Any) -> MagicMock:
    """Return a mocked server that records the queue updates it publishes."""
    mass = MagicMock()
    mass.players.get_player = Mock(return_value=player)
    mass.players.get_player_or_protocol_player = Mock(return_value=None)
    mass.players._handle_cmd_stop = AsyncMock()
    # the published queue is the live snapshot, so what an event said is taken as it goes out
    mass.published = []

    def _publish(event: EventType, **kwargs: Any) -> None:
        if event == EventType.QUEUE_UPDATED:
            queue = kwargs["data"]
            held = bool(queue.extra_attributes.get(ATTR_ANNOUNCEMENT_IN_PROGRESS))
            mass.published.append(_Update(queue.state, queue.elapsed_time, held))

    mass.signal_event = Mock(side_effect=_publish)
    return mass


def _controller(queue: PlayerQueue, player: Any) -> PlayerQueuesController:
    """Return a queue controller whose only queue is the given one, on a mocked server."""
    data = PlayerQueueData(queue=queue)
    data.items = [_item(TRACK_A), _item(TRACK_B)]
    # a flow session whose first item was fully streamed before the second one started
    data.flow_mode_stream_log = [
        PlayLogEntry(TRACK_A, seconds_streamed=0.0, seek_position=0),
        PlayLogEntry(TRACK_B, seconds_streamed=None, seek_position=None),
    ]
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl._queue_data = {QUEUE_ID: data}
    ctrl.logger = MagicMock()
    ctrl.mass = _mass(player)
    ctrl.mass.player_queues = ctrl
    return ctrl


def _announcements(ctrl: PlayerQueuesController) -> PlayerController:
    """Return the players controller that announces on the given queue controller."""
    announcements = PlayerController.__new__(PlayerController)
    announcements.mass = ctrl.mass
    announcements.logger = MagicMock()
    announcements.get_active_queue = Mock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(queue_id=QUEUE_ID)
    )
    announcements.get_player = Mock(return_value=None)  # type: ignore[method-assign]
    announcements._wait_for_playback_state = AsyncMock()  # type: ignore[method-assign]
    announcements._unmute_and_set_announcement_volume = AsyncMock()  # type: ignore[method-assign]
    return announcements


def _fallback_announcements(ctrl: PlayerQueuesController) -> PlayerController:
    """Return the players controller, announcing through the default implementation."""
    renderer = ctrl.mass.streams.announcement_renderer
    renderer.register = Mock(  # type: ignore[method-assign]
        return_value=Mock(wait_ready=AsyncMock(return_value=True))
    )
    renderer.get = Mock(return_value=None)  # type: ignore[method-assign]
    renderer.unregister = AsyncMock()  # type: ignore[method-assign]
    ctrl.mass.streams.get_announcement_url = Mock(  # type: ignore[method-assign]
        return_value="http://mass/announcement"
    )
    announcements = _announcements(ctrl)
    announcements._resolve_ready_announce_player = AsyncMock(  # type: ignore[method-assign]
        return_value=None
    )
    announcements._handle_play_media = AsyncMock()  # type: ignore[method-assign]
    announcements._handle_cmd_resume = AsyncMock()  # type: ignore[method-assign]
    return announcements


def _announce_data() -> AnnounceData:
    """Return the announcement to play."""
    return AnnounceData(
        announcement_url="http://mass/clip.mp3",
        pre_announce=False,
        pre_announce_url="",
        announce_player_id=None,
    )


async def _prepare(announcements: PlayerController, player: Any, prev_state: PlaybackState) -> None:
    """Free up the player for an announcement, as the default announcement does."""
    await announcements._prepare_for_announcement(
        player,
        volume_level=None,
        prev_state=prev_state,
        prev_synced_to=None,
        prev_group=None,
        prev_members=[],
        prev_media_name=None,
        prev_volumes={},
        prev_muted=set(),
    )


def _prev_state(queue: PlayerQueue) -> CompareState:
    """Return the state the queue reported before an announcement took the player over."""
    return CompareState(
        queue_id=QUEUE_ID,
        state=PlaybackState.PLAYING,
        current_item_id=TRACK_B,
        next_item_id=None,
        current_item=queue.current_item,
        elapsed_time=SAVED_POSITION,
        last_playing_elapsed_time=SAVED_POSITION,
        stream_title=None,
        codec_type=None,
        output_player_ids=[QUEUE_ID],
    )


def _held(ctrl: PlayerQueuesController) -> bool:
    """Return whether the queue carries the announcement marker."""
    queue = ctrl._queue_data[QUEUE_ID].queue
    return bool(queue.extra_attributes.get(ATTR_ANNOUNCEMENT_IN_PROGRESS))


def _queue_updates(mass: Any) -> list[_Update]:
    """Return the QUEUE_UPDATED events the server published, as they went out."""
    return cast("list[_Update]", mass.published)


async def test_the_announcement_holds_a_playing_queue_at_the_position_its_stop_saved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The queue keeps playing state, carries the marker and holds the saved position."""
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now)
    queue = _queue()
    queue.elapsed_time_last_updated = now
    player = _player()
    ctrl = _controller(queue, player)

    async def _device_stop(_queue_id: str) -> None:
        # a slow device: the wall clock runs on while the stop travels to it
        nonlocal now
        now += 24.0
        # and the queue's own clock is moved on with it
        queue.elapsed_time = SAVED_POSITION + 24

    ctrl.mass.players._handle_cmd_stop = AsyncMock(  # type: ignore[method-assign]
        side_effect=_device_stop
    )

    await _prepare(_announcements(ctrl), player, PlaybackState.PLAYING)

    assert queue.state == PlaybackState.PLAYING
    assert _held(ctrl)
    assert queue.elapsed_time == SAVED_POSITION
    assert queue.elapsed_time_last_updated == now
    assert _queue_updates(ctrl.mass)[-1] == _Update(PlaybackState.PLAYING, SAVED_POSITION, True)


async def test_the_hold_is_published_to_the_player_before_its_device_reports_the_stop() -> None:
    """The player is told about the held queue, so it keeps playing across the device's stop."""
    queue = _queue()
    player = _player()
    ctrl = _controller(queue, player)

    await _prepare(_announcements(ctrl), player, PlaybackState.PLAYING)

    assert player.extra_data[ATTR_ANNOUNCEMENT_HELD_QUEUE] == QUEUE_ID
    player.update_state.assert_called_with(force_update=True)


async def test_the_player_is_marked_as_held_before_the_queue_is_stopped() -> None:
    """The stop of the device is never published, not even when it is reported at once."""
    queue = _queue()
    player = _player()
    ctrl = _controller(queue, player)
    marked_during_stop: list[object] = []

    async def _stop(*_args: Any) -> None:
        marked_during_stop.append(player.extra_data.get(ATTR_ANNOUNCEMENT_HELD_QUEUE))

    ctrl._handle_stop = _stop  # type: ignore[assignment]

    await _prepare(_announcements(ctrl), player, PlaybackState.PLAYING)

    assert marked_during_stop == [QUEUE_ID]
    assert player.extra_data[ATTR_ANNOUNCEMENT_HELD_QUEUE] == QUEUE_ID


async def test_a_queue_that_cannot_be_held_takes_the_early_mark_back() -> None:
    """A queue that is not playing any more is not held, so the player follows its device."""
    queue = _queue(PlaybackState.IDLE)
    player = _player()
    ctrl = _controller(queue, player)
    ctrl._handle_stop = AsyncMock()  # type: ignore[method-assign]

    await _prepare(_announcements(ctrl), player, PlaybackState.PLAYING)

    assert ATTR_ANNOUNCEMENT_HELD_QUEUE not in player.extra_data
    player.update_state.assert_called_with(force_update=True)


async def test_a_failing_publish_still_releases_the_queue() -> None:
    """The release does not depend on the publish before it."""
    player = _player(announcement=False)
    ctrl = _controller(_queue(), player)
    ctrl.release_after_announcement = Mock()  # type: ignore[method-assign]
    announcements = _fallback_announcements(ctrl)
    announcements._handle_play_media = AsyncMock(  # type: ignore[method-assign]
        side_effect=PlayerCommandFailed("unreachable")
    )
    # the first publish follows the hold, the second one the release
    player.update_state = Mock(side_effect=[None, RuntimeError("publish failed")])

    with pytest.raises(PlayerCommandFailed):
        await announcements._run_announcement(player, _announce_data(), None)

    ctrl.release_after_announcement.assert_called_once_with(QUEUE_ID)
    ctrl.mass.streams.announcement_renderer.unregister.assert_awaited_once()  # type: ignore[attr-defined]


async def test_the_player_is_told_when_the_announcement_releases_its_queue() -> None:
    """The player stops keeping its playing state once the queue is released."""
    queue = _queue()
    player = _player(announcement=False)
    ctrl = _controller(queue, player)
    announcements = _fallback_announcements(ctrl)
    announcements._handle_play_media = AsyncMock(  # type: ignore[method-assign]
        side_effect=PlayerCommandFailed("unreachable")
    )

    with pytest.raises(PlayerCommandFailed):
        await announcements._run_announcement(player, _announce_data(), None)

    assert ATTR_ANNOUNCEMENT_HELD_QUEUE not in player.extra_data
    # once for the hold and once for the release
    assert player.update_state.call_args_list == [call(force_update=True)] * 2


async def test_the_announcement_does_not_hold_a_queue_the_user_paused() -> None:
    """A paused queue keeps its own position and carries no marker."""
    queue = _queue(PlaybackState.PAUSED)
    queue.elapsed_time = 7
    player = _player(state=PlaybackState.PAUSED)
    ctrl = _controller(queue, player)

    await _prepare(_announcements(ctrl), player, PlaybackState.PAUSED)

    assert not _held(ctrl)
    assert queue.elapsed_time == 7


async def test_the_release_resyncs_a_queue_the_restore_did_not_play_again() -> None:
    """A queue that was not played again follows its player once the announcement is over."""
    queue = _queue()
    player = _player(announcement=False)
    ctrl = _controller(queue, player)
    ctrl._queue_data[QUEUE_ID].prev_state = _prev_state(queue)
    announcements = _fallback_announcements(ctrl)

    async def _resume(*_args: Any) -> None:
        # the device plays the item on; its updates are ignored until the announcement is over
        player.playback_state = PlaybackState.PLAYING
        player.corrected_elapsed_time = SAVED_POSITION + 3

    announcements._handle_cmd_resume = AsyncMock(side_effect=_resume)  # type: ignore[method-assign]

    await announcements._run_announcement(player, _announce_data(), None)

    assert not _held(ctrl)
    assert queue.state == PlaybackState.PLAYING
    assert queue.elapsed_time == SAVED_POSITION + 3
    assert _queue_updates(ctrl.mass)[-1] == _Update(
        PlaybackState.PLAYING, SAVED_POSITION + 3, False
    )


async def test_a_failing_announcement_releases_the_queue() -> None:
    """An announcement that fails after the stop leaves no marker behind."""
    queue = _queue()
    player = _player(announcement=False)
    ctrl = _controller(queue, player)
    announcements = _fallback_announcements(ctrl)
    announcements._handle_play_media = AsyncMock(  # type: ignore[method-assign]
        side_effect=PlayerCommandFailed("unreachable")
    )

    with pytest.raises(PlayerCommandFailed):
        await announcements._run_announcement(player, _announce_data(), None)

    assert not _held(ctrl)
    assert not _queue_updates(ctrl.mass)[-1].held


def test_the_end_of_an_announcement_that_held_nothing_publishes_nothing() -> None:
    """An announcement that did not stop the queue, as a native one, leaves it as it is."""
    ctrl = _controller(_queue(), _player(announcement=False))

    ctrl.release_after_announcement(QUEUE_ID)

    assert not _queue_updates(ctrl.mass)


async def test_a_pause_during_the_clip_keeps_the_saved_resume_position() -> None:
    """A pause while the clip plays must not move the position the queue resumes from."""
    queue = _queue()
    queue.resume_pos = SAVED_POSITION
    player = _player()
    ctrl = _controller(queue, player)
    ctrl._resolve_queue_id = Mock(return_value=QUEUE_ID)  # type: ignore[method-assign]
    ctrl._check_player_permission = Mock()  # type: ignore[method-assign]
    ctrl.mass.cancel_timer = Mock()  # type: ignore[method-assign]
    ctrl.mass.players._handle_cmd_pause = AsyncMock()  # type: ignore[method-assign]
    ctrl.mass.players._cancel_member_power_ons = Mock()  # type: ignore[method-assign]
    player.offline_by_power_control = False
    ctrl.hold_for_announcement(QUEUE_ID)
    # seven seconds of the clip have passed
    queue.elapsed_time_last_updated = time.time() - 7
    await ctrl.pause(QUEUE_ID)
    assert queue.resume_pos == SAVED_POSITION


async def test_a_stop_during_the_clip_keeps_the_saved_resume_position() -> None:
    """A stop while the clip plays must not move the position the queue resumes from."""
    queue = _queue()
    queue.resume_pos = SAVED_POSITION
    player = _player()
    ctrl = _controller(queue, player)
    ctrl._cancel_pending_playback = Mock()  # type: ignore[method-assign]
    ctrl._clear_sleep_timer = Mock()  # type: ignore[method-assign]
    ctrl.hold_for_announcement(QUEUE_ID)
    queue.elapsed_time_last_updated = time.time() - 7
    await ctrl._handle_stop(QUEUE_ID)
    assert queue.resume_pos == SAVED_POSITION


async def test_the_position_stands_still_for_the_length_of_the_clip() -> None:
    """The position the queue works with does not run on while the clip plays."""
    queue = _queue()
    queue.resume_pos = SAVED_POSITION
    ctrl = _controller(queue, _player())
    ctrl.hold_for_announcement(QUEUE_ID)
    queue.elapsed_time_last_updated = time.time() - 10

    assert ctrl._item_position(queue) == SAVED_POSITION


def test_a_restored_queue_carries_no_announcement_marker() -> None:
    """A restart ends the announcement, so a marker found in the cache is not restored."""
    queue = _queue()
    queue.extra_attributes[ATTR_ANNOUNCEMENT_IN_PROGRESS] = True
    data = PlayerQueueData(queue=queue)
    data.items = [_item(TRACK_A), _item(TRACK_B)]
    # a cache that still carries the marker
    cached = data.to_cache()
    cached["queue"].setdefault("extra_attributes", {})[ATTR_ANNOUNCEMENT_IN_PROGRESS] = True

    restored = PlayerQueueData.from_cache(cached, data.items_to_cache())

    assert ATTR_ANNOUNCEMENT_IN_PROGRESS not in restored.queue.extra_attributes


async def test_the_queue_an_announcement_held_is_released_when_it_is_not_the_players_own() -> None:
    """The marker leaves the queue the stop was issued on, also when it is another player's."""
    other_id = "other_queue"
    queue = _queue()
    queue.queue_id = other_id
    player = _player(announcement=False)
    ctrl = _controller(_queue(), player)
    ctrl._queue_data[other_id] = PlayerQueueData(queue=queue)
    ctrl._handle_stop = AsyncMock()  # type: ignore[method-assign]
    announcements = _fallback_announcements(ctrl)
    announcements.get_active_queue = Mock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(queue_id=other_id)
    )
    announcements._handle_play_media = AsyncMock(  # type: ignore[method-assign]
        side_effect=PlayerCommandFailed("unreachable")
    )

    with pytest.raises(PlayerCommandFailed):
        await announcements._run_announcement(player, _announce_data(), None)

    assert not queue.extra_attributes.get(ATTR_ANNOUNCEMENT_IN_PROGRESS)


async def test_a_failing_release_neither_hides_the_announcement_error_nor_skips_cleanup() -> None:
    """The error of the announcement is the one that surfaces, and the renderer is cleaned up."""
    player = _player(announcement=False)
    ctrl = _controller(_queue(), player)
    ctrl.release_after_announcement = Mock(  # type: ignore[method-assign]
        side_effect=RuntimeError("release failed")
    )
    announcements = _fallback_announcements(ctrl)
    announcements._handle_play_media = AsyncMock(  # type: ignore[method-assign]
        side_effect=PlayerCommandFailed("unreachable")
    )

    with pytest.raises(PlayerCommandFailed):
        await announcements._run_announcement(player, _announce_data(), None)

    ctrl.mass.streams.announcement_renderer.unregister.assert_awaited_once()  # type: ignore[attr-defined]


def test_the_sleep_timer_estimate_stands_still_for_the_length_of_the_clip() -> None:
    """The time a sleep timer needs does not shrink while the clip plays."""
    queue = _queue()
    queue.resume_pos = SAVED_POSITION
    ctrl = _controller(queue, _player())
    ctrl._queue_data[QUEUE_ID].sleep_timer_target = SleepTimerTarget(
        mode=SleepTimerMode.END_OF_ITEM, queue_item_id=TRACK_B, stop_position=100
    )
    ctrl.hold_for_announcement(QUEUE_ID)
    now = time.time()
    queue.elapsed_time_last_updated = now - 10

    expires_at = ctrl._sleep_timer_expires_at(queue)

    assert expires_at is not None
    assert expires_at == pytest.approx(now + 100 - SAVED_POSITION, abs=1)


async def test_a_change_of_the_playback_speed_during_the_clip_keeps_the_saved_position() -> None:
    """The speed change closes off the item at the held position, not at the running clock."""
    queue = _queue()
    queue.resume_pos = SAVED_POSITION
    ctrl = _controller(queue, _player())
    ctrl._resolve_queue_id = Mock(return_value=QUEUE_ID)  # type: ignore[method-assign]
    ctrl._check_player_permission = Mock()  # type: ignore[method-assign]
    ctrl.resume = AsyncMock()  # type: ignore[method-assign]
    audiobook = SimpleNamespace(
        queue_item_id=TRACK_B,
        name=TRACK_B,
        media_type=MediaType.AUDIOBOOK,
        duration=200,
        extra_attributes={},
    )
    ctrl.get_item = Mock(return_value=audiobook)  # type: ignore[method-assign]
    ctrl.hold_for_announcement(QUEUE_ID)
    queue.elapsed_time_last_updated = time.time() - 10

    await ctrl.set_playback_speed(QUEUE_ID, 1.5)

    assert queue.elapsed_time == SAVED_POSITION


def test_the_release_ends_the_hold_of_the_position() -> None:
    """After the release no held position is left for the queue to report."""
    queue = _queue()
    queue.resume_pos = SAVED_POSITION
    ctrl = _controller(queue, _player(announcement=False))
    ctrl.hold_for_announcement(QUEUE_ID)
    queue.elapsed_time_last_updated = time.time() - 10

    ctrl.release_after_announcement(QUEUE_ID)

    assert ctrl._queue_data[QUEUE_ID].announcement_position is None


async def test_the_queue_that_the_restore_played_again_is_left_to_its_player() -> None:
    """After a restore that started playback the release leaves the position to the next report."""
    queue = _queue()
    player = _player(announcement=False)
    ctrl = _controller(queue, player)
    ctrl._queue_data[QUEUE_ID].prev_state = _prev_state(queue)
    ctrl.on_player_update = Mock()  # type: ignore[method-assign]
    announcements = _fallback_announcements(ctrl)

    async def _resume(*_args: Any) -> None:
        # what the restore's play_index does once the item loaded
        ctrl.on_player_update.reset_mock()  # type: ignore[attr-defined]
        player.playback_state = PlaybackState.PLAYING
        ctrl._queue_data[QUEUE_ID].announcement_position = None

    announcements._handle_cmd_resume = AsyncMock(side_effect=_resume)  # type: ignore[method-assign]

    await announcements._run_announcement(player, _announce_data(), None)

    ctrl.on_player_update.assert_not_called()
    assert not _held(ctrl)


def test_a_queue_that_did_not_get_to_play_again_is_resynced() -> None:
    """When the restore did not bring the player to play, the release resyncs the queue."""
    queue = _queue()
    queue.resume_pos = SAVED_POSITION
    player = _player(announcement=False, state=PlaybackState.IDLE)
    ctrl = _controller(queue, player)
    ctrl.on_player_update = Mock()  # type: ignore[method-assign]
    ctrl.hold_for_announcement(QUEUE_ID)
    ctrl._queue_data[QUEUE_ID].announcement_position = None

    ctrl.release_after_announcement(QUEUE_ID)

    ctrl.on_player_update.assert_called_once_with(player, {})


def test_a_sleep_timer_set_during_the_clip_stops_at_the_end_of_the_chapter_of_the_music() -> None:
    """The chapter is the one at the held position, not the one the running clock has reached."""
    queue = _queue()
    queue.resume_pos = SAVED_POSITION
    queue.current_item = SimpleNamespace(  # type: ignore[assignment]
        queue_item_id=TRACK_B,
        duration=200,
        media_item=SimpleNamespace(
            metadata=SimpleNamespace(
                chapters=[SimpleNamespace(start=0, end=70), SimpleNamespace(start=70, end=200)]
            )
        ),
    )
    ctrl = _controller(queue, _player())
    ctrl.release_unneeded_audio = Mock()  # type: ignore[method-assign]
    ctrl._update_player_sleep_timer = Mock(return_value=None)  # type: ignore[method-assign]
    ctrl.hold_for_announcement(QUEUE_ID)
    queue.elapsed_time_last_updated = time.time() - 10

    ctrl.set_sleep_timer_target(QUEUE_ID, SleepTimerMode.END_OF_CHAPTER)

    target = ctrl._queue_data[QUEUE_ID].sleep_timer_target
    assert target is not None
    assert target.stop_position == 70


def test_clearing_the_queue_ends_the_hold_of_the_position() -> None:
    """A queue without items has no position an announcement could hold."""
    queue = _queue()
    queue.resume_pos = SAVED_POSITION
    ctrl = _controller(queue, _player())
    ctrl.hold_for_announcement(QUEUE_ID)
    ctrl.mass.streams.audio_processing.clear = Mock()  # type: ignore[method-assign]
    ctrl.store_sources = Mock()  # type: ignore[method-assign]
    ctrl.is_smart_shuffle_active = Mock(return_value=False)  # type: ignore[method-assign]

    ctrl._clear(QUEUE_ID, skip_stop=True)

    assert ctrl._queue_data[QUEUE_ID].announcement_position is None


def test_the_hold_of_an_announcement_is_not_written_to_the_cache() -> None:
    """The cached queue carries no announcement marker."""
    queue = _queue()
    queue.extra_attributes[ATTR_ANNOUNCEMENT_IN_PROGRESS] = True
    data = PlayerQueueData(queue=queue)

    assert ATTR_ANNOUNCEMENT_IN_PROGRESS not in data.to_cache()["queue"]["extra_attributes"]
    assert queue.extra_attributes[ATTR_ANNOUNCEMENT_IN_PROGRESS]


async def test_a_skip_during_the_clip_is_relative_to_the_saved_position() -> None:
    """A skip that arrives while the clip plays seeks from where the music stopped."""
    queue = _queue()
    queue.resume_pos = SAVED_POSITION
    ctrl = _controller(queue, _player())
    ctrl._resolve_queue_id = Mock(return_value=QUEUE_ID)  # type: ignore[method-assign]
    ctrl._check_player_permission = Mock()  # type: ignore[method-assign]
    ctrl.seek = AsyncMock()  # type: ignore[method-assign]
    ctrl.hold_for_announcement(QUEUE_ID)
    queue.elapsed_time_last_updated = time.time() - 8

    await ctrl.skip(QUEUE_ID, 10)

    ctrl.seek.assert_awaited_once_with(QUEUE_ID, SAVED_POSITION + 10)


def test_a_snapshot_during_the_clip_keeps_the_saved_position() -> None:
    """A snapshot taken while the clip plays resumes from where the announcement stopped."""
    queue = _queue()
    queue.resume_pos = SAVED_POSITION
    ctrl = _controller(queue, _player())
    ctrl.hold_for_announcement(QUEUE_ID)
    queue.elapsed_time_last_updated = time.time() - 10

    snapshot = ctrl.snapshot(QUEUE_ID)

    assert snapshot.data.queue.resume_pos == SAVED_POSITION


def test_a_queue_that_ends_during_the_clip_holds_no_position() -> None:
    """A queue marked as ended has no position an announcement could hold."""
    queue = _queue()
    queue.resume_pos = SAVED_POSITION
    ctrl = _controller(queue, _player())
    ctrl._clear_sleep_timer = Mock()  # type: ignore[method-assign]
    ctrl.hold_for_announcement(QUEUE_ID)

    ctrl.mark_ended(QUEUE_ID)

    assert ctrl._queue_data[QUEUE_ID].announcement_position is None


async def test_a_cancelled_announcement_releases_the_queue() -> None:
    """The queue is released also when the announcement is cancelled while the clip plays."""
    queue = _queue()
    player = _player(announcement=False)
    ctrl = _controller(queue, player)
    announcements = _fallback_announcements(ctrl)
    announcements._handle_play_media = AsyncMock(  # type: ignore[method-assign]
        side_effect=asyncio.CancelledError
    )

    with pytest.raises(asyncio.CancelledError):
        await announcements._run_announcement(player, _announce_data(), None)

    assert not _held(ctrl)
    assert ctrl._queue_data[QUEUE_ID].announcement_position is None


async def test_an_announcement_that_held_nothing_still_releases_the_queue_of_its_player() -> None:
    """A queue that was paused follows its player again after the clip, as one that was held."""
    queue = _queue(PlaybackState.PAUSED)
    queue.extra_attributes[ATTR_ANNOUNCEMENT_IN_PROGRESS] = True
    player = _player(announcement=False, state=PlaybackState.PAUSED)
    ctrl = _controller(queue, player)
    announcements = _fallback_announcements(ctrl)
    announcements._handle_play_media = AsyncMock(  # type: ignore[method-assign]
        side_effect=PlayerCommandFailed("unreachable")
    )

    with pytest.raises(PlayerCommandFailed):
        await announcements._run_announcement(player, _announce_data(), None)

    assert not _held(ctrl)
