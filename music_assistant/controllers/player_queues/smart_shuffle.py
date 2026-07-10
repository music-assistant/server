"""
Smart shuffle helper for the Player Queues controller.

Smart shuffle reorders the upcoming queue items so recently-heard music is pushed toward the back
and same songs/artists are spread out, while honouring intentionally-duplicated items. It reads the
configured recency windows for a queue, takes a single play-history snapshot from the shared recency
engine, and runs the pure ``_arrange`` algorithm. Plain (non-smart) shuffle stays a pure random
shuffle in the controller.

The algorithm works in two stages:
- recency tiering: each item is fresh / artist-recently-played / song-recently-played, where a
  deliberately-duplicated song uses the short duplicate repeat-gap instead of the long song window;
- within-tier interleave: each distinct song's copies are placed at evenly-spaced fractional
  positions with a random phase so duplicates spread across the whole span without clustering, then
  a bounded pass separates directly-adjacent same-artist items.
"""

from __future__ import annotations

import random
from collections import Counter, defaultdict
from typing import TYPE_CHECKING

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
    CONF_SMART_SHUFFLE_SONG_RECENCY,
    SMART_SHUFFLE_ARTIST_RECENCY_DEFAULT,
    SMART_SHUFFLE_DUPLICATE_GAP_DEFAULT,
    SMART_SHUFFLE_SONG_RECENCY_DEFAULT,
)
from music_assistant.controllers.player_queues.helpers import space_by_artist

if TYPE_CHECKING:
    from music_assistant_models.player_queue import PlayerQueue
    from music_assistant_models.queue_item import QueueItem

    from music_assistant.controllers.music.recency import RecencySnapshot
    from music_assistant.controllers.player_queues.controller import PlayerQueuesController


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

    async def arrange(self, queue: PlayerQueue, items: list[QueueItem]) -> list[QueueItem]:
        """
        Return the items reordered with recency-aware smart shuffle.

        :param queue: The queue being (re)shuffled; its owner scopes the play history.
        :param items: The upcoming queue items to reorder.
        """
        windows = self.windows()
        snapshot = await self.mass.music.recency.snapshot(
            windows, userid=self.queues.queue_data(queue.queue_id).userid
        )
        return _arrange(items, snapshot, windows)

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
    """Spread each distinct song's copies evenly with a random phase so groups don't clump."""
    groups: dict[tuple[str, str], list[QueueItem]] = defaultdict(list)
    for item in bucket:
        groups[_song_key(item)].append(item)
    positioned: list[tuple[float, QueueItem]] = []
    for copies in groups.values():
        phase = random.random()
        total = len(copies)
        for offset, item in enumerate(copies):
            positioned.append(((phase + offset / total) % 1.0, item))
    positioned.sort(key=lambda entry: entry[0])
    return [item for _, item in positioned]


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
