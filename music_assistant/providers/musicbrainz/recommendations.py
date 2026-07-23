"""MusicBrainz-based recommendations for Music Assistant."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING

from music_assistant_models.enums import ArtistEntityType, ExternalID, RecommendationFolderType
from music_assistant_models.media_items import (
    Artist,
    MediaItemMetadata,
    RecommendationFolder,
    UniqueList,
)
from music_assistant_models.media_items.metadata import LifeSpan

from music_assistant.helpers.datetime import utc as datetime_utc

if TYPE_CHECKING:
    from music_assistant_models.media_items import BrowseFolder, ItemMapping, MediaItemType

    from .provider import MusicbrainzProvider

# Cache key for the precomputed matches (namespaced per provider instance).
RECOMMENDATIONS_CACHE_KEY = "artist_timeline_recommendations_v2"


class MusicBrainzRecommendationManager:
    """Manages MusicBrainz-based recommendations (birthdays and memorials)."""

    def __init__(self, provider: MusicbrainzProvider) -> None:
        """Initialize recommendation manager."""
        self.provider = provider
        self.logger = provider.logger
        self.mass = provider.mass
        self._refresh_task_id = f"{provider.instance_id}_recommendations_refresh"

    async def get_recommendations(self) -> list[RecommendationFolder]:
        """
        Return recommendation folder metadata without items.

        Fast cache lookup that returns only the folder structure. Items are fetched
        separately via get_recommendation_items.
        """
        artist_dicts = await self.mass.cache.get(
            RECOMMENDATIONS_CACHE_KEY, provider=self.provider.instance_id, default=None
        )
        if artist_dicts is not None:
            return [self._build_folder_metadata()] if artist_dicts else []
        # Nothing fresh: refresh in the background and serve stale data if we have any.
        self.schedule_refresh()
        stale = await self.mass.cache.get(
            RECOMMENDATIONS_CACHE_KEY,
            provider=self.provider.instance_id,
            allow_expired_cache=True,
            default=None,
        )
        if stale is not None:
            return [self._build_folder_metadata()] if stale else []
        return []

    async def get_recommendation_items(
        self, item_id: str
    ) -> UniqueList[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Return artists for the musicbrainz_timeline recommendation folder.

        :param item_id: The folder ID (expected to be "musicbrainz_timeline").
        """
        if item_id != "musicbrainz_timeline":
            return UniqueList()
        # Serve from cache (fresh or stale)
        artist_dicts = await self.mass.cache.get(
            RECOMMENDATIONS_CACHE_KEY,
            provider=self.provider.instance_id,
            allow_expired_cache=True,
            default=None,
        )
        if artist_dicts is None:
            return UniqueList()
        return UniqueList([Artist.from_dict(d) for d in artist_dicts])

    def schedule_refresh(self) -> None:
        """Scan the library and refresh the cached matches in the background (deduplicated)."""
        self.mass.create_task(self._refresh(), task_id=self._refresh_task_id)

    def cancel(self) -> None:
        """Cancel any pending background refresh (called on provider unload)."""
        self.mass.cancel_task(self._refresh_task_id)

    # ------------------------------------------------------------------
    # background refresh
    # ------------------------------------------------------------------

    async def _refresh(self) -> None:
        """Scan the library once and cache matched artists as serialized dicts."""
        try:
            artists = await self._scan_matches()
        except Exception as err:
            self.logger.warning("Failed to compute MusicBrainz recommendations: %s", err)
            return
        # Expire at the next UTC midnight so the fresh path recomputes daily; the entry
        # survives cleanup (allow_expired_cache) so it can be served as stale fallback.
        await self.mass.cache.set(
            RECOMMENDATIONS_CACHE_KEY,
            [a.to_dict() for a in artists],
            expiration=self._seconds_until_next_utc_midnight() + 60,
            provider=self.provider.instance_id,
            allow_expired_cache=True,
        )

    async def _scan_matches(self) -> list[Artist]:
        """
        Scan the library for artists whose birth/death/founding/disbanding date falls in the window.

        Returns matched artists with their metadata populated (life_span and artist_entity_type)
        so the frontend can compute event type and date offset independently.
        """
        days_before_after = self._days_window()
        self.logger.info(
            "MusicBrainz recommendations: scanning %d days before/after today",
            days_before_after,
        )
        window = self._window_dates()

        matched: list[Artist] = []
        scanned = 0
        async for artist in self.mass.music.artists.iter_library_items(order_by="name"):
            mbid = artist.get_external_id(ExternalID.MB_ARTIST)
            if not mbid:
                continue
            scanned += 1
            entity_type: ArtistEntityType | None = (
                artist.metadata.artist_entity_type if artist.metadata else None
            )
            life_span = artist.metadata.life_span if artist.metadata else None
            if life_span is None:
                # Metadata not yet populated; fall back to a direct MusicBrainz API call.
                try:
                    mb_artist = await self.provider.get_artist_details(mbid)
                except Exception as err:
                    self.logger.debug("Skipping artist %s: %s", mbid, err)
                    continue
                if entity_type is None and mb_artist.type:
                    entity_type = ArtistEntityType(mb_artist.type.lower())
                if mb_artist.life_span:
                    mb_ls = mb_artist.life_span
                    life_span = LifeSpan(begin=mb_ls.begin, end=mb_ls.end, ended=mb_ls.ended)
            if not life_span:
                continue
            if entity_type in (ArtistEntityType.CHARACTER, ArtistEntityType.OTHER):
                continue
            begin = life_span.begin
            end = life_span.end
            # Only full "YYYY-MM-DD" dates are usable; partial dates like "1990" are skipped
            birth_in_window = begin and len(begin) >= 10 and begin[5:10] in window
            death_in_window = life_span.ended and end and len(end) >= 10 and end[5:10] in window
            if birth_in_window or death_in_window:
                # Ensure life_span and entity_type are stored in the artist metadata
                # so the frontend can compute event type and date without extra API calls.
                metadata = artist.metadata or MediaItemMetadata()
                metadata.life_span = life_span
                if entity_type not in (None, ArtistEntityType.UNKNOWN):
                    metadata.artist_entity_type = entity_type
                artist.metadata = metadata
                matched.append(artist)

        self.logger.debug(
            "Scanned %d library artist(s) with MB IDs, %d matched", scanned, len(matched)
        )
        return matched

    # ------------------------------------------------------------------
    # folder building
    # ------------------------------------------------------------------

    def _build_folder_metadata(self) -> RecommendationFolder:
        """Build recommendation folder metadata without items."""
        return RecommendationFolder(
            item_id="musicbrainz_timeline",
            name="Artist Events",
            provider=self.provider.instance_id,
            translation_key="artist_timeline",
            items=UniqueList(),
            is_playable=False,
            type=RecommendationFolderType.TIMELINE,
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _window_dates(self) -> set[str]:
        """Return the set of MM-DD strings within the current scan window."""
        today = datetime_utc().date()
        days_before_after = self._days_window()
        return {
            f"{(today + timedelta(days=offset)).month:02d}-{(today + timedelta(days=offset)).day:02d}"
            for offset in range(-days_before_after, days_before_after + 1)
        }

    def _days_window(self) -> int:
        """Return the validated number of days to scan before/after today."""
        days_config = self.provider.config.get_value("recommendation_days", 3)
        try:
            days_before_after = int(str(days_config))
        except TypeError, ValueError:
            days_before_after = 3
        return max(1, min(15, days_before_after))

    def _seconds_until_next_utc_midnight(self) -> int:
        """Return the number of seconds until the next UTC midnight (at least 60)."""
        now = datetime_utc()
        next_midnight = datetime.combine(
            now.date() + timedelta(days=1), time.min, tzinfo=now.tzinfo
        )
        return max(60, int((next_midnight - now).total_seconds()))
