"""
Smart shuffle helper for the Player Queues controller.

Smart shuffle reorders the upcoming queue items so recently-heard music is pushed toward the back
and same songs/artists are spread out, while honouring intentionally-duplicated items. It reads the
configured recency windows for a queue, takes a single play-history snapshot from the shared recency
engine, and runs the pure ``_arrange`` algorithm. Plain (non-smart) shuffle stays a pure random
shuffle in the controller.

The algorithm always keeps recency tiers authoritative. Within each tier, duplicate copies are
interleaved first. Regular Smart Shuffle then applies its bounded same-artist spacing pass. When
Smart Fades ordering is enabled and Smart Fades is active, the local transition selector orders one
batch of upcoming items at a time instead and handles their artist adjacency itself, reordering only
inside that batch and recency tier. A shuffle orders the first batch, and playback orders the next
one when it gets close.
"""

from __future__ import annotations

import random
from collections import Counter, defaultdict
from typing import TYPE_CHECKING

from music_assistant_models.media_items import Track

from music_assistant.constants import (
    CONF_PLAYER_QUEUES,
    CONF_VALUE_DISABLED,
    CONF_VALUE_ENABLED,
)
from music_assistant.controllers.music.recency import RecencyWindows
from music_assistant.controllers.player_queues.constants import (
    CONF_SMART_SHUFFLE_ARTIST_RECENCY,
    CONF_SMART_SHUFFLE_DUPLICATE_GAP,
    CONF_SMART_SHUFFLE_ENABLED,
    CONF_SMART_SHUFFLE_OPTIMIZE_SMART_FADES,
    CONF_SMART_SHUFFLE_SONG_RECENCY,
    SMART_FADE_ORDERING_BATCH,
    SMART_SHUFFLE_ARTIST_RECENCY_DEFAULT,
    SMART_SHUFFLE_DUPLICATE_GAP_DEFAULT,
    SMART_SHUFFLE_SONG_RECENCY_DEFAULT,
)
from music_assistant.controllers.player_queues.helpers import (
    committed_index,
    interleave_groups,
    space_by_artist,
)
from music_assistant.controllers.player_queues.smart_fade_ordering import order_queue_items

if TYPE_CHECKING:
    from music_assistant_models.player_queue import PlayerQueue
    from music_assistant_models.queue_item import QueueItem

    from music_assistant import MusicAssistant
    from music_assistant.controllers.music.recency import RecencySnapshot
    from music_assistant.controllers.player_queues.controller import PlayerQueuesController

# playback orders the next Smart Fades batch while this many ordered items are still ahead of it
_NEXT_BATCH_LEAD = 5


