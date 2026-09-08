"""
Managed dynamic pool for the Player Queues controller.

A queue with one or more *dynamic sources* (its ``sources``) is kept as a small bounded pool
that is topped up as it plays down. Each source has a *fill mode*:

- ``DYNAMIC`` — a dynamic playlist's own self-managing batch (a radio playlist, a station): a fresh
  batch is pulled every refill, so it never runs dry;
- ``TRACKS`` — a finite source (a playlist/album/artist mixed into the pool): its tracks are
  materialized once into a per-source deque and dequeued progressively, so the source plays through
  once and then drops out rather than recycling the same tracks as they age out of the recency
  window. A recency-denied track rotates to the back of its deque (a fair second chance) instead of
  being dropped, and the deque is bounded (paging in more as it drains) so a huge source can't
  balloon internal state.

Each top-up apportions slots across the sources by weight (per-base quota by default, so adding a
source more than once weights it up) and gates every candidate against the shared ``RecencyEngine``
so a recently-heard track isn't pulled back in. A dynamic batch is offered least-recently-played
first and de-prioritizes tracks whose artist is within the artist-recency window (a soft nudge, not
a hard exclusion, so a single-artist station still plays); a finite source keeps its own
(materialized) order so it plays through coherently. The selected source groups are randomly
interleaved across the batch before a seam-aware artist-spacing pass best-effort keeps
directly-adjacent tracks from sharing an artist, including the currently-queued tail's last artist.
"""

from __future__ import annotations

from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from music_assistant_models.errors import MusicAssistantError
from music_assistant_models.media_items import Track

from music_assistant.controllers.music.recency import song_keys
from music_assistant.controllers.player_queues.constants import (
    MANAGED_POOL_SOURCE_CAP,
    MANAGED_POOL_TARGET,
)
from music_assistant.controllers.player_queues.helpers import (
    interleave_groups,
    is_dynamic_source,
    space_by_artist,
)
from music_assistant.controllers.player_queues.smart_fade_ordering import order_tracks
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


@dataclass(slots=True)
class _MaterializedSource:
    """
    A finite (TRACKS) source materialized once and dequeued progressively across refills.

    ``tracks`` holds the not-yet-dispatched tracks (bounded by ``MANAGED_POOL_SOURCE_CAP``);
    ``dispatched`` records the item ids already handed to the queue so they are never re-offered and
    so a larger-than-cap source can page the rest in without repeats; ``fully_paged`` is True once
    the source has no further tracks to page in (it is exhausted when ``tracks`` is then empty).
    """

    media_item: MediaItemType
    multiplicity: int
    tracks: deque[Track]
    dispatched: set[str]
    fully_paged: bool


