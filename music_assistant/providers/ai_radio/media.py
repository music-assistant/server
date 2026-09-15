"""Media item surface for AI Radio: shows exposed as dynamic Radio items."""
# mypy: disable-error-code=attr-defined

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import ImageType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    MediaItemImage,
    ProviderMapping,
    Radio,
    Track,
    UniqueList,
)

from .constants import FALLBACK_TRACK_SECONDS, SHOW_FEED_PAGE_SIZE

if TYPE_CHECKING:
    from music_assistant_models.player_queue import PlayerQueue
    from music_assistant_models.queue_item import QueueItem

    from music_assistant.mass import MusicAssistant


@dataclass(slots=True)
class _ShowRun:
    """One in-flight play-through of a show: its track snapshot and feed cursor."""

    # the tracks this run still has to feed
    tracks: list[Track]
    # the queue playing this run: a run only ever exists for a queue that sources the show
    queue_id: str
    cursor: int = 0
    # True once the queue DJ has been auto-armed for this run, so a detach (queue ended, show
    # left the sources) or a manual disable is never immediately re-armed by a later event
    dj_armed: bool = False

    @property
    def exhausted(self) -> bool:
        """Return True when the snapshot has been fully served."""
        return self.cursor >= len(self.tracks)


class AIRadioMediaMixin:
    """Mixin exposing AI Radio shows as library-backed dynamic Radio media items."""

    if TYPE_CHECKING:
        mass: MusicAssistant
        logger: logging.Logger
        _stations: dict[str, dict[str, Any]]
        _show_runs: dict[str, _ShowRun]
        _show_runs_lock: asyncio.Lock
        _show_library_ids: dict[str, str]

        async def _fetch_source_tracks(
            self, station: dict[str, Any]
        ) -> tuple[list[dict[str, Any]], str]: ...
        def _dj_queue_items(self, queue_id: str) -> list[QueueItem]: ...

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """
        Return the Radio media item for one of this provider's shows.

        :param prov_radio_id: The station id of the show.
        """
        station = self._stations.get(prov_radio_id)
        if station is None:
            raise MediaNotFoundError(f"AI Radio show {prov_radio_id} not found")
        return self._station_to_radio(station)

    async def get_dynamic_radio_tracks(
        self, prov_radio_id: str, *, sample: bool = False
    ) -> list[Track]:
        """
        Return the next feed page for a playing show, or a preview batch for a sample.

        The consume path pages through a run bound to the queue playing the show (the
        first call starts it, or resumes it behind the show tracks the queue already
        holds); the empty batch after the last page is what ends the show's feed. A
        sample gets a fresh preview slice that consumes nothing.

        :param prov_radio_id: The station id of the show.
        :param sample: True returns a preview batch that must not mutate any
            playback state.
        """
        station = self._stations.get(prov_radio_id)
        if station is None:
            raise MediaNotFoundError(f"AI Radio show {prov_radio_id} not found")
        if sample:
            # a browse/details preview: never touches the playback run
            tracks = await self._snapshot_show_tracks(station)
            return tracks[:SHOW_FEED_PAGE_SIZE]
        # snapshotting awaits, so two concurrent first-calls for the same station
        # must not both pass the "no active run" check and each start their own run
        async with self._show_runs_lock:
            run = self._show_runs.get(prov_radio_id)
            if run is None:
                tracks = await self._snapshot_show_tracks(station)
                # resolved after the snapshot: a queue found before it could have been
                # cleared or removed while the fetch was in flight
                queue = self._find_show_queue(prov_radio_id)
                if queue is None:
                    # a stray fetch with no queue sourcing the show: serve a one-off
                    # batch, since there is no queue to bind a run to
                    return tracks[:SHOW_FEED_PAGE_SIZE]
                run = self._start_show_run(tracks, queue)
                self._show_runs[prov_radio_id] = run
            page = run.tracks[run.cursor : run.cursor + SHOW_FEED_PAGE_SIZE]
            run.cursor += len(page)
            return page

    def _station_to_radio(self, station: dict[str, Any]) -> Radio:
        """Build the Radio media item for a station."""
        station_id = str(station["id"])
        radio = Radio(
            item_id=station_id,
            provider=self.instance_id,
            name=str(station["name"]),
            is_dynamic=True,
            provider_mappings={
                ProviderMapping(
                    item_id=station_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    is_unique=True,
                )
            },
        )
        radio.metadata.images = UniqueList(
            [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=self._ai_radio_cover_image_path(),
                    provider="builtin",
                    remotely_accessible=False,
                )
            ]
        )
        return radio

    async def _sync_show_library_items(self) -> None:
        """Mirror all shows into the library and prune rows of deleted shows."""
        radio_ctrl = self.mass.music.radio
        show_library_ids: dict[str, str] = {}
        for station in self._stations.values():
            prov_item = self._station_to_radio(station)
            for prov_map in prov_item.provider_mappings:
                prov_map.in_library = True
            library_item = await radio_ctrl.get_library_item_by_prov_mappings(
                prov_item.provider_mappings
            )
            if library_item is None:
                library_item = await radio_ctrl.add_item_to_library(prov_item)
            elif prov_item.name != library_item.name:
                # must overwrite: merging keeps mappings that serve the wrong tracks
                library_item = await radio_ctrl.update_item_in_library(
                    library_item.item_id, prov_item, overwrite=True
                )
            show_library_ids[str(library_item.item_id)] = str(station["id"])
        # queue sources name shows by their library identity, so the map resolving
        # them back to a station is rebuilt alongside the rows themselves
        self._show_library_ids = show_library_ids
        # deletions are collected first: deleting rows mid-pagination can skip rows
        prune_db_ids = [
            library_radio.item_id
            async for library_radio in radio_ctrl.iter_library_items(provider=self.instance_id)
            if str(library_radio.item_id) not in show_library_ids
        ]
        for db_id in prune_db_ids:
            await radio_ctrl.remove_item_from_library(db_id)

    def _end_show_run(self, station_id: str) -> None:
        """Drop the station's active run so a replay starts fresh."""
        self._show_runs.pop(station_id, None)

    def _find_show_queue(self, station_id: str) -> PlayerQueue | None:
        """
        Return the live queue sourcing this show, preferring the one being filled right now.

        A show has one run at a time, so when two queues play the same show at once the
        second queue shares the first one's feed instead of getting a run of its own.
        """
        # an ended queue keeps its sources, so a persisted one must not shadow the queue
        # that is starting the show now
        candidates = [
            queue
            for queue in self.mass.player_queues.all()
            if not queue.ended
            and any(
                self._station_id_from_source_uri(source.uri) == station_id
                for source in queue.sources
            )
        ]
        # a queue's first pool fetch happens on an emptied queue, so among several live
        # ones the empty one is the requester
        return next(
            (queue for queue in candidates if queue.items == 0),
            candidates[0] if candidates else None,
        )

    def _station_id_from_source_uri(self, uri: str | None) -> str | None:
        """Return the station id a queue source uri points at, if it is one of our shows."""
        if not uri:
            return None
        # matches the prefix instance_id::create_uri actually stamps on a show's Radio item,
        # not the provider domain, so a second AI Radio instance is matched correctly too
        prefix = f"{self.instance_id}://radio/"
        if uri.startswith(prefix):
            return uri.removeprefix(prefix)
        # shows are library-backed, so a queue's sources usually name the library item
        library_prefix = "library://radio/"
        if uri.startswith(library_prefix):
            return self._show_library_ids.get(uri.removeprefix(library_prefix))
        return None

    async def _snapshot_show_tracks(self, station: dict[str, Any]) -> list[Track]:
        """Build one run's track snapshot from the source playlist."""
        source_tracks, _playlist_name = await self._fetch_source_tracks(station)
        if station.get("shuffle_source_tracks", True):
            source_tracks = random.sample(source_tracks, len(source_tracks))
        source_tracks = self._apply_duration_cap(
            source_tracks, float(station.get("max_duration_minutes") or 0.0)
        )
        return [
            track["media_item"] for track in source_tracks if track.get("media_item") is not None
        ]

    def _start_show_run(self, tracks: list[Track], queue: PlayerQueue) -> _ShowRun:
        """Bind a run to the queue, resuming behind the show tracks it already holds."""
        # runs live in memory only, so a queue already holding tracks of this show is
        # resuming it (a restart mid-show): the tracks it holds are not fed again
        queued_uris = {item.uri for item in self._dj_queue_items(queue.queue_id)}
        return _ShowRun(
            tracks=[track for track in tracks if track.uri not in queued_uris],
            queue_id=queue.queue_id,
        )

    def _apply_duration_cap(
        self, tracks: list[dict[str, Any]], max_minutes: float
    ) -> list[dict[str, Any]]:
        """Trim the track list to the show's configured maximum duration (0 = whole playlist)."""
        if max_minutes <= 0:
            return tracks
        kept: list[dict[str, Any]] = []
        total_minutes = 0.0
        for track in tracks:
            kept.append(track)
            duration = track.get("duration")
            seconds = (
                float(duration)
                if isinstance(duration, (int, float)) and duration > 0
                else FALLBACK_TRACK_SECONDS
            )
            total_minutes += seconds / 60.0
            if total_minutes >= max_minutes:
                break
        return kept