class SmartShuffle:
    """Produce a recency-aware, well-spaced ordering of upcoming queue items."""

    def __init__(self, queues: PlayerQueuesController) -> None:
        """
        Initialize the smart shuffle helper.

        :param queues: The owning player queues controller.
        """
        self.queues = queues
        self.mass = queues.mass
        self.logger = queues.logger.getChild("smart_shuffle")

    def is_enabled(self, queue_id: str) -> bool:
        """
        Return whether smart shuffle is enabled for the given queue.

        Follows the global (queue controller) setting when the per-queue value is "global".

        :param queue_id: The queue to read the smart-shuffle setting for.
        """
        return (
            self.mass.config.get_effective_player_queue_config_value(
                queue_id, CONF_SMART_SHUFFLE_ENABLED, CONF_VALUE_DISABLED
            )
            == CONF_VALUE_ENABLED
        )

    # Only run this extra ordering when the option is on and Smart Crossfade is active.
    # Turning it off leaves normal Smart Shuffle alone.

    def is_smart_fade_ordering_enabled(self, queue: PlayerQueue) -> bool:
        """Return whether Smart Fades-aware ordering should run for this queue."""
        return bool(
            queue.smart_fades_active
            and self.mass.config.get_effective_player_queue_config_value(
                queue.queue_id,
                CONF_SMART_SHUFFLE_OPTIMIZE_SMART_FADES,
                CONF_VALUE_DISABLED,
            )
            == CONF_VALUE_ENABLED
        )

    async def arrange(
        self, queue: PlayerQueue, items: list[QueueItem], *, preceding_item: QueueItem | None = None
    ) -> list[QueueItem]:
        """
        Return the items reordered with recency-aware smart shuffle.

        With Smart Fades ordering only the first batch is ordered for its transitions; playback
        orders the next batches.

        :param queue: The queue being (re)shuffled; its owner scopes the play history.
        :param items: The upcoming queue items to reorder.
        :param preceding_item: Locked item immediately before ``items``; used as the first anchor.
        """
        queue_data = self.queues.queue_data(queue.queue_id)
        windows = self.windows()
        snapshot = await self.mass.music.recency.snapshot(windows, userid=queue_data.userid)
        if self.is_smart_fade_ordering_enabled(queue):
            arranged = await _arrange_for_smart_fades(
                self.mass, items, snapshot, windows, preceding_item=preceding_item
            )
            queue_data.fade_ordered_until = (
                arranged[min(len(arranged), SMART_FADE_ORDERING_BATCH) - 1].queue_item_id
                if arranged
                else None
            )
            return arranged
        # nothing is ordered for Smart Fades now, so an earlier batch end no longer applies
        queue_data.fade_ordered_until = None
        return _arrange(items, snapshot, windows)

    def schedule_next_batch(self, queue: PlayerQueue) -> None:
        """
        Schedule ordering the next Smart Fades batch once playback gets close to the ordered end.

        :param queue: The queue whose playing item just changed.
        """
        if queue.current_index is None or not self._orders_batches(queue):
            return
        ordered_until = self.queues.queue_data(queue.queue_id).fade_ordered_until
        until_index = (
            self.queues.index_by_id(queue.queue_id, ordered_until) if ordered_until else None
        )
        if until_index is not None and until_index - queue.current_index > _NEXT_BATCH_LEAD:
            return
        # the delay folds a burst of skips into a single run
        self.mass.call_later(
            5,
            self.order_next_batch,
            queue.queue_id,
            task_id=f"order_next_fade_batch_{queue.queue_id}",
        )

    async def order_next_batch(self, queue_id: str) -> None:
        """
        Order the next batch of upcoming items for Smart Fades, behind the items ordered before.

        The queue is left as it is when it changed while the batch was being ordered.

        :param queue_id: The queue to order the next batch for.
        """
        if (queue_data := self.queues.queue_data_or_none(queue_id)) is None:
            return
        queue = queue_data.queue
        if (boundary := committed_index(queue)) is None or not self._orders_batches(queue):
            return
        items = queue_data.items
        # the player owns everything up to the boundary and may already hold the item after it
        start = boundary + 2
        ordered_until = queue_data.fade_ordered_until
        until_index = self.queues.index_by_id(queue_id, ordered_until) if ordered_until else None
        if until_index is not None:
            start = max(start, until_index + 1)
        if not (batch := items[start : start + SMART_FADE_ORDERING_BATCH]):
            return
        # copies of a song outside this batch still make it a deliberate duplicate
        song_counts = Counter(_song_key(item) for item in items[boundary + 1 :])
        windows = self.windows()
        snapshot = await self.mass.music.recency.snapshot(windows, userid=queue_data.userid)
        ordered = await _arrange_for_smart_fades(
            self.mass,
            batch,
            snapshot,
            windows,
            preceding_item=items[start - 1],
            song_counts=song_counts,
        )
        if (
            self.queues.queue_data_or_none(queue_id) is not queue_data
            or queue_data.items is not items
            or committed_index(queue) != boundary
        ):
            # the queue was edited or moved on meanwhile; its next playing item tries again
            return
        queue_data.fade_ordered_until = ordered[-1].queue_item_id
        self.queues.update_items(queue_id, [*items[:start], *ordered, *items[start + len(batch) :]])

    def windows(self) -> RecencyWindows:
        """Read the configured recency windows (in seconds). These are a global-only setting."""
        return RecencyWindows(
            song_seconds=self._window_seconds(
                CONF_SMART_SHUFFLE_SONG_RECENCY, SMART_SHUFFLE_SONG_RECENCY_DEFAULT
            ),
            artist_seconds=self._window_seconds(
                CONF_SMART_SHUFFLE_ARTIST_RECENCY, SMART_SHUFFLE_ARTIST_RECENCY_DEFAULT
            ),
            duplicate_gap_seconds=self._window_seconds(
                CONF_SMART_SHUFFLE_DUPLICATE_GAP, SMART_SHUFFLE_DUPLICATE_GAP_DEFAULT
            ),
        )

    def _window_seconds(self, key: str, default: int) -> int:
        """Read a window preset (seconds, 0 = off) from the global queue-controller config."""
        raw = self.mass.config.get_raw_core_config_value(CONF_PLAYER_QUEUES, key, default)
        try:
            return int(raw)
        except TypeError, ValueError:
            return default

    def _orders_batches(self, queue: PlayerQueue) -> bool:
        """Return whether playback orders the queue's upcoming items for Smart Fades per batch."""
        # a dynamic queue orders each refill batch when it adds it
        return (
            queue.shuffle_enabled
            and not queue.is_dynamic
            and self.is_enabled(queue.queue_id)
            and self.is_smart_fade_ordering_enabled(queue)
        )


def _arrange(
    items: list[QueueItem], snapshot: RecencySnapshot, windows: RecencyWindows
) -> list[QueueItem]:
    """
    Reorder items by recency tier, then spread duplicates and same-artist items within each tier.

    :param items: The queue items to reorder.
    :param snapshot: The play-history snapshot to score recency against.
    :param windows: The configured recency windows (singleton song window vs duplicate gap).
    """
    if len(items) <= 2:
        return random.sample(items, len(items))
    counts = Counter(_song_key(item) for item in items)
    tiers: dict[int, list[QueueItem]] = {0: [], 1: [], 2: []}
    for item in items:
        tiers[_tier(item, counts, snapshot, windows)].append(item)
    result: list[QueueItem] = []
    for tier in (0, 1, 2):
        if bucket := tiers[tier]:
            result.extend(_space_artists(_interleave(bucket)))
    return result


