"""
Managed dynamic pool for the Player Queues controller.

A queue with one or more *dynamic sources* (its ``sources``) is kept as a small bounded pool
that is topped up as it plays down. Each source has a *fill mode*:

- ``DYNAMIC`` — a dynamic playlist's own self-managing batch (a radio playlist, a station);
- ``TRACKS`` — the source's own unplayed tracks (a playlist/album/artist mixed into the pool).

Each top-up apportions slots across the sources by weight (per-base quota by default, so adding a
source more than once weights it up), gates every candidate against the shared ``RecencyEngine`` so
a recently-heard track isn't pulled back in, and prefers the least-recently-played candidates.
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from music_assistant_models.errors import MusicAssistantError
from music_assistant_models.media_items import Playlist, Track

from music_assistant.controllers.player_queues.constants import MANAGED_POOL_TARGET
from music_assistant.helpers.track_filter import track_filter

if TYPE_CHECKING:
    from music_assistant_models.media_items import MediaItemType
    from music_assistant_models.player_queue import PlayerQueue

    from music_assistant.controllers.music.recency import RecencySnapshot, RecencyWindows
    from music_assistant.controllers.player_queues.controller import PlayerQueuesController


class PoolWeightModel(StrEnum):
    """How refill slots are apportioned across the queue's dynamic sources."""

    # equal share per source, scaled only by multiplicity (times added) — size-independent
    PER_BASE_QUOTA = "per_base_quota"
    # share scales with the source's candidate count as well as its multiplicity
    SIZE_MULTIPLICITY = "size_multiplicity"


class DynamicFillMode(StrEnum):
    """How a dynamic source contributes tracks to the managed pool."""

    # a dynamic playlist (radio playlist, station): its own self-managing batch of tracks
    DYNAMIC = "dynamic"
    # a finite source (playlist/album/artist): its own unplayed tracks mixed into the pool
    TRACKS = "tracks"


# the shipped default; SIZE_MULTIPLICITY is a one-line switch if the feel needs revisiting
POOL_WEIGHT_MODEL = PoolWeightModel.PER_BASE_QUOTA


@dataclass(slots=True)
class DynamicSource:
    """A single base item feeding the managed pool, with its multiplicity, fill mode + candidates."""

    media_item: MediaItemType
    multiplicity: int
    fill_mode: DynamicFillMode
    candidates: list[Track] = field(default_factory=list)


