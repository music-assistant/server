"""MusicBrainz-based recommendations for Music Assistant."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from music_assistant_models.enums import ExternalID
from music_assistant_models.media_items import (
    BrowseFolder,
    ItemMapping,
    MediaItemType,
    RecommendationFolder,
    UniqueList,
)

from music_assistant.helpers.datetime import utc as datetime_utc

if TYPE_CHECKING:
    from music_assistant_models.media_items import Artist

    from .provider import MusicbrainzProvider


class MusicBrainzRecommendationManager:
    """Manages MusicBrainz-based recommendations (birthdays and memorials)."""

    def __init__(self, provider: MusicbrainzProvider) -> None:
        """Initialize recommendation manager."""
        self.provider = provider
        self.logger = provider.logger
        self.mass = provider.mass

    async def get_recommendations(self) -> list[RecommendationFolder]:
        """
        Return recommendation folders for artists with dates in a configurable window.

        Scans configurable days before today through configurable days after today.
        Includes:
        - Birthday artists (born on these days)
        - In-memoriam artists (passed away on these days)
        """
        today = datetime_utc().date()
        folders: list[RecommendationFolder] = []

        # Get config values with type safety and range validation
        days_config = self.provider.config.get_value("recommendation_days", 3)
        try:
            days_before_after = int(str(days_config))
        except TypeError, ValueError:
            days_before_after = 3
        days_before_after = max(1, min(15, days_before_after))

        self.logger.info(
            "MusicBrainz recommendations: scanning %d days before/after today",
            days_before_after,
        )

        # Build MBID → library-artist mapping once, shared across all artist checks
        mbid_to_artist: dict[str, Artist] = {}
        async for artist in self.mass.music.artists.iter_library_items(order_by="name"):
            if mbid := artist.get_external_id(ExternalID.MB_ARTIST):
                mbid_to_artist[mbid] = artist

        self.logger.debug("Library contains %d artist(s) with MB IDs", len(mbid_to_artist))

        # Scan configurable day window
        for day_offset in range(-days_before_after, days_before_after + 1):
            target_date = today + timedelta(days=day_offset)
            target_mmdd = f"{target_date.month:02d}-{target_date.day:02d}"

            # Determine translation key suffix based on day offset
            day_suffix = self._get_day_suffix(day_offset)
            day_params = self._get_day_params(day_offset)

            # --- birthdays ---
            birthday_mbids = await self._find_artist_mbids_by_date(
                mbid_to_artist, target_mmdd, date_field="begin"
            )
            if birthday_mbids:
                matching = [mbid_to_artist[m] for m in birthday_mbids if m in mbid_to_artist]
                self.logger.debug(
                    "Birthday scan for %s (%s) found %d artist(s)",
                    target_mmdd,
                    day_suffix,
                    len(matching),
                )
                folders.extend(
                    self._build_artist_folders(
                        matching,
                        folder_id_prefix=f"birthdays_{day_offset}",
                        translation_key=f"artist_birthdays_{day_suffix}",
                        icon="mdi-cake-variant",
                        translation_params=day_params,
                    )
                )

            # --- in memoriam ---
            memoriam_mbids = await self._find_artist_mbids_by_date(
                mbid_to_artist, target_mmdd, date_field="end"
            )
            if memoriam_mbids:
                matching_mem = [mbid_to_artist[m] for m in memoriam_mbids if m in mbid_to_artist]
                self.logger.debug(
                    "In-memoriam scan for %s (%s) found %d artist(s)",
                    target_mmdd,
                    day_suffix,
                    len(matching_mem),
                )
                folders.extend(
                    self._build_artist_folders(
                        matching_mem,
                        folder_id_prefix=f"memoriam_{day_offset}",
                        translation_key=f"artist_memoriam_{day_suffix}",
                        icon="mdi-candle",
                        translation_params=day_params,
                    )
                )

        return folders

    # ------------------------------------------------------------------
    # artist date helpers
    # ------------------------------------------------------------------

    def _get_day_suffix(self, day_offset: int) -> str:
        """
        Return translation key suffix for a day offset.

        :param day_offset: Days from today (negative = past, 0 = today, positive = future).
        """
        if day_offset == -1:
            return "yesterday"
        if day_offset == 0:
            return "today"
        if day_offset == 1:
            return "tomorrow"
        if day_offset < 0:
            return "n_days_ago"
        return "in_n_days"

    def _get_day_params(self, day_offset: int) -> list[str] | None:
        """
        Return translation_params for a day offset, if needed.

        :param day_offset: Days from today (negative = past, 0 = today, positive = future).
        """
        if day_offset in (-1, 0, 1):
            return None
        return [str(abs(day_offset))]

    async def _find_artist_mbids_by_date(
        self,
        mbid_to_artist: dict[str, Artist],
        today_mmdd: str,
        *,
        date_field: str,
    ) -> list[str]:
        """
        Return MBIDs of library artists whose MB life-span field matches today's month/day.

        :param mbid_to_artist: Mapping of MB artist ID to library Artist.
        :param today_mmdd: Today's month-day string in ``MM-DD`` format.
        :param date_field: Either ``"begin"`` (birthday) or ``"end"`` (death day).
        """
        matched: list[str] = []
        for mbid in mbid_to_artist:
            try:
                mb_artist = await self.provider.get_artist_details(mbid)
            except Exception as err:
                self.logger.debug("Skipping artist %s: %s", mbid, err)
                continue
            life_span = mb_artist.life_span
            if not life_span:
                continue
            raw_date = life_span.begin if date_field == "begin" else life_span.end
            # Only full "YYYY-MM-DD" dates are usable; partial dates like "1990" are skipped
            if raw_date and len(raw_date) >= 10 and raw_date[5:10] == today_mmdd:
                if date_field == "end" and not life_span.ended:
                    continue
                matched.append(mbid)
        return matched

    def _build_artist_folders(
        self,
        artists: list[Artist],
        *,
        folder_id_prefix: str,
        translation_key: str,
        icon: str,
        translation_params: list[str] | None = None,
    ) -> list[RecommendationFolder]:
        """Return a single RecommendationFolder containing all given artists."""
        if not artists:
            return []
        # Use translation_key as fallback name if translation is unavailable
        fallback_name = translation_key.replace("_", " ").title()
        return [
            RecommendationFolder(
                item_id=folder_id_prefix,
                name=fallback_name,
                provider=self.provider.instance_id,
                translation_key=translation_key,
                translation_params=translation_params,
                icon=icon,
                items=UniqueList[MediaItemType | ItemMapping | BrowseFolder](artists),
                is_playable=False,
            )
        ]
