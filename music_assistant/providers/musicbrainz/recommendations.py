"""MusicBrainz-based recommendations for Music Assistant."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import ExternalID
from music_assistant_models.media_items import (
    Artist,
    BrowseFolder,
    ItemMapping,
    MediaItemType,
    RecommendationFolder,
    UniqueList,
)

from music_assistant.helpers.datetime import utc as datetime_utc

if TYPE_CHECKING:
    from .provider import MusicbrainzProvider

# Cache key for the precomputed matches (namespaced per provider instance).
RECOMMENDATIONS_CACHE_KEY = "birthday_memoriam_recommendations"


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
        Return the birthday/in-memoriam recommendation rows, without items.

        Row ids and labels are derived against the current day, so the set of rows
        changes as dates enter or leave the configured window.
        """
        return [
            RecommendationFolder(
                item_id=folder.item_id,
                name=folder.name,
                provider=folder.provider,
                translation_key=folder.translation_key,
                translation_params=folder.translation_params,
                icon=folder.icon,
                is_playable=folder.is_playable,
            )
            for folder in await self._current_folders()
        ]

    async def get_recommendation_items(
        self, item_id: str
    ) -> UniqueList[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Return the items for a single birthday/in-memoriam row.

        :param item_id: The item_id of the row (empty result if unknown).
        """
        for folder in await self._current_folders():
            if folder.item_id == item_id:
                return folder.items
        return UniqueList()

    def schedule_refresh(self) -> None:
        """Scan the library and refresh the cached matches in the background (deduplicated)."""
        self.mass.create_task(self._refresh(), task_id=self._refresh_task_id)

    def cancel(self) -> None:
        """Cancel any pending background refresh (called on provider unload)."""
        self.mass.cancel_task(self._refresh_task_id)

    # ------------------------------------------------------------------
    # cached read path
    # ------------------------------------------------------------------

    async def _current_folders(self) -> list[RecommendationFolder]:
        """
        Return the current birthday/in-memoriam folders, with items.

        Served from cache so the discover page is never blocked by the rate-limited
        MusicBrainz library scan. If the cache has expired, the last-known-good result
        is served immediately (stale-while-revalidate) while a fresh scan runs in the
        background; day labels are derived against the current day on every read, so
        stale data stays correctly labelled and entries that fell out of the window are
        dropped.
        """
        matches = await self.mass.cache.get(
            RECOMMENDATIONS_CACHE_KEY, provider=self.provider.instance_id, default=None
        )
        if matches is not None:
            return self._folders_from_matches(matches)
        # Nothing fresh: refresh in the background and serve stale data if we have any.
        self.schedule_refresh()
        stale = await self.mass.cache.get(
            RECOMMENDATIONS_CACHE_KEY,
            provider=self.provider.instance_id,
            allow_expired_cache=True,
            default=None,
        )
        if stale is not None:
            return self._folders_from_matches(stale)
        return []

    # ------------------------------------------------------------------
    # background refresh
    # ------------------------------------------------------------------

    async def _refresh(self) -> None:
        """Scan the library once and cache the matched artists keyed by month/day."""
        try:
            matches = await self._scan_matches()
        except Exception as err:
            self.logger.warning("Failed to compute MusicBrainz recommendations: %s", err)
            return
        # Expire at the next UTC midnight so the fresh path recomputes daily; the entry
        # survives cleanup (allow_expired_cache) so it can be served as stale fallback.
        await self.mass.cache.set(
            RECOMMENDATIONS_CACHE_KEY,
            [
                {"kind": kind, "mmdd": mmdd, "artist": artist.to_dict()}
                for kind, mmdd, artist in matches
            ],
            expiration=self._seconds_until_next_utc_midnight() + 60,
            provider=self.provider.instance_id,
            allow_expired_cache=True,
        )

    async def _scan_matches(self) -> list[tuple[str, str, Artist]]:
        """
        Scan the library for artists whose birth/death day falls in the configured window.

        Each library artist's MusicBrainz details are fetched once; matches are returned
        as ``(kind, MM-DD, artist)`` tuples (``kind`` is ``"birthday"`` or ``"memoriam"``)
        so the read side can re-derive day offsets for the current day.
        """
        days_before_after = self._days_window()
        self.logger.info(
            "MusicBrainz recommendations: scanning %d days before/after today",
            days_before_after,
        )
        window = set(self._offset_map())

        matches: list[tuple[str, str, Artist]] = []
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
            if begin and len(begin) >= 10 and begin[5:10] in window:
                matches.append(("birthday", begin[5:10], artist))
            end = life_span.end
            if life_span.ended and end and len(end) >= 10 and end[5:10] in window:
                matches.append(("memoriam", end[5:10], artist))

        self.logger.debug("Scanned %d library artist(s) with MB IDs", scanned)
        return matches

    # ------------------------------------------------------------------
    # folder building
    # ------------------------------------------------------------------

    def _folders_from_matches(self, matches: list[dict[str, Any]]) -> list[RecommendationFolder]:
        """Build recommendation folders from cached matches, labelled for the current day."""
        offset_map = self._offset_map()
        birthdays: dict[int, list[Artist]] = {}
        memoriam: dict[int, list[Artist]] = {}
        for entry in matches:
            match_offset = offset_map.get(entry["mmdd"])
            if match_offset is None:
                # date is no longer in the window (cache is from another day)
                continue
            artist = Artist.from_dict(entry["artist"])
            bucket = birthdays if entry["kind"] == "birthday" else memoriam
            bucket.setdefault(match_offset, []).append(artist)

        days_before_after = self._days_window()
        folders: list[RecommendationFolder] = []
        for offset in range(-days_before_after, days_before_after + 1):
            day_suffix = self._get_day_suffix(offset)
            day_params = self._get_day_params(offset)
            if artists := birthdays.get(offset):
                folders.extend(
                    self._build_artist_folders(
                        artists,
                        folder_id_prefix=f"birthdays_{offset}",
                        translation_key=f"artist_birthdays_{day_suffix}",
                        icon="mdi-cake-variant",
                        translation_params=day_params,
                    )
                )
            if artists := memoriam.get(offset):
                folders.extend(
                    self._build_artist_folders(
                        artists,
                        folder_id_prefix=f"memoriam_{offset}",
                        translation_key=f"artist_memoriam_{day_suffix}",
                        icon="mdi-candle",
                        translation_params=day_params,
                    )
                )
        return folders

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

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _offset_map(self) -> dict[str, int]:
        """Return a mapping of each MM-DD in the current window to its day offset."""
        today = datetime_utc().date()
        days_before_after = self._days_window()
        offset_map: dict[str, int] = {}
        for offset in range(-days_before_after, days_before_after + 1):
            target_date = today + timedelta(days=offset)
            offset_map[f"{target_date.month:02d}-{target_date.day:02d}"] = offset
        return offset_map

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