class ManagedPoolHelper:
    """Build and top up a queue's bounded managed pool from its dynamic sources."""

    def __init__(self, queues: PlayerQueuesController) -> None:
        """
        Initialize the managed pool helper.

        :param queues: The owning player queues controller.
        """
        self.queues = queues
        self.mass = queues.mass
        self.logger = queues.logger.getChild("managed_pool")

    async def fill(self, queue_id: str, *, is_initial: bool) -> list[Track]:
        """
        Build (or top up) the managed pool and return the tracks to add.

        :param queue_id: The queue to fill.
        :param is_initial: True when seeding a fresh pool, False when topping up an existing one.
        """
        queue = self.queues._queues[queue_id]
        windows = self.queues._smart_shuffle._windows(queue_id)
        snapshot = await self.mass.music.recency.snapshot(windows, userid=queue.userid)
        # publish a best-effort recency filter so dynamic-playlist generation can pre-skip recently
        # played tracks while over-generating; allocate_refill still applies the authoritative gate
        with track_filter(lambda track: not snapshot.track_recent(track, windows.song_seconds)):
            # the initial fill skips dynamic playlists (each seeds its own first batch directly); a
            # top-up folds them in so all sources are mixed, weighted and recency-gated together
            sources = await self._collect_sources(queue_id, queue, include_dynamic=not is_initial)
        if not sources:
            return []
        # dedupe only against the active (current + unplayed) tail; played history is left out so
        # recency, not permanent exclusion, decides when a track may return
        items = self.queues._queue_items[queue_id]
        start = queue.current_index if queue.current_index is not None else 0
        pool_keys = {
            item.media_item for item in items[start:] if isinstance(item.media_item, Track)
        }
        slots = (
            MANAGED_POOL_TARGET
            if is_initial
            else max(MANAGED_POOL_TARGET - self._unplayed(queue), 0)
        )
        return allocate_refill(
            sources, slots=slots, pool_keys=pool_keys, snapshot=snapshot, windows=windows
        )

    async def _collect_sources(
        self, queue_id: str, queue: PlayerQueue, *, include_dynamic: bool
    ) -> list[DynamicSource]:
        """
        Group the queue's source items into dynamic sources and fetch each one's candidates.

        :param queue_id: The queue being filled.
        :param queue: The queue being filled.
        :param include_dynamic: Whether to include dynamic-playlist sources; skipped on the initial
            fill, where each dynamic playlist seeds its own first batch directly.
        """
        # multiplicity = how often a source was added (adding a source more than once weights it up)
        counts: dict[str, int] = {}
        items: dict[str, MediaItemType] = {}
        for item in self.queues._source_items.get(queue_id, []):
            if isinstance(item, Playlist) and item.is_dynamic and not include_dynamic:
                continue
            if not (uri := _uri(item)):
                continue
            counts[uri] = counts.get(uri, 0) + 1
            items.setdefault(uri, item)
        if not items:
            return []
        preferred = await self._preferred_providers(queue)
        sources: list[DynamicSource] = []
        for uri, media_item in items.items():
            if isinstance(media_item, Playlist) and media_item.is_dynamic:
                fill_mode = DynamicFillMode.DYNAMIC
                candidates = await self._fetch_dynamic(media_item)
            else:
                fill_mode = DynamicFillMode.TRACKS
                candidates = await self._fetch_tracks(media_item, preferred=preferred)
            sources.append(
                DynamicSource(
                    media_item=media_item,
                    multiplicity=counts[uri],
                    fill_mode=fill_mode,
                    candidates=candidates,
                )
            )
        return sources

    async def _fetch_dynamic(self, media_item: MediaItemType) -> list[Track]:
        """Fetch the next self-managed batch from a dynamic playlist (radio playlist, station)."""
        if not isinstance(media_item, Playlist):
            return []
        with suppress(MusicAssistantError):
            tracks = await self.queues.get_playlist_tracks(media_item, start_item=None)
            return [track for track in tracks if isinstance(track, Track) and track.available]
        return []

    async def _fetch_tracks(
        self, media_item: MediaItemType, *, preferred: list[str] | None
    ) -> list[Track]:
        """Fetch a TRACKS source's own (playable) tracks via its media controller."""
        controller = self.mass.music.get_controller(media_item.media_type)
        with suppress(MusicAssistantError):
            tracks = await controller.base_tracks(media_item, preferred)  # type: ignore[arg-type]
            return [track for track in tracks if isinstance(track, Track) and track.available]
        return []

    async def _preferred_providers(self, queue: PlayerQueue) -> list[str] | None:
        """Return the queue owner's preferred provider instances, if any."""
        if (
            queue.userid
            and (user := await self.mass.webserver.auth.get_user(queue.userid))
            and user.provider_filter
        ):
            return user.provider_filter
        return None

    def _unplayed(self, queue: PlayerQueue) -> int:
        """Return how many not-yet-played items remain in the queue."""
        items = self.queues._queue_items.get(queue.queue_id, [])
        if queue.current_index is None:
            return len(items)
        return max(len(items) - (queue.current_index + 1), 0)


