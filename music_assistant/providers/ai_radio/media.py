"""Media item surface for AI Radio: shows exposed as dynamic Radio items."""
# mypy: disable-error-code=attr-defined

from __future__ import annotations

import logging
import random
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

from .constants import FALLBACK_TRACK_SECONDS

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


class AIRadioMediaMixin:
    """Mixin exposing AI Radio shows as library-backed tracklisted (finite) Radio media items."""

    if TYPE_CHECKING:
        mass: MusicAssistant
        logger: logging.Logger
        _stations: dict[str, dict[str, Any]]
        _show_library_ids: dict[str, str]

        async def _fetch_source_tracks(
            self, station: dict[str, Any]
        ) -> tuple[list[dict[str, Any]], str]: ...

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """
        Return the Radio media item for one of this provider's shows.

        :param prov_radio_id: The station id of the show.
        """
        station = self._stations.get(prov_radio_id)
        if station is None:
            raise MediaNotFoundError(f"AI Radio show {prov_radio_id} not found")
        return self._station_to_radio(station)

    async def get_radio_tracks(self, prov_radio_id: str, page: int = 0) -> list[Track]:
        """
        Return one page of a show's tracklist.

        The whole show fits on page 0, which returns a fresh shuffled snapshot of
        the source playlist on every call; any later page is empty.

        :param prov_radio_id: The station id of the show.
        :param page: The zero-based page to return.
        """
        station = self._stations.get(prov_radio_id)
        if station is None:
            raise MediaNotFoundError(f"AI Radio show {prov_radio_id} not found")
        if page > 0:
            return []
        return await self._snapshot_show_tracks(station)

    def _station_to_radio(self, station: dict[str, Any]) -> Radio:
        """Build the Radio media item for a station."""
        station_id = str(station["id"])
        radio = Radio(
            item_id=station_id,
            provider=self.instance_id,
            name=str(station["name"]),
            is_endless_stream=False,
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
            elif (
                prov_item.name != library_item.name
                or prov_item.is_endless_stream != library_item.is_endless_stream
            ):
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

    async def _remove_show_library_items(self) -> None:
        """Remove every library radio row this provider instance mirrored (plugin removed)."""
        radio_ctrl = self.mass.music.radio
        # deletions are collected first: deleting rows mid-pagination can skip rows
        db_ids = [
            library_radio.item_id
            async for library_radio in radio_ctrl.iter_library_items(provider=self.instance_id)
        ]
        for db_id in db_ids:
            await radio_ctrl.remove_item_from_library(db_id)
        self._show_library_ids = {}

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
        """Build a fresh, shuffled and duration-capped tracklist from the source playlist."""
        source_tracks, _playlist_name = await self._fetch_source_tracks(station)
        if station.get("shuffle_source_tracks", True):
            source_tracks = random.sample(source_tracks, len(source_tracks))
        source_tracks = self._apply_duration_cap(
            source_tracks, float(station.get("max_duration_minutes") or 0.0)
        )
        return [
            track["media_item"] for track in source_tracks if track.get("media_item") is not None
        ]

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