async def _arrange_for_smart_fades(
    mass: MusicAssistant,
    items: list[QueueItem],
    snapshot: RecencySnapshot,
    windows: RecencyWindows,
    *,
    preceding_item: QueueItem | None,
    song_counts: Counter[tuple[str, str]] | None = None,
) -> list[QueueItem]:
    """
    Keep recency tiers fixed and improve the transitions of the first batch of upcoming items.

    The first SMART_FADE_ORDERING_BATCH items are reordered inside their own tier; the items after
    them keep the regular smart shuffle spacing.

    :param mass: The Music Assistant instance the stored analysis is read from.
    :param items: The upcoming queue items to reorder.
    :param snapshot: The play-history snapshot to score recency against.
    :param windows: The configured recency windows (singleton song window vs duplicate gap).
    :param preceding_item: Locked item immediately before ``items``; used as the first anchor.
    :param song_counts: How often each song occurs among all upcoming items, when ``items`` is only
        a part of them; defaults to counting ``items``.
    """
    counts = song_counts if song_counts is not None else Counter(_song_key(item) for item in items)
    tiers: dict[int, list[QueueItem]] = {0: [], 1: [], 2: []}
    for item in items:
        tiers[_tier(item, counts, snapshot, windows)].append(item)

    result: list[QueueItem] = []
    preceding = preceding_item
    budget = SMART_FADE_ORDERING_BATCH
    for tier in (0, 1, 2):
        if not (bucket := tiers[tier]):
            continue
        # Spread duplicates first. Artist spacing happens in the local selector so we do
        # not reshuffle the ordered part afterwards.
        bucket = _interleave(bucket)
        ordered, rest = bucket[:budget], bucket[budget:]
        if ordered:
            ordered = await order_queue_items(
                mass,
                ordered,
                get_track=_queue_item_track,
                preceding_track=_queue_item_track(preceding) if preceding is not None else None,
            )
            budget -= len(ordered)
            preceding = ordered[-1]
        if rest:
            rest = _space_artists(
                rest, preceding=_artist_name_set(preceding) if preceding is not None else None
            )
            preceding = rest[-1]
        result.extend(ordered + rest)
    return result


def _queue_item_track(item: QueueItem | None) -> Track | None:
    """Return a queue item's Track payload, or None for a non-track boundary."""
    return item.media_item if item is not None and isinstance(item.media_item, Track) else None


def _tier(
    item: QueueItem,
    counts: Counter[tuple[str, str]],
    snapshot: RecencySnapshot,
    windows: RecencyWindows,
) -> int:
    """Return the recency tier: 0 fresh, 1 artist recently played, 2 song recently played."""
    media_item = item.media_item
    if media_item is None:
        return 0
    # a deliberately-duplicated song uses the short repeat-gap, a singleton the long song window
    song_window = (
        windows.song_seconds if counts[_song_key(item)] == 1 else windows.duplicate_gap_seconds
    )
    if snapshot.track_recent(media_item, song_window):
        return 2
    if any(snapshot.artist_recent(name, windows.artist_seconds) for name in _artist_names(item)):
        return 1
    return 0


def _interleave(bucket: list[QueueItem]) -> list[QueueItem]:
    """Spread each distinct song's copies with independently randomized repeat cycles."""
    groups: dict[tuple[str, str], list[QueueItem]] = defaultdict(list)
    for item in bucket:
        groups[_song_key(item)].append(item)
    return interleave_groups(list(groups.values()))


def _space_artists(items: list[QueueItem], *, preceding: set[str] | None = None) -> list[QueueItem]:
    """
    Best-effort separate directly-adjacent same-artist items.

    :param items: The items to space.
    :param preceding: Artist names of the item that will sit directly before the first item (the
        seam with the already-queued tail); the first item is kept clear of it too. None ignores it.
    """
    order = space_by_artist([_artist_name_set(item) for item in items], preceding=preceding)
    return [items[index] for index in order]


def _song_key(item: QueueItem) -> tuple[str, str]:
    """Return the grouping key identifying the same song (falls back to a unique id)."""
    media_item = item.media_item
    if media_item is None:
        return ("", item.queue_item_id)
    return (media_item.provider, media_item.item_id)


def _artist_names(item: QueueItem) -> list[str]:
    """Return the artist names for the item's media item (empty for non-track items)."""
    return [
        artist.name for artist in getattr(item.media_item, "artists", None) or () if artist.name
    ]


def _artist_name_set(item: QueueItem) -> set[str]:
    """Return the lowercased set of artist names for the item."""
    return {name.lower() for name in _artist_names(item)}
