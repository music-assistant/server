"""Tests for PlayerQueuesController.transfer_queue protocol/ungroup and settings handover."""

from __future__ import annotations

import asyncio
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import (
    AlbumType,
    ContentType,
    MediaType,
    PlaybackState,
    PlayerType,
    StreamType,
)
from music_assistant_models.errors import PlayerCommandFailed
from music_assistant_models.media_items import Album, AudioFormat, ItemMapping, Track
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem, QueueItemOrigin
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.controllers.streams.audio_buffer import AudioBuffer


class _DummyACM:
    """Minimal async context manager stand-in for wait_for_player_update."""

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: object) -> bool:
        return False


def _fake_controller(source_id: str, target_player: MagicMock) -> MagicMock:
    """
    Build a MagicMock standing in for the controller for transfer_queue tests.

    The source queue is idle so the capture/stop/resume path is a no-op; the test
    only exercises the pre-transfer ungroup branch.
    """
    source_queue = MagicMock()
    source_queue.state = PlaybackState.IDLE
    source_queue.resume_pos = 0
    source_queue.elapsed_time = 0
    source_queue.current_index = 0
    source_queue.current_item = None
    target_queue = MagicMock()

    fake = MagicMock()
    fake.get = MagicMock(side_effect=lambda qid: source_queue if qid == source_id else target_queue)
    fake.stop = AsyncMock()
    fake._cleanup_queue_audio_data = AsyncMock()
    fake.load = AsyncMock()
    fake.resume = AsyncMock()
    fake.clear = MagicMock()
    fake.update_items = MagicMock()
    fake._notify_audio_source_transferred = AsyncMock()
    fake.mass.players.get_player = MagicMock(return_value=target_player)
    fake.mass.players.cmd_ungroup = AsyncMock()
    fake.mass.players.wait_for_player_update = MagicMock(return_value=_DummyACM())
    fake.mass.streams.is_smart_fades_active = MagicMock(return_value=False)
    return fake


async def test_transfer_queue_ad_hoc_member_ungroups_target_not_leader() -> None:
    """
    Transferring onto an ad-hoc sync member frees the target itself, not its leader.

    Ungrouping the leader would transfer leadership to a remaining member and
    recurse back into transfer_queue, so only the target may be ungrouped.
    """
    target_player = MagicMock()
    target_player.state.type = PlayerType.PLAYER
    target_player.state.synced_to = "leaderA"
    target_player.state.active_group = None
    fake = _fake_controller("src", target_player)

    await PlayerQueuesController.transfer_queue(
        cast("PlayerQueuesController", fake), "src", "memberB", auto_play=False
    )

    fake.mass.players.cmd_ungroup.assert_awaited_once_with("memberB")


async def test_transfer_queue_refuses_non_audio_target() -> None:
    """
    A target that can never render audio is refused before anything is touched.

    The source queue must survive intact: no stop, no clear, no item handover.
    """
    target_player = MagicMock()
    target_player.state.type = PlayerType.VISUALIZER
    fake = _fake_controller("src", target_player)

    with pytest.raises(PlayerCommandFailed):
        await PlayerQueuesController.transfer_queue(
            cast("PlayerQueuesController", fake), "src", "viz", auto_play=False
        )

    fake.stop.assert_not_awaited()
    fake._clear.assert_not_called()
    fake.load.assert_not_awaited()
    fake.mass.players.cmd_ungroup.assert_not_awaited()


async def test_transfer_queue_group_member_ungroups_group() -> None:
    """Transferring onto a virtual-group member releases the group player itself."""
    target_player = MagicMock()
    target_player.state.type = PlayerType.PLAYER
    target_player.state.synced_to = None
    target_player.state.active_group = "groupP"
    fake = _fake_controller("src", target_player)

    await PlayerQueuesController.transfer_queue(
        cast("PlayerQueuesController", fake), "src", "memberB", auto_play=False
    )

    fake.mass.players.cmd_ungroup.assert_awaited_once_with("groupP")


