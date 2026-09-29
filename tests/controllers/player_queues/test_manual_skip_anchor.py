"""
Tests that a manual next/previous stashes the outgoing track for the folder tiebreak.

next()/previous() advance queue.current_item to the target immediately, ahead of their
debounced play_index resolving its streamdetails - so by the time get_stream_details runs,
current_item is the target itself rather than the predecessor it needs to break same-quality
mapping ties toward the currently playing folder. _stash_transition_anchor captures the
outgoing item's streamdetails onto PlayerQueueData.pending_transition_anchor before that
overwrite, for get_stream_details to fall back to.

The stash is scoped to the specific target it was captured for, and play_index/_clear both
clear it once that transition ends (successfully or not) - so a value left behind by one
transition can never bias a later, unrelated get_stream_details call.
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, Mock

from music_assistant_models.enums import ContentType, MediaType, PlaybackState
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import AudioFormat, ItemMapping, ProviderMapping, Track
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.streamdetails import StreamDetails
from music_assistant_models.unique_list import UniqueList

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData

TRACK_IDS = ["t1", "t2", "t3"]
INSTANCE = "filesystem_local--main"


def _track(item_id: str) -> Track:
    """Build a playable Track on the 'filesystem_local' provider."""
    return Track(
        item_id=item_id,
        provider=INSTANCE,
        name=f"Track {item_id}",
        duration=60,
        artists=UniqueList(
            [ItemMapping(item_id="a", provider=INSTANCE, name="A", media_type=MediaType.ARTIST)]
        ),
        provider_mappings={
            ProviderMapping(
                item_id=item_id, provider_domain="filesystem_local", provider_instance=INSTANCE
            )
        },
    )


def _controller() -> Any:
    """Build a bare controller whose queue "q1" is playing three tracks."""
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.logger = Mock()
    ctrl.mass = MagicMock()
    ctrl.mass.players.get_player = Mock(return_value=Mock(extra_data={}))
    lock_cm = MagicMock()
    lock_cm.__aenter__ = AsyncMock(return_value=None)
    lock_cm.__aexit__ = AsyncMock(return_value=None)
    ctrl.mass.players.get_player_lock = Mock(return_value=lock_cm)
    ctrl.mass.call_later = Mock()
    ctrl.signal_update = Mock()  # type: ignore[method-assign]
    ctrl.on_player_update = Mock()  # type: ignore[method-assign]

    items = [QueueItem.from_media_item("q1", _track(item_id)) for item_id in TRACK_IDS]
    queue = PlayerQueue(
        queue_id="q1",
        active=True,
        display_name="Q1",
        available=True,
        items=len(items),
        state=PlaybackState.PLAYING,
        current_index=1,
        current_item=items[1],
    )
    ctrl._queue_data = {"q1": PlayerQueueData(queue=queue, items=items)}
    return ctrl


async def test_next_stashes_the_outgoing_tracks_streamdetails_before_advancing() -> None:
    """A manual skip preserves the predecessor's streamdetails for the folder tiebreak."""
    ctrl = _controller()
    queue_data = cast("PlayerQueueData", ctrl._queue_data["q1"])
    assert queue_data.queue.current_item is not None
    outgoing_details = StreamDetails(
        provider=INSTANCE,
        item_id="Various Artists/Compilation Album/02 Track.flac",
        audio_format=AudioFormat(content_type=ContentType.MP3),
        media_type=MediaType.TRACK,
    )
    queue_data.queue.current_item.streamdetails = outgoing_details

    await ctrl.next("q1")

    assert queue_data.pending_transition_anchor == (
        queue_data.items[2].queue_item_id,
        INSTANCE,
        outgoing_details.item_id,
    )
    # the target advanced too, ahead of play_index resolving its own streamdetails
    assert queue_data.queue.current_index == 2
    assert queue_data.queue.current_item is not None
    assert queue_data.queue.current_item.queue_item_id == queue_data.items[2].queue_item_id