class ManagedPool:
    """Build and top up a queue's bounded managed pool from its dynamic sources."""

    def __init__(self, queues: PlayerQueuesController) -> None:
        """
        Initialize the managed pool helper.

        :param queues: The owning player queues controller.
        """
        self.queues = queues
        self.mass = queues.mass
        self.logger = queues.logger.getChild("managed_pool")
        # finite-source materialized state, keyed by queue id then source uri; see _MaterializedSource
        self._materialized: dict[str, dict[str, _MaterializedSource]] = {}

    async def fill(self, queue_id: str, *, is_initial: bool) -> list[Track]:
        """
        Build (or top up) the managed pool and return the tracks to add.

        :param queue_id: The queue to fill.
        :param is_initial: True when seeding a fresh pool, False when topping up an existing one.
        """
        if (queue_data := self.queues.queue_data_or_none(queue_id)) is None:
            # the queue was removed while this background refill was starting up
            return []
        queue = queue_data.queue
        windows = self.queues.recency_windows()
        snapshot = await self.mass.music.recency.snapshot(windows, userid=queue_data.userid)
        # publish a best-effort recency filter so dynamic-playlist generation can pre-skip recently
        # played tracks while over-generating; allocate_refill still applies the authoritative gate
        with track_filter(lambda track: not snapshot.track_recent(track, windows.song_seconds)):
            # the initial fill skips dynamic playlists (each seeds its own first batch directly); a
            # top-up folds them in so all sources are mixed, weighted and recency-gated together
            sources = await self._collect_sources(queue_id, include_dynamic=not is_initial)
        if not sources:
            return []
        # dedupe only against the active (current + unplayed) tail; played history is left out so
        # recency, not permanent exclusion, decides when a track may return. Besides exact item
        # identity we also dedupe on fuzzy same-song keys (title + artist) so a different
        # release/version of an already-queued song is not pulled in as well.
        items = queue_data.items
        start = queue.current_index if queue.current_index is not None else 0
        pool_keys: set[Track] = set()
        pool_song_keys: set[tuple[str, str]] = set()
        for item in items[start:]:
            if isinstance(item.media_item, Track):
                pool_keys.add(item.media_item)
                pool_song_keys.update(song_keys(item.media_item))
        slots = (
            MANAGED_POOL_TARGET
            if is_initial
            else max(MANAGED_POOL_TARGET - self._unplayed(queue), 0)
        )
        # the batch is appended after the current tail, so keep its first track clear of the last
        # queued item's artist (the seam the listener actually hears)
        preceding_track = (
            items[-1].media_item if items and isinstance(items[-1].media_item, Track) else None
        )
        preceding = _track_artist_set(preceding_track) if preceding_track is not None else set()
        chosen = allocate_refill(
            sources,
            slots=slots,
            pool_keys=pool_keys,
            pool_song_keys=pool_song_keys,
            snapshot=snapshot,
            windows=windows,
            preceding_artists=preceding,
        )
        if chosen and self.queues.smart_fade_ordering_enabled(queue):
            # Dynamic Mode already picked the refill tracks. Reorder only that
            # batch, starting from the queue tail.
            chosen = await order_tracks(
                self.mass,
                chosen,
                preceding_track=preceding_track,
            )
        # Keep the existing finite-source bookkeeping after ordering: mark dispatched tracks,
        # page more in and retire exhausted sources.
        await self._reconcile_tracks(queue_id, sources, chosen, snapshot, windows)
        return chosen

    def forget(self, queue_id: str) -> None:
        """Drop all materialized finite-source state for a queue (it was cleared or removed)."""
        self._materialized.pop(queue_id, None)

    def retain(self, queue_id: str, uris: set[str]) -> None:
        """
        Prune materialized finite-source state down to the queue's current source uris.

        Called whenever the queue's sources change so an exhausted/removed source releases its
        materialized tracks instead of lingering until the queue is torn down.

        :param queue_id: The queue whose sources changed.
        :param uris: The uris of the sources the queue still has.
        """
        if not (materialized := self._materialized.get(queue_id)):
            return
        for uri in [uri for uri in materialized if uri not in uris]:
            del materialized[uri]
        if not materialized:
            self._materialized.pop(queue_id, None)

    async def _collect_sources(
        self, queue_id: str, *, include_dynamic: bool
    ) -> list[DynamicSource]:
        """
        Group the queue's source items into dynamic sources and fetch each one's candidates.

        :param queue_id: The queue being filled.
        :param include_dynamic: Whether to include dynamic sources; skipped on the initial
            fill, where each dynamic source seeds its own first batch directly.
        """
        if (queue_data := self.queues.queue_data_or_none(queue_id)) is None:
            # the queue was removed while earlier fetches of this refill were awaited
            return []
        # multiplicity = how often a source was added (adding a source more than once weights it up)
        counts: dict[str, int] = {}
        items: dict[str, MediaItemType] = {}
        for item in queue_data.source_items:
            if is_dynamic_source(item) and not include_dynamic:
                continue
            if not (uri := _uri(item)):
                continue
            counts[uri] = counts.get(uri, 0) + 1
            items.setdefault(uri, item)
        if not items:
            return []
        materialized = self._materialized.setdefault(queue_id, {})
        sources: list[DynamicSource] = []
        for uri, media_item in items.items():
            if is_dynamic_source(media_item):
                fill_mode = DynamicFillMode.DYNAMIC
                candidates = await self._fetch_dynamic(media_item)
            else:
                # a finite source is materialized once, then its deque feeds (and is advanced by)
                # every refill so it plays through instead of recycling tracks that age out
                fill_mode = DynamicFillMode.TRACKS
                source = materialized.get(uri)
                if source is None:
                    source = await self._materialize(media_item, counts[uri])
                    materialized[uri] = source
                else:
                    source.multiplicity = counts[uri]
                candidates = list(source.tracks)
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
        """Fetch the next self-managed batch from a dynamic playlist or radio station."""
        with suppress(MusicAssistantError):
            tracks = await self.queues.get_dynamic_source_tracks(media_item)
            return [track for track in tracks if track.available]
        return []

    async def _fetch_tracks(self, media_item: MediaItemType) -> list[Track]:
        """Fetch a TRACKS source's own (playable) tracks, honoring the user's selection prefs."""
        with suppress(MusicAssistantError):
            tracks = await self.queues.get_tracks_for_playback(media_item)
            return [track for track in tracks if isinstance(track, Track) and track.available]
        return []

    async def _materialize(
        self, media_item: MediaItemType, multiplicity: int
    ) -> _MaterializedSource:
        """Fetch a finite source's tracks once into a bounded deque for progressive dequeue."""
        tracks = await self._fetch_tracks(media_item)
        return _MaterializedSource(
            media_item=media_item,
            multiplicity=multiplicity,
            tracks=deque(tracks[:MANAGED_POOL_SOURCE_CAP]),
            dispatched=set(),
            # everything fetched already fits: there is nothing more to page in beyond this deque
            fully_paged=len(tracks) <= MANAGED_POOL_SOURCE_CAP,
        )

    async def _reconcile_tracks(
        self,
        queue_id: str,
        sources: list[DynamicSource],
        chosen: list[Track],
        snapshot: RecencySnapshot,
        windows: RecencyWindows,
    ) -> None:
        """Advance each finite source's deque for what this refill took, then page/retire it."""
        materialized = self._materialized.get(queue_id)
        if not materialized:
            return
        chosen_ids = {track.item_id for track in chosen}
        exhausted: list[str] = []
        for source in sources:
            if source.fill_mode != DynamicFillMode.TRACKS:
                continue
            if not (uri := _uri(source.media_item)) or (mat := materialized.get(uri)) is None:
                continue
            # a duplicated source repeats on the short gap, a singleton on the long song window
            window = windows.duplicate_gap_seconds if mat.multiplicity > 1 else windows.song_seconds
            kept: deque[Track] = deque()
            denied: deque[Track] = deque()
            for track in mat.tracks:
                if track.item_id in chosen_ids:
                    mat.dispatched.add(track.item_id)  # handed to the queue: never offer it again
                elif window and snapshot.track_recent(track, window):
                    denied.append(track)  # too recent for now: a fair second chance, at the back
                else:
                    kept.append(track)
            kept.extend(denied)
            mat.tracks = kept
            if len(mat.tracks) < MANAGED_POOL_TARGET and not mat.fully_paged:
                await self._page_in(mat)
            if not mat.tracks and mat.fully_paged:
                exhausted.append(uri)
        for uri in exhausted:
            materialized.pop(uri, None)
        if not materialized:
            self._materialized.pop(queue_id, None)
        if exhausted:
            self._retire_sources(queue_id, exhausted)

    async def _page_in(self, mat: _MaterializedSource) -> None:
        """Pull the next as-yet-unseen tracks from a larger-than-cap source as its deque drains."""
        have = {track.item_id for track in mat.tracks}
        for track in await self._fetch_tracks(mat.media_item):
            if len(mat.tracks) >= MANAGED_POOL_SOURCE_CAP:
                break
            if track.item_id in mat.dispatched or track.item_id in have:
                continue
            mat.tracks.append(track)
            have.add(track.item_id)
        # could not refill toward the cap: every remaining track has been seen, so it is fully paged
        if len(mat.tracks) < MANAGED_POOL_SOURCE_CAP:
            mat.fully_paged = True

    def _retire_sources(self, queue_id: str, uris: list[str]) -> None:
        """Drop fully-played finite sources from the queue so only live sources remain."""
        queue = self.queues.get(queue_id)
        if queue is None:
            return
        retire = set(uris)
        remaining = [
            item
            for item in self.queues.queue_data(queue_id).source_items
            if _uri(item) not in retire
        ]
        self.queues.store_sources(queue, remaining)
        self.logger.debug(
            "Finite source(s) exhausted on queue %s; %d source(s) remain", queue_id, len(remaining)
        )
        self.queues.signal_update(queue_id)

    def _unplayed(self, queue: PlayerQueue) -> int:
        """Return how many not-yet-played items remain in the queue."""
        items = (
            queue_data.items
            if (queue_data := self.queues.queue_data_or_none(queue.queue_id))
            else []
        )
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
    preceding_artists: set[str] | None = None,
    pool_song_keys: set[tuple[str, str]] | None = None,
) -> list[Track]:
    """
    Pick the next batch of tracks for the managed pool, weighted per source and recency-gated.

    Slots are apportioned across sources by weight; each source's candidates are hard-gated against
    the recency snapshot (a within-window track is excluded entirely). A dynamic batch is ordered
    least-recently-played first and de-prioritizes recently-heard artists; a finite (TRACKS) source
    keeps its own materialized order. If gating leaves nothing, an ungated least-recently-played
    fallback is returned so playback never stalls. The selected source groups are randomly
    interleaved while retaining each source's candidate order, then spaced so no two adjacent tracks
    share an artist.

    :param sources: The queue's dynamic sources, each with its already-fetched candidate tracks.
    :param slots: How many tracks to add (0 or fewer returns nothing).
    :param pool_keys: Tracks already in the queue, to avoid immediate repeats.
    :param snapshot: The play-history snapshot to gate/score against.
    :param windows: The configured recency windows.
    :param weight_model: How to weight sources against each other.
    :param preceding_artists: Artist names of the track that will sit directly before the batch (the
        seam with the current tail); the first added track is kept clear of it.
    :param pool_song_keys: Fuzzy same-song keys of the tracks already in the queue, so a different
        release/version of an already-queued song is skipped as well.
    """
    if slots <= 0 or not sources:
        return []
    pool_song_keys = pool_song_keys or set()
    eligible = [
        _eligible(source, pool_keys, pool_song_keys, snapshot, windows) for source in sources
    ]
    weights = [max(_weight(source, weight_model), 0) for source in sources]
    total_weight = sum(weights) or len(sources)
    shares = [slots * (weight or 0) / total_weight for weight in weights]
    taken = [0.0] * len(sources)
    pointers = [0] * len(sources)
    chosen_by_source: list[list[Track]] = [[] for _ in sources]
    chosen_set: set[Track] = set()
    chosen_song_keys: set[tuple[str, str]] = set()
    for _ in range(slots):
        best_index = -1
        best_deficit = 0.0
        for index in range(len(sources)):
            # skip candidates already chosen for another source this round (also as a
            # different release/version of the same song)
            while pointers[index] < len(eligible[index]) and (
                eligible[index][pointers[index]] in chosen_set
                or not chosen_song_keys.isdisjoint(song_keys(eligible[index][pointers[index]]))
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
        chosen_by_source[best_index].append(track)
        chosen_set.add(track)
        chosen_song_keys.update(song_keys(track))
        taken[best_index] += 1
        pointers[best_index] += 1
    batch = (
        interleave_groups(chosen_by_source)
        if chosen_set
        else _ungated_fallback(sources, slots, pool_keys, pool_song_keys, snapshot)
    )
    return _space_tracks(batch, preceding_artists)


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
    pool_song_keys: set[tuple[str, str]],
    snapshot: RecencySnapshot,
    windows: RecencyWindows,
) -> list[Track]:
    """
    Return a source's candidates minus pool/recency-blocked ones.

    A finite (TRACKS) source keeps its materialized deque order so it plays through coherently; a
    dynamic batch is ordered fresh-artist first (recently-heard artists nudged back), then
    least-recently-played.
    """
    # a deliberately-duplicated source uses the short repeat-gap, a singleton the long song window
    window = windows.duplicate_gap_seconds if source.multiplicity > 1 else windows.song_seconds
    if source.fill_mode == DynamicFillMode.TRACKS:
        return [
            track
            for track in source.candidates
            if track not in pool_keys
            and pool_song_keys.isdisjoint(song_keys(track))
            and not snapshot.track_recent(track, window)
        ]
    scored: list[tuple[int, int, int, Track]] = []
    for track in source.candidates:
        if track in pool_keys or not pool_song_keys.isdisjoint(song_keys(track)):
            continue
        last_played = snapshot.last_played(track)
        if window and last_played is not None and last_played >= snapshot.now - window:
            continue  # hard recency gate: a within-window track is excluded entirely
        # a within-artist-window track sorts behind fresh-artist ones (soft, so a single-artist
        # station still plays); then never-played (None) ahead of played; then oldest play first
        artist_recent = any(
            snapshot.artist_recent(artist.name, windows.artist_seconds)
            for artist in track.artists
            if artist.name
        )
        scored.append(
            (1 if artist_recent else 0, 0 if last_played is None else 1, last_played or 0, track)
        )
    scored.sort(key=lambda entry: (entry[0], entry[1], entry[2]))
    return [entry[3] for entry in scored]


