"""MusicBrainz-based recommendations for Music Assistant."""

from __future__ import annotations

from datetime import datetime, time, timedelta
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

# Cache key for the precomputed recommendation folders (namespaced per provider instance).
RECOMMENDATIONS_CACHE_KEY = "birthday_memoriam_recommendations"


class MusicBrainzRecommendationManager:
    """Manages MusicBrainz-based recommendations (birthdays and memorials)."""

    def __init__(self, provider: MusicbrainzProvider) -> None:
        """Initialize recommendation manager."""
        self.provider = provider
        self.logger = provider.logger
        self.mass = provider.mass
        self._refresh_task_id = f"{provider.instance_id}_recommendations_refresh"
        self._daily_task_id = f"{provider.instance_id}_recommendations_daily"

    async def get_recommendations(self) -> list[RecommendationFolder]:
        """
        Return the precomputed birthday/in-memoriam recommendation folders.

        The result is served from cache so the discover page is never blocked by the
        rate-limited MusicBrainz library scan; the scan runs in the background and
        populates the cache (see :meth:`schedule_refresh`).
        """
        cached: list[RecommendationFolder] | None = await self.mass.cache.get(
            RECOMMENDATIONS_CACHE_KEY,
            provider=self.provider.instance_id,
            base_class=RecommendationFolder,
            default=None,
        )
        if cached is not None:
            return cached
        # Nothing computed yet (first run or past the daily expiry): kick off the
        # background scan and return empty for now; results appear on the next load.
        self.schedule_refresh()
        return []

    def schedule_refresh(self) -> None:
        """Compute and cache the recommendation folders in the background (deduplicated)."""
        self.mass.create_task(self._refresh(), task_id=self._refresh_task_id)

    def cancel(self) -> None:
        """Cancel any pending background refresh work (called on provider unload)."""
        self.mass.cancel_task(self._refresh_task_id)
        self.mass.cancel_timer(self._daily_task_id)

    # ------------------------------------------------------------------
    # background refresh
    # ------------------------------------------------------------------

    async def _refresh(self) -> None:
        """Scan the library once, cache the resulting folders, and re-arm for tomorrow."""
        try:
            folders = await self._compute_folders()
        except Exception as err:
            self.logger.warning("Failed to compute MusicBrainz recommendations: %s", err)
            return
        # Expire at the next UTC midnight so the relative day labels (today/tomorrow/...)
        # stay correct; the daily timer below repopulates the cache right after.
        await self.mass.cache.set(
            RECOMMENDATIONS_CACHE_KEY,
            [folder.to_dict() for folder in folders],
            expiration=self._seconds_until_next_utc_midnight() + 60,
            provider=self.provider.instance_id,
        )
        self.mass.call_later(
            self._seconds_until_next_utc_midnight() + 60,
            self.schedule_refresh,
            task_id=self._daily_task_id,
        )

    async def _compute_folders(self) -> list[RecommendationFolder]:
        """
        Build recommendation folders for artists with dates in a configurable window.

        Scans configurable days before today through configurable days after today,
        fetching each library artist's MusicBrainz details once and bucketing it by
        birthday (life-span ``begin``) and/or death day (life-span ``end``).
        """
        today = datetime_utc().date()
        days_before_after = self._days_window()
        self.logger.info(
            "MusicBrainz recommendations: scanning %d days before/after today",
            days_before_after,
        )

        # Map each MM-DD in the window to its day offset; the window never exceeds 31
        # days so there are no month/day collisions.
        mmdd_to_offset: dict[str, int] = {}
        for offset in range(-days_before_after, days_before_after + 1):
            target_date = today + timedelta(days=offset)
            mmdd_to_offset[f"{target_date.month:02d}-{target_date.day:02d}"] = offset

        birthdays: dict[int, list[Artist]] = {}
        memoriam: dict[int, list[Artist]] = {}
        scanned = 0
        async for artist in self.mass.music.artists.iter_library_items(order_by="name"):
            mbid = artist.get_external_id(ExternalID.MB_ARTIST)
            if not mbid:
                continue
            scanned += 1
            try:
                mb_artist = await self.provider.get_artist_details(mbid)
            except Exception as err:
                self.logger.debug("Skipping artist %s: %s", mbid, err)
                continue
            life_span = mb_artist.life_span
            if not life_span:
                continue
            begin = life_span.begin
            # Only full "YYYY-MM-DD" dates are usable; partial dates like "1990" are skipped
            if begin and len(begin) >= 10:
                match_offset = mmdd_to_offset.get(begin[5:10])
                if match_offset is not None:
                    birthdays.setdefault(match_offset, []).append(artist)
            end = life_span.end
            if life_span.ended and end and len(end) >= 10:
                match_offset = mmdd_to_offset.get(end[5:10])
                if match_offset is not None:
                    memoriam.setdefault(match_offset, []).append(artist)

        self.logger.debug("Scanned %d library artist(s) with MB IDs", scanned)

        folders: list[RecommendationFolder] = []
        for offset in range(-days_before_after, days_before_after + 1):
            day_suffix = self._get_day_suffix(offset)
            day_params = self._get_day_params(offset)
            if matching := birthdays.get(offset):
                folders.extend(
                    self._build_artist_folders(
                        matching,
                        folder_id_prefix=f"birthdays_{offset}",
                        translation_key=f"artist_birthdays_{day_suffix}",
                        icon="mdi-cake-variant",
                        translation_params=day_params,
                    )
                )
            if matching_mem := memoriam.get(offset):
                folders.extend(
                    self._build_artist_folders(
                        matching_mem,
                        folder_id_prefix=f"memoriam_{offset}",
                        translation_key=f"artist_memoriam_{day_suffix}",
                        icon="mdi-candle",
                        translation_params=day_params,
                    )
                )
        return folders

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

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