def _shuffle_controller(
    source_shuffle_enabled: bool,
    target_shuffle_enabled: bool = False,
    source_is_dynamic: bool = False,
    *,
    source_crossfade_override: bool | None = None,
    source_autoplay_override: bool | None = None,
    target_crossfade_override: bool | None = None,
    target_autoplay_override: bool | None = None,
) -> MagicMock:
    """
    Build a controller stand-in whose two queues carry real state records.

    The shuffle flag lives on the per-queue state record, so unlike the ungroup tests above these
    need real queues rather than one shared mock.

    :param source_shuffle_enabled: Whether shuffle is on for the queue being handed over.
    :param target_shuffle_enabled: Whether shuffle is on for the queue being handed to.
    :param source_is_dynamic: Whether the source queue is managed by a dynamic source.
    :param source_crossfade_override: Pinned crossfade override on the source queue, if any.
    :param source_autoplay_override: Pinned autoplay override on the source queue, if any.
    :param target_crossfade_override: Pinned crossfade override already on the target, if any.
    :param target_autoplay_override: Pinned autoplay override already on the target, if any.
    """
    source_queue = PlayerQueue(
        queue_id="src",
        active=True,
        display_name="Src",
        available=True,
        items=0,
        shuffle_enabled=source_shuffle_enabled,
        smart_shuffle_active=source_is_dynamic,
        is_dynamic=source_is_dynamic,
        # the wire fields carry the already-resolved effective value for these tests
        crossfade_enabled=bool(source_crossfade_override),
        autoplay_enabled=bool(source_autoplay_override),
    )
    target_queue = PlayerQueue(
        queue_id="tgt",
        active=True,
        display_name="Tgt",
        available=True,
        items=0,
        shuffle_enabled=target_shuffle_enabled,
        crossfade_enabled=bool(target_crossfade_override),
        autoplay_enabled=bool(target_autoplay_override),
    )

    fake = MagicMock()
    fake.get = MagicMock(side_effect=lambda qid: source_queue if qid == "src" else target_queue)
    fake._queue_data = {
        "src": PlayerQueueData(
            queue=source_queue,
            crossfade_override=source_crossfade_override,
            autoplay_override=source_autoplay_override,
        ),
        "tgt": PlayerQueueData(
            queue=target_queue,
            crossfade_override=target_crossfade_override,
            autoplay_override=target_autoplay_override,
        ),
    }
    fake.stop = AsyncMock()
    fake._cleanup_queue_audio_data = AsyncMock()
    fake.load = AsyncMock()
    fake.resume = AsyncMock()
    fake._clear = MagicMock()
    fake.update_items = MagicMock()
    fake._notify_audio_source_transferred = AsyncMock()
    fake.is_smart_shuffle_active = MagicMock(side_effect=lambda queue: queue.is_dynamic)
    # bind the real toggle resolver (with stubbed globals) so the handover under test
    # exercises actual logic
    defaults = {"autoplay_enabled": True, "crossfade_enabled": False}
    fake.mass.config.get_raw_core_config_value = MagicMock(
        side_effect=lambda _core_module, key, _default: defaults[key]
    )
    fake._resolve_default_toggles = lambda queue_data: (
        PlayerQueuesController._resolve_default_toggles(fake, queue_data)
    )
    target_player = MagicMock()
    target_player.state.type = PlayerType.PLAYER
    target_player.state.synced_to = None
    target_player.state.active_group = None
    fake.mass.players.get_player = MagicMock(return_value=target_player)
    fake.mass.streams.is_smart_fades_active = MagicMock(return_value=False)
    return fake


async def test_transfer_queue_carries_the_album_credit_bookkeeping() -> None:
    """A credited album stays credited on the player the queue is handed to."""
    fake = _shuffle_controller(source_shuffle_enabled=False)
    album = Album(
        item_id="a1",
        provider="library",
        name="A",
        provider_mappings=set(),
        album_type=AlbumType.ALBUM,
    )
    track = Track(item_id="t1", provider="library", name="T1", provider_mappings=set(), album=album)
    item = QueueItem.from_media_item("src", track)
    item.origin = QueueItemOrigin(container=ItemMapping.from_item(album))
    source_data = fake._queue_data["src"]
    source_data.credited_albums = {"library://album/a1"}

    await PlayerQueuesController.transfer_queue(
        cast("PlayerQueuesController", fake), "src", "tgt", auto_play=False
    )

    target_data = fake._queue_data["tgt"]
    # the target holds its own copy, so the source's set no longer drives it
    assert target_data.credited_albums == {"library://album/a1"}
    assert target_data.credited_albums is not source_data.credited_albums
    # and the album is not credited a second time on the new player
    assert (
        PlayerQueuesController._claim_album_credit(
            cast("PlayerQueuesController", fake), target_data, item
        )
        is None
    )


async def test_transfer_queue_overwrites_the_targets_own_shuffle() -> None:
    """The queue brings its own shuffle state, so the target's previous one does not survive."""
    fake = _shuffle_controller(source_shuffle_enabled=False, target_shuffle_enabled=True)

    await PlayerQueuesController.transfer_queue(
        cast("PlayerQueuesController", fake), "src", "tgt", auto_play=False
    )

    assert fake.get("tgt").shuffle_enabled is False


async def test_transfer_queue_carries_the_source_shuffle() -> None:
    """A shuffled queue handed to another player stays shuffled there."""
    fake = _shuffle_controller(source_shuffle_enabled=True)

    await PlayerQueuesController.transfer_queue(
        cast("PlayerQueuesController", fake), "src", "tgt", auto_play=False
    )

    assert fake.get("tgt").shuffle_enabled is True


async def test_transfer_queue_carries_where_the_smart_fades_ordering_ended() -> None:
    """The target keeps ordering Smart Fades batches behind the same item as the source did."""
    fake = _shuffle_controller(source_shuffle_enabled=True)
    fake._queue_data["src"].fade_ordered_until = "item-24"

    await PlayerQueuesController.transfer_queue(
        cast("PlayerQueuesController", fake), "src", "tgt", auto_play=False
    )

    assert fake._queue_data["tgt"].fade_ordered_until == "item-24"