def _weight(source: DynamicSource, weight_model: PoolWeightModel) -> int:
    """Return a source's refill weight under the configured weight model."""
    if weight_model == PoolWeightModel.SIZE_MULTIPLICITY:
        return max(len(source.candidates), 1) * source.multiplicity
    return source.multiplicity


def _ungated_fallback(
    sources: list[DynamicSource],
    slots: int,
    pool_keys: set[Track],
    pool_song_keys: set[tuple[str, str]],
    snapshot: RecencySnapshot,
) -> list[Track]:
    """Return the globally least-recently-played candidates, ignoring the recency gate."""
    seen: set[Track] = set()
    seen_song_keys = set(pool_song_keys)
    pool: list[Track] = []
    for source in sources:
        for track in source.candidates:
            if (
                track in pool_keys
                or track in seen
                or not seen_song_keys.isdisjoint(track_song_keys := song_keys(track))
            ):
                continue
            seen.add(track)
            seen_song_keys.update(track_song_keys)
            pool.append(track)
    pool.sort(
        key=lambda track: (
            0 if snapshot.last_played(track) is None else 1,
            snapshot.last_played(track) or 0,
        )
    )
    return pool[:slots]


def _space_tracks(tracks: list[Track], preceding: set[str] | None) -> list[Track]:
    """
    Reorder the batch to best-effort keep directly-adjacent tracks from sharing an artist.

    :param tracks: The assembled batch to space.
    :param preceding: Artist names of the track that will sit directly before the batch (the seam
        with the current tail); the first track is kept clear of it too.
    """
    order = space_by_artist([_track_artist_set(track) for track in tracks], preceding=preceding)
    return [tracks[index] for index in order]


def _track_artist_set(track: Track) -> set[str]:
    """Return the lowercased set of artist names for a track."""
    return {artist.name.lower() for artist in track.artists if artist.name}


def _uri(media_item: MediaItemType) -> str | None:
    """Return a media item's URI, or None when it has none."""
    return getattr(media_item, "uri", None)