async def test_rapid_second_skip_carries_the_true_predecessor_forward() -> None:
    """A second next() before play_index runs must not lose the real predecessor's details."""
    ctrl = _controller()
    queue_data = cast("PlayerQueueData", ctrl._queue_data["q1"])
    # start from the first track so both presses land on real queue items
    queue_data.queue.current_index = 0
    queue_data.queue.current_item = queue_data.items[0]
    real_predecessor_details = StreamDetails(
        provider=INSTANCE,
        item_id="Various Artists/Compilation Album/01 Track.flac",
        audio_format=AudioFormat(content_type=ContentType.MP3),
        media_type=MediaType.TRACK,
    )
    queue_data.queue.current_item.streamdetails = real_predecessor_details

    await ctrl.next("q1")  # t1 (really playing) -> t2 (target); t2 never actually starts
    intermediate_item = queue_data.queue.current_item
    assert intermediate_item is not None
    assert intermediate_item.queue_item_id == queue_data.items[1].queue_item_id
    assert intermediate_item.streamdetails is None

    await ctrl.next("q1")  # rapid second press cancels play_index(t2) before it ever ran

    assert queue_data.pending_transition_anchor == (
        queue_data.items[2].queue_item_id,
        INSTANCE,
        real_predecessor_details.item_id,
    )


async def test_rapid_second_skip_ignores_the_intermediate_items_stale_streamdetails() -> None:
    """A leftover streamdetails on the skipped-over item must not replace the real predecessor's."""
    ctrl = _controller()
    queue_data = cast("PlayerQueueData", ctrl._queue_data["q1"])
    queue_data.queue.current_index = 0
    queue_data.queue.current_item = queue_data.items[0]
    real_predecessor_details = StreamDetails(
        provider=INSTANCE,
        item_id="Various Artists/Compilation Album/01 Track.flac",
        audio_format=AudioFormat(content_type=ContentType.MP3),
        media_type=MediaType.TRACK,
    )
    queue_data.queue.current_item.streamdetails = real_predecessor_details

    await ctrl.next("q1")  # t1 -> t2 (target)
    intermediate_item = queue_data.queue.current_item
    assert intermediate_item is not None
    # t2 carries a stale streamdetails from some earlier, unrelated play - never re-fetched,
    # since this attempt's play_index gets cancelled by the second press below
    intermediate_item.streamdetails = StreamDetails(
        provider=INSTANCE,
        item_id="Various Artists/Compilation Album/99 Old Play.flac",
        audio_format=AudioFormat(content_type=ContentType.MP3),
        media_type=MediaType.TRACK,
    )

    await ctrl.next("q1")  # rapid second press, before play_index(t2) ever consumes that stash

    assert queue_data.pending_transition_anchor == (
        queue_data.items[2].queue_item_id,
        INSTANCE,
        real_predecessor_details.item_id,
    )


async def test_previous_stashes_the_outgoing_tracks_streamdetails_before_advancing() -> None:
    """A manual skip back preserves the predecessor's streamdetails for the folder tiebreak."""
    ctrl = _controller()
    queue_data = cast("PlayerQueueData", ctrl._queue_data["q1"])
    assert queue_data.queue.current_item is not None
    queue_data.queue.elapsed_time = 2  # <5s in, so previous() moves back rather than restarting
    outgoing_details = StreamDetails(
        provider=INSTANCE,
        item_id="Various Artists/Compilation Album/02 Track.flac",
        audio_format=AudioFormat(content_type=ContentType.MP3),
        media_type=MediaType.TRACK,
    )
    queue_data.queue.current_item.streamdetails = outgoing_details

    await ctrl.previous("q1")

    assert queue_data.pending_transition_anchor == (
        queue_data.items[0].queue_item_id,
        INSTANCE,
        outgoing_details.item_id,
    )
    assert queue_data.queue.current_index == 0
    assert queue_data.queue.current_item is not None
    assert queue_data.queue.current_item.queue_item_id == queue_data.items[0].queue_item_id