def allocate_refill(
    sources: list[DynamicSource],
    *,
    slots: int,
    pool_keys: set[Track],
    snapshot: RecencySnapshot,
    windows: RecencyWindows,
    weight_model: PoolWeightModel = POOL_WEIGHT_MODEL,
) -> list[Track]:
    """
    Pick the next batch of tracks for the managed pool, weighted per source and recency-gated.

    Slots are apportioned across sources by weight; each source's candidates are hard-gated against
    the recency snapshot (a within-window track is excluded entirely) and ordered least-recently-
    played first. If gating leaves nothing, an ungated least-recently-played fallback is returned so
    playback never stalls.

    :param sources: The queue's dynamic sources, each with its already-fetched candidate tracks.
    :param slots: How many tracks to add (0 or fewer returns nothing).
    :param pool_keys: Tracks already in the queue, to avoid immediate repeats.
    :param snapshot: The play-history snapshot to gate/score against.
    :param windows: The configured recency windows.
    :param weight_model: How to weight sources against each other.
    """
    if slots <= 0 or not sources:
        return []
    eligible = [_eligible(source, pool_keys, snapshot, windows) for source in sources]
    weights = [max(_weight(source, weight_model), 0) for source in sources]
    total_weight = sum(weights) or len(sources)
    shares = [slots * (weight or 0) / total_weight for weight in weights]
    taken = [0.0] * len(sources)
    pointers = [0] * len(sources)
    chosen: list[Track] = []
    chosen_set: set[Track] = set()
    for _ in range(slots):
        best_index = -1
        best_deficit = 0.0
        for index in range(len(sources)):
            # skip candidates already chosen for another source this round
            while pointers[index] < len(eligible[index]) and (
                eligible[index][pointers[index]] in chosen_set
            ):
                pointers[index] += 1
            if pointers[index] >= len(eligible[index]):
                continue
            deficit = shares[index] - taken[index]
            if best_index == -1 or deficit > best_deficit:
                best_index, best_deficit = index, deficit
        if best_index == -1:
            break
        track = eligible[best_index][pointers[best_index]]
        chosen.append(track)
        chosen_set.add(track)
        taken[best_index] += 1
        pointers[best_index] += 1
    return chosen or _ungated_fallback(sources, slots, pool_keys, snapshot)


def gate_tracks(
    tracks: list[Track],
    snapshot: RecencySnapshot,
    windows: RecencyWindows,
    *,
    duplicate: bool = False,
) -> list[Track]:
    """
    Drop tracks played within the recency window, keeping the original order.

    Falls back to the ungated list when every track is blocked, so a refill never returns empty.

    :param tracks: The candidate tracks to filter.
    :param snapshot: The play-history snapshot to gate against.
    :param windows: The configured recency windows.
    :param duplicate: True to gate on the duplicate repeat-gap instead of the song window.
    """
    window = windows.duplicate_gap_seconds if duplicate else windows.song_seconds
    if not window:
        return list(tracks)
    kept = [track for track in tracks if not snapshot.track_recent(track, window)]
    return kept or list(tracks)


def _eligible(
    source: DynamicSource,
    pool_keys: set[Track],
    snapshot: RecencySnapshot,
    windows: RecencyWindows,
) -> list[Track]:
    """Return a source's candidates minus pool/recency-blocked ones, least-recently-played first."""
    # a deliberately-duplicated source uses the short repeat-gap, a singleton the long song window
    window = windows.duplicate_gap_seconds if source.multiplicity > 1 else windows.song_seconds
    scored: list[tuple[int, int, Track]] = []
    for track in source.candidates:
        if track in pool_keys:
            continue
        last_played = snapshot.last_played(track)
        if window and last_played is not None and last_played >= snapshot.now - window:
            continue  # hard recency gate: a within-window track is excluded entirely
        # never-played (None) sorts ahead of played; then oldest play first
        scored.append((0 if last_played is None else 1, last_played or 0, track))
    scored.sort(key=lambda entry: (entry[0], entry[1]))
    return [track for _, _, track in scored]


def _weight(source: DynamicSource, weight_model: PoolWeightModel) -> int:
    """Return a source's refill weight under the configured weight model."""
    if weight_model == PoolWeightModel.SIZE_MULTIPLICITY:
        return max(len(source.candidates), 1) * source.multiplicity
    return source.multiplicity


def _ungated_fallback(
    sources: list[DynamicSource], slots: int, pool_keys: set[Track], snapshot: RecencySnapshot
) -> list[Track]:
    """Return the globally least-recently-played candidates, ignoring the recency gate."""
    seen: set[Track] = set()
    pool: list[Track] = []
    for source in sources:
        for track in source.candidates:
            if track in pool_keys or track in seen:
                continue
            seen.add(track)
            pool.append(track)
    pool.sort(
        key=lambda track: (
            0 if snapshot.last_played(track) is None else 1,
            snapshot.last_played(track) or 0,
        )
    )
    return pool[:slots]


def _uri(media_item: MediaItemType) -> str | None:
    """Return a media item's URI, or None when it has none."""
    return getattr(media_item, "uri", None)