async def test_transfer_queue_drops_dynamic_shuffle_from_source() -> None:
    """The shuffle imposed by a dynamic source follows it to the target queue."""
    fake = _shuffle_controller(source_shuffle_enabled=True, source_is_dynamic=True)
    fake._clear.side_effect = lambda queue_id, skip_stop=False: PlayerQueuesController._clear(
        cast("PlayerQueuesController", fake), queue_id, skip_stop
    )

    await PlayerQueuesController.transfer_queue(
        cast("PlayerQueuesController", fake), "src", "tgt", auto_play=False
    )

    assert fake.get("tgt").is_dynamic is True
    assert fake.get("tgt").shuffle_enabled is True
    assert fake.get("src").is_dynamic is False
    assert fake.get("src").shuffle_enabled is False
    assert fake.get("src").smart_shuffle_active is False


async def test_transfer_queue_carries_pinned_toggle_overrides() -> None:
    """Pinned autoplay/crossfade overrides, and their already-resolved wire values, reach the target."""
    fake = _shuffle_controller(
        source_shuffle_enabled=False,
        source_crossfade_override=True,
        source_autoplay_override=False,
        target_crossfade_override=False,
        target_autoplay_override=True,
    )

    await PlayerQueuesController.transfer_queue(
        cast("PlayerQueuesController", fake), "src", "tgt", auto_play=False
    )

    target_data = fake._queue_data["tgt"]
    assert target_data.crossfade_override is True
    assert target_data.autoplay_override is False
    assert target_data.queue.crossfade_enabled is True
    assert target_data.queue.autoplay_enabled is False


async def test_transfer_queue_waits_for_the_source_teardown_before_the_target_starts() -> None:
    """
    The target starts only after the source has released its audio.

    A single-stream provider would otherwise keep its only slot on the source.
    """
    source_queue = PlayerQueue(
        queue_id="src", active=True, display_name="Src", available=True, items=1
    )
    source_queue.state = PlaybackState.PLAYING
    target_queue = PlayerQueue(
        queue_id="tgt", active=True, display_name="Tgt", available=True, items=0
    )
    playing = QueueItem(queue_id="src", queue_item_id="i1", name="i1", duration=180)
    playing.streamdetails = StreamDetails(
        provider="spotify--1",
        item_id="i1",
        audio_format=AudioFormat(content_type=ContentType.OGG),
        media_type=MediaType.TRACK,
        stream_type=StreamType.CUSTOM,
        queue_id="src",
    )
    playing.streamdetails.queue_session_id = "sess-src"
    source_released = asyncio.Event()
    source_buffer = MagicMock(spec=AudioBuffer)
    source_buffer.clear = AsyncMock(side_effect=source_released.wait)
    playing.streamdetails.buffer = source_buffer
    source_queue.current_item = playing
    source_queue.current_index = 0

    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.logger = MagicMock()
    ctrl._queue_data = {
        "src": PlayerQueueData(queue=source_queue, items=[playing], session_id="sess-src"),
        "tgt": PlayerQueueData(queue=target_queue),
    }
    ctrl.mass = MagicMock()
    # mass.create_task starts tasks eagerly
    ctrl.mass.create_task = lambda coro, *_args, **_kwargs: asyncio.Task(
        coro, loop=asyncio.get_running_loop(), eager_start=True
    )
    ctrl.mass.players.get_group_and_player_lock = MagicMock(return_value=_DummyACM())
    ctrl.mass.players._handle_cmd_stop = AsyncMock()
    target_player = MagicMock()
    target_player.state.type = PlayerType.PLAYER
    target_player.state.synced_to = None
    target_player.state.active_group = None
    ctrl.mass.players.get_player = MagicMock(return_value=target_player)
    ctrl.mass.streams.is_smart_fades_active = MagicMock(return_value=False)
    ctrl._check_player_permission = MagicMock()  # type: ignore[method-assign]
    ctrl.signal_update = MagicMock()  # type: ignore[method-assign]
    ctrl.store_sources = MagicMock()  # type: ignore[method-assign]
    ctrl._set_transitioning = MagicMock()  # type: ignore[method-assign]
    ctrl._resolve_default_toggles = MagicMock()  # type: ignore[method-assign]
    ctrl.is_smart_shuffle_active = MagicMock(return_value=False)  # type: ignore[method-assign]

    def _update_items(queue_id: str, queue_items: list[QueueItem]) -> None:
        ctrl._queue_data[queue_id].items = queue_items

    ctrl.update_items = _update_items  # type: ignore[method-assign]
    ctrl.load = AsyncMock()  # type: ignore[method-assign]
    ctrl.resume = AsyncMock()  # type: ignore[method-assign]

    transfer = asyncio.create_task(ctrl.transfer_queue("src", "tgt", auto_play=True))
    await asyncio.sleep(0.05)
    ctrl.resume.assert_not_awaited()

    source_released.set()
    await asyncio.wait_for(transfer, 1)
    ctrl.resume.assert_awaited_once_with("tgt")