async def test_next_leaves_the_anchor_unset_when_the_outgoing_track_has_no_streamdetails() -> None:
    """No streamdetails to anchor on yet (e.g. the queue just started) stashes nothing."""
    ctrl = _controller()
    queue_data = cast("PlayerQueueData", ctrl._queue_data["q1"])
    assert queue_data.queue.current_item is not None
    assert queue_data.queue.current_item.streamdetails is None

    await ctrl.next("q1")

    assert queue_data.pending_transition_anchor is None


async def test_clear_drops_a_pending_transition_anchor() -> None:
    """A queue reset must not leave a stash behind for whatever plays next to stumble into."""
    ctrl = _controller()
    queue_data = cast("PlayerQueueData", ctrl._queue_data["q1"])
    queue_data.pending_transition_anchor = ("t3", INSTANCE, "Various Artists/Comp/02 Track.flac")
    ctrl.store_sources = Mock()
    ctrl.is_smart_shuffle_active = Mock(return_value=False)
    ctrl._cleanup_queue_audio_data = AsyncMock()

    ctrl._clear("q1", skip_stop=True)

    assert queue_data.pending_transition_anchor is None


async def test_play_index_clears_the_pending_transition_anchor_once_it_finishes() -> None:
    """The stash next/previous left behind is consumed by its own transition and then dropped."""
    ctrl = _controller()
    queue_data = cast("PlayerQueueData", ctrl._queue_data["q1"])
    # a stash left over from some earlier, unrelated transition
    queue_data.pending_transition_anchor = ("t3", INSTANCE, "Various Artists/Comp/02 Track.flac")
    ctrl._load_item = AsyncMock()
    ctrl.player_media_from_queue_item = AsyncMock()
    ctrl.mass.players.play_media = AsyncMock()

    await ctrl.play_index("q1", 2)

    assert queue_data.pending_transition_anchor is None


async def test_play_index_retry_carries_the_real_predecessor_to_the_next_candidate() -> None:
    """A failed target's own stale streamdetails must not leak into the retry's anchor."""
    ctrl = _controller()  # 3 items, current_index=1 (t2): the pre-advanced, about-to-fail target
    queue_data = cast("PlayerQueueData", ctrl._queue_data["q1"])
    real_predecessor = (INSTANCE, "Various Artists/Compilation Album/01 Track.flac")
    queue_data.pending_transition_anchor = (queue_data.items[1].queue_item_id, *real_predecessor)
    # t2 (about to fail) carries stale streamdetails from some earlier, unrelated play
    queue_data.items[1].streamdetails = StreamDetails(
        provider=INSTANCE,
        item_id="Various Artists/Compilation Album/99 Old Play.flac",
        audio_format=AudioFormat(content_type=ContentType.MP3),
        media_type=MediaType.TRACK,
    )
    seen_anchors: list[tuple[str, str, str] | None] = []

    async def _load_item(queue_item: QueueItem, **_kwargs: object) -> None:
        seen_anchors.append(queue_data.pending_transition_anchor)
        if queue_item.queue_item_id == queue_data.items[1].queue_item_id:
            raise MediaNotFoundError("t2 unavailable")
        queue_item.streamdetails = StreamDetails(
            provider=INSTANCE,
            item_id="doesnt-matter",
            audio_format=AudioFormat(content_type=ContentType.MP3),
            media_type=MediaType.TRACK,
        )

    ctrl._load_item = _load_item
    ctrl.player_media_from_queue_item = AsyncMock()
    ctrl.mass.players.play_media = AsyncMock()

    await ctrl.play_index("q1", 1)

    assert seen_anchors[0] == (queue_data.items[1].queue_item_id, *real_predecessor)
    # the retry's own stash carries the real predecessor forward, not t2's stale streamdetails
    assert seen_anchors[1] == (queue_data.items[2].queue_item_id, *real_predecessor)
