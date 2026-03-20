"""Browse implementation for the YuTorah Music Assistant provider."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from music_assistant_models.media_items import (
    BrowseFolder,
    ItemMapping,
    MediaItemMetadata,
    MediaItemType,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
)
from music_assistant_models.unique_list import UniqueList

from .helpers import (
    _make_images,
    _path_segment,
    _segment_id,
    _series_to_podcast,
    _shiur_to_episode,
)


class YuTorahBrowseMixin:
    """Mixin providing browse() and private _browse_* helpers for YuTorahProvider.

    Requires the host class to provide: domain, instance_id, _api_get,
    _fetch_series_list, _fetch_teachers_map, and _fetch_episodes_paged.
    """

    # ------------------------------------------------------------------
    # Interface required from the host class (YuTorahProvider)
    # Declared under TYPE_CHECKING so they are visible to mypy but do not
    # override the final attributes declared in the MusicProvider base class.
    # ------------------------------------------------------------------

    if TYPE_CHECKING:
        domain: str
        instance_id: str

    async def _api_get(self, endpoint: str, **params: Any) -> Any:
        """Make a GET request to the YuTorah JSON API."""
        raise NotImplementedError

    async def _fetch_series_list(self) -> list[dict[str, Any]]:
        """Fetch the full list of curated series."""
        raise NotImplementedError

    async def _fetch_teachers_map(self) -> dict[str, dict[str, Any]]:
        """Fetch all teachers as a dict keyed by teacher ID string."""
        raise NotImplementedError

    async def _fetch_episodes_paged(
        self,
        parent_series_id: str | None = None,
        **filter_params: Any,
    ) -> list[PodcastEpisode]:
        """Fetch episodes from search/get with automatic pagination."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Browse
    # ------------------------------------------------------------------

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse the YuTorah content tree.

        :param path: The path to browse (e.g. yutorah://series).
        """
        section, p1, p2, *_ = [*path.split("://", 1)[1].split("/"), "", ""]

        if section == "":
            return [
                BrowseFolder(
                    item_id="series",
                    provider=self.instance_id,
                    path=f"{self.domain}://series",
                    name="Browse by Series",
                    translation_key="podcasts",
                    is_playable=False,
                ),
                BrowseFolder(
                    item_id="teachers",
                    provider=self.instance_id,
                    path=f"{self.domain}://teachers",
                    name="Browse by Teacher",
                    translation_key="artists",
                    is_playable=False,
                ),
                BrowseFolder(
                    item_id="categories",
                    provider=self.instance_id,
                    path=f"{self.domain}://categories",
                    name="Browse by Topic",
                    is_playable=False,
                ),
                BrowseFolder(
                    item_id="recent",
                    provider=self.instance_id,
                    path=f"{self.domain}://recent",
                    name="Recent Shiurim",
                    is_playable=True,
                ),
            ]

        if section == "series" and not p1:
            return await self._browse_all_series()
        if section == "series" and p1 and p2:
            series_id = _segment_id(p1)
            teacher_id = _segment_id(p2)
            episodes, teachers_map, series_list = await asyncio.gather(
                self._browse_series_teacher_episodes(series_id, teacher_id),
                self._fetch_teachers_map(),
                self._fetch_series_list(),
            )
            t = teachers_map.get(teacher_id) or {}
            teacher_name = t.get("fullName") or f"Teacher {teacher_id}"
            image_url = t.get("imageURL") or ""
            series_name = next(
                (
                    str(s.get("name") or "")
                    for s in series_list
                    if str(s.get("ID") or s.get("seriesID") or "") == series_id
                ),
                "",
            )
            podcast_id = f"st_{series_id}_{teacher_id}"
            podcast_name = f"{series_name} — {teacher_name}" if series_name else teacher_name
            st_podcast = Podcast(
                item_id=podcast_id,
                provider=self.instance_id,
                name=podcast_name,
                metadata=MediaItemMetadata(
                    images=UniqueList(_make_images(image_url, self.instance_id)) or None,
                ),
                provider_mappings={
                    ProviderMapping(
                        item_id=podcast_id,
                        provider_domain="yutorah",
                        provider_instance=self.instance_id,
                    )
                },
            )
            return [st_podcast, *episodes]
        if section == "series" and p1:
            series_id = _segment_id(p1)
            teacher_folders, series_list = await asyncio.gather(
                self._browse_series_teachers(p1),
                self._fetch_series_list(),
            )
            series_item: Podcast | None = next(
                (
                    _series_to_podcast(s, self.instance_id)
                    for s in series_list
                    if str(s.get("ID") or s.get("seriesID") or "") == series_id
                ),
                None,
            )
            items: list[Podcast | BrowseFolder] = []
            if series_item:
                items.append(series_item)
            items.extend(teacher_folders)
            return items

        if section == "teachers" and not p1:
            return await self._browse_all_teachers()
        if section == "teachers" and p1:
            return await self._browse_teacher_episodes(_segment_id(p1))

        if section == "categories" and not p1:
            return await self._browse_category_list()
        if section == "categories" and p1:
            return await self._browse_category(_segment_id(p1))

        if section == "recent":
            return await self._browse_recent()

        return []

    async def _browse_all_series(self) -> list[BrowseFolder]:
        """Return all series as browse folders (each expands to teacher sub-folders)."""
        series_list = await self._fetch_series_list()
        folders = []
        for s in series_list:
            sid = str(s.get("ID") or s.get("seriesID") or "")
            if not sid:
                continue
            name = s.get("name") or "Unknown Series"
            folders.append(
                BrowseFolder(
                    item_id=sid,
                    provider=self.instance_id,
                    path=f"{self.domain}://series/{_path_segment(name, sid)}",
                    name=name,
                    is_playable=False,
                )
            )
        folders.sort(key=lambda f: f.name.lower())
        return folders

    async def _browse_series_teachers(self, series_segment: str) -> list[BrowseFolder]:
        """Return teacher sub-folders for a series, derived from search facets.

        :param series_segment: Full path segment for this series (e.g. 'Daf Yomi|4015').
        """
        series_id = _segment_id(series_segment)
        data = await self._api_get("search/get", searchTerm="", seriesID=series_id, getFacets=True)
        if not data or not isinstance(data, dict):
            return []

        teachers = (data.get("facet_counts") or {}).get("facet_fields", {}).get("teachers", [])
        folders = []
        for t in teachers:
            tid = str(t.get("TeacherId") or "")
            name = t.get("TeacherName") or ""
            count = t.get("Match", 0)
            if not tid or not name or count == 0:
                continue
            folders.append(
                BrowseFolder(
                    item_id=f"st_{series_id}_{tid}",
                    provider=self.instance_id,
                    path=f"{self.domain}://series/{series_segment}/{_path_segment(name, tid)}",
                    name=f"{name} ({count})",
                    is_playable=False,
                )
            )
        return folders

    async def _browse_series_teacher_episodes(
        self, series_id: str, teacher_id: str
    ) -> list[PodcastEpisode]:
        """Return episodes for one teacher within a series."""
        return await self._fetch_episodes_paged(
            seriesID=series_id, teacherID=teacher_id, parent_series_id=series_id
        )

    async def _browse_all_teachers(self) -> list[Podcast]:
        """Return all teachers as subscribable Podcast items (virtual podcasts with t_ prefix)."""
        teachers_map = await self._fetch_teachers_map()
        if not teachers_map:
            return []

        podcasts = []
        for tid, t in teachers_map.items():
            name = t.get("fullName") or ""
            count = t.get("shiurCount") or 0
            if not tid or not name or t.get("isHidden") or count == 0:
                continue
            image_url = t.get("imageURL") or ""
            podcasts.append(
                Podcast(
                    item_id=f"t_{tid}",
                    provider=self.instance_id,
                    name=f"{name} ({count})",
                    metadata=MediaItemMetadata(
                        images=UniqueList(_make_images(image_url, self.instance_id)) or None,
                    ),
                    provider_mappings={
                        ProviderMapping(
                            item_id=f"t_{tid}",
                            provider_domain="yutorah",
                            provider_instance=self.instance_id,
                        )
                    },
                )
            )
        return podcasts

    async def _browse_teacher_episodes(self, teacher_id: str) -> list[PodcastEpisode]:
        """Return all episodes by a teacher using paginated search."""
        return await self._fetch_episodes_paged(teacherID=teacher_id)

    async def _browse_category_list(self) -> list[BrowseFolder]:
        """Return subcategories as browse folders.

        browse/categories returns top-level categories each with a subCategories list.
        Subcategory IDs are what landingpage/landing accepts as search targets.
        """
        data = await self._api_get("browse/categories", favoritesOnly=False)
        if not data or not isinstance(data, list):
            return []

        folders: list[BrowseFolder] = []
        for cat in data:
            cat_name = cat.get("name") or ""
            for sub in cat.get("subCategories") or []:
                sub_id = str(sub.get("ID") or sub.get("id") or "")
                sub_name = sub.get("name") or ""
                if not sub_id or not sub_name:
                    continue
                label = f"{cat_name} — {sub_name}" if cat_name else sub_name
                folders.append(
                    BrowseFolder(
                        item_id=f"sub_{sub_id}",
                        provider=self.instance_id,
                        path=f"{self.domain}://categories/{_path_segment(label, sub_id)}",
                        name=label,
                        is_playable=False,
                    )
                )
        return folders

    async def _browse_category(self, category_id: str) -> list[Podcast | BrowseFolder]:
        """Return series with shiurim in a subcategory using landingpage/landing.

        Uses type=subcategory which requires no authentication, then deduplicates
        by series so the user sees which podcasts cover that topic.
        """
        data = await self._api_get("landingpage/landing", type="subcategory", value=category_id)
        if not data or not isinstance(data, dict):
            return []

        series_list = await self._fetch_series_list()
        series_by_id = {str(s.get("ID") or s.get("seriesID") or ""): s for s in series_list}

        seen_series: set[str] = set()
        results: list[Podcast | BrowseFolder] = []
        for key in ("recentlyAddedShiurim", "topShiurim", "featuredShiurim"):
            for raw in data.get(key) or []:
                sid = str(raw.get("shiurSeries") or "")
                if sid and sid not in seen_series:
                    seen_series.add(sid)
                    if sid in series_by_id:
                        results.append(_series_to_podcast(series_by_id[sid], self.instance_id))
                    else:
                        series_name = raw.get("shiurSeriesName") or sid
                        results.append(
                            Podcast(
                                item_id=sid,
                                provider=self.instance_id,
                                name=series_name,
                                provider_mappings={
                                    ProviderMapping(
                                        item_id=sid,
                                        provider_domain="yutorah",
                                        provider_instance=self.instance_id,
                                    )
                                },
                            )
                        )
        return results

    async def _browse_recent(self) -> list[PodcastEpisode]:
        """Return recently uploaded shiurim from the homepage endpoint (no auth needed)."""
        data = await self._api_get("homepage/details")
        episodes: list[PodcastEpisode] = []
        for i, raw in enumerate((data or {}).get("recentlyUploaded") or []):
            episode = _shiur_to_episode(raw, i, self.instance_id)
            if episode:
                episodes.append(episode)
        return episodes
