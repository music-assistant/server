"""Tests for PlayerQueuesController.transfer_queue protocol/ungroup and settings handover."""

from __future__ import annotations

import time
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import AlbumType, PlaybackState, PlayerType
from music_assistant_models.errors import PlayerCommandFailed
from music_assistant_models.media_items import Album, ItemMapping, Radio, Track
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem, QueueItemOrigin

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData


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


def _playing_transfer(
    target_state: PlaybackState,
    media_item: Track | Radio,
    *,
    source_position: float = 20,
    item_in_queue: bool = True,
    target_ended: bool = False,
) -> tuple[MagicMock, QueueItem]:
    """
    Build a controller stand-in whose source plays an item, with a target on its own clock at 150 s.

    :param target_state: Playback state of the target queue.
    :param media_item: The media the source queue is playing.
    :param source_position: Where the source is in that item, in seconds.
    :param item_in_queue: Whether the current item is still among the transferred items.
    :param target_ended: Whether the target's own queue had played to its end.
    """
    fake = _shuffle_controller(source_shuffle_enabled=False)
    item = QueueItem.from_media_item("src", media_item)
    source_queue = fake.get("src")
    source_queue.state = PlaybackState.PLAYING
    source_queue.elapsed_time = source_position
    source_queue.elapsed_time_last_updated = time.time()
    source_queue.current_item = item
    target_queue = fake.get("tgt")
    target_queue.state = target_state
    target_queue.elapsed_time = 150
    target_queue.elapsed_time_last_updated = time.time()
    target_queue.ended = target_ended
    fake._queue_data["src"].items = [item]
    fake.index_by_id = MagicMock(return_value=0 if item_in_queue else None)
    fake.play_index = AsyncMock()
    return fake, item


async def test_transfer_queue_resumes_a_replacement_for_a_missing_item_from_the_start() -> None:
    """
    An item the queue moved on to, as the source's own was gone, does not inherit its position.

    The source's current item was deleted while it was paused; the target still has a buffered
    index from what it played before, so loading the items moves it onto the item at that index.
    """
    fake = _shuffle_controller(source_shuffle_enabled=False)
    for name in ("load", "update_items", "_resync_position", "index_by_id", "get_item"):
        method = getattr(PlayerQueuesController, name)
        setattr(fake, name, method.__get__(fake))
    fake.play_index = AsyncMock()
    deleted = QueueItem.from_media_item(
        "src", Track(item_id="t1", provider="library", name="T1", provider_mappings=set())
    )
    remaining = QueueItem.from_media_item(
        "src", Track(item_id="t2", provider="library", name="T2", provider_mappings=set())
    )
    source_queue = fake.get("src")
    source_queue.state = PlaybackState.PAUSED
    source_queue.resume_pos = 150
    source_queue.current_index = 0
    source_queue.current_item = deleted
    fake._queue_data["src"].items = [remaining]
    target_queue = fake.get("tgt")
    target_queue.index_in_buffer = 0

    await PlayerQueuesController.transfer_queue(
        cast("PlayerQueuesController", fake), "src", "tgt", auto_play=True
    )

    assert target_queue.current_item is remaining
    assert target_queue.resume_pos == 0
    fake.play_index.assert_not_awaited()
    fake.resume.assert_awaited_once_with("tgt")


@pytest.mark.parametrize(
    ("target_state", "target_ended"),
    [
        (PlaybackState.PLAYING, False),
        (PlaybackState.PAUSED, False),
        (PlaybackState.IDLE, False),
        (PlaybackState.IDLE, True),
    ],
)
async def test_transfer_queue_starts_the_item_at_the_source_position(
    target_state: PlaybackState, target_ended: bool
) -> None:
    """
    The transferred item starts where the source was, whatever the target played before.

    :param target_state: Playback state of the target queue.
    :param target_ended: Whether the target's own queue had played to its end.
    """
    track = Track(item_id="t1", provider="library", name="T1", provider_mappings=set())
    fake, item = _playing_transfer(target_state, track, target_ended=target_ended)

    await PlayerQueuesController.transfer_queue(
        cast("PlayerQueuesController", fake), "src", "tgt", auto_play=True
    )

    fake.play_index.assert_awaited_once_with("tgt", item.queue_item_id, 20)
    fake.resume.assert_not_awaited()


async def test_transfer_queue_starts_an_item_the_source_just_began_at_zero() -> None:
    """A source at 0 s does not let the target fall back to its own last position."""
    track = Track(item_id="t1", provider="library", name="T1", provider_mappings=set())
    fake, item = _playing_transfer(PlaybackState.PAUSED, track, source_position=0)

    await PlayerQueuesController.transfer_queue(
        cast("PlayerQueuesController", fake), "src", "tgt", auto_play=True
    )

    fake.play_index.assert_awaited_once_with("tgt", item.queue_item_id, 0)


async def test_transfer_queue_starts_radio_from_the_start() -> None:
    """A radio stream is not started part way in."""
    radio = Radio(item_id="r1", provider="library", name="R1", provider_mappings=set())
    fake, item = _playing_transfer(PlaybackState.PLAYING, radio)

    await PlayerQueuesController.transfer_queue(
        cast("PlayerQueuesController", fake), "src", "tgt", auto_play=True
    )

    fake.play_index.assert_awaited_once_with("tgt", item.queue_item_id, 0)


@pytest.mark.parametrize("current_item", ["missing", "none"])
async def test_transfer_queue_resumes_without_a_current_item_in_the_queue(
    current_item: str,
) -> None:
    """
    Without a current item that is in the queue, resume() picks where to start.

    :param current_item: Whether the current item is missing from the items or not set at all.
    """
    track = Track(item_id="t1", provider="library", name="T1", provider_mappings=set())
    fake, _item = _playing_transfer(PlaybackState.PLAYING, track, item_in_queue=False)
    if current_item == "none":
        fake.get("src").current_item = None
        fake.get("tgt").current_item = None

    await PlayerQueuesController.transfer_queue(
        cast("PlayerQueuesController", fake), "src", "tgt", auto_play=True
    )

    fake.resume.assert_awaited_once_with("tgt")
    fake.play_index.assert_not_awaited()
    assert fake.get("tgt").resume_pos == 20


async def test_transfer_queue_clears_the_targets_own_ended_state() -> None:
    """A target whose own queue had ended is not left marked ended with the new items."""
    track = Track(item_id="t1", provider="library", name="T1", provider_mappings=set())
    fake, _item = _playing_transfer(PlaybackState.IDLE, track, target_ended=True)

    await PlayerQueuesController.transfer_queue(
        cast("PlayerQueuesController", fake), "src", "tgt", auto_play=False
    )

    assert fake.get("tgt").ended is False


async def test_transfer_queue_restarts_a_queue_that_had_ended() -> None:
    """A queue that played to its end stays ended on the target, so resume() starts it over."""
    track = Track(item_id="t1", provider="library", name="T1", provider_mappings=set())
    fake, _item = _playing_transfer(PlaybackState.PLAYING, track, source_position=0)
    source_queue = fake.get("src")
    source_queue.state = PlaybackState.IDLE
    source_queue.ended = True

    await PlayerQueuesController.transfer_queue(
        cast("PlayerQueuesController", fake), "src", "tgt", auto_play=True
    )

    fake.resume.assert_awaited_once_with("tgt")
    fake.play_index.assert_not_awaited()
    assert fake.get("tgt").ended is True


async def test_transfer_queue_carries_the_source_clock_without_auto_play() -> None:
    """A later resume() on the target reads the source's position, not the target's own clock."""
    track = Track(item_id="t1", provider="library", name="T1", provider_mappings=set())
    fake, _item = _playing_transfer(PlaybackState.IDLE, track, source_position=0)

    await PlayerQueuesController.transfer_queue(
        cast("PlayerQueuesController", fake), "src", "tgt", auto_play=False
    )

    target_queue = fake.get("tgt")
    assert target_queue.elapsed_time == 0
    assert (
        PlayerQueuesController._last_played_position(
            cast("PlayerQueuesController", fake), target_queue
        )
        == 0
    )


async def test_transfer_queue_carries_no_position_from_a_queue_that_had_ended() -> None:
    """An ended queue arrives at 0 s, not at the end of the item it finished on."""
    track = Track(item_id="t1", provider="library", name="T1", provider_mappings=set())
    fake, item = _playing_transfer(PlaybackState.IDLE, track, source_position=0)
    source_queue = fake.get("src")
    source_queue.state = PlaybackState.IDLE
    source_queue.ended = True
    source_queue.resume_pos = 0
    fake._queue_data["src"].prev_state = {
        "current_item_id": item.queue_item_id,
        "last_playing_elapsed_time": 238,
    }
    fake._last_played_position = lambda queue: PlayerQueuesController._last_played_position(
        cast("PlayerQueuesController", fake), queue
    )

    await PlayerQueuesController.transfer_queue(
        cast("PlayerQueuesController", fake), "src", "tgt", auto_play=False
    )

    target_queue = fake.get("tgt")
    assert target_queue.ended is True
    assert target_queue.resume_pos == 0
    assert target_queue.elapsed_time == 0
