"""YuTorahProvider class for Music Assistant."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

import aiohttp
from music_assistant_models.enums import (
    ContentType,
    MediaType,
    StreamType,
)
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    ProviderUnavailableError,
    SetupFailedError,
)
from music_assistant_models.media_items import (
    Artist,
    AudioFormat,
    MediaItemMetadata,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    SearchResults,
    Track,
)
from music_assistant_models.streamdetails import StreamDetails
from music_assistant_models.unique_list import UniqueList

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
from music_assistant.controllers.cache import use_cache
from music_assistant.models.music_provider import MusicProvider

from .browse import YuTorahBrowseMixin
from .constants import API_BASE, API_HEADERS, MAX_EPISODES, PAGE_SIZE, YUTORAH_BASE
from .helpers import (
    _build_st_podcast,
    _extract_docs,
    _make_images,
    _series_to_podcast,
    _shiur_to_episode,
    _shiur_to_track,
    _slugify,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

_T = TypeVar("_T")


class YuTorahProvider(YuTorahBrowseMixin, MusicProvider):  # type: ignore[misc]
    """Music Assistant provider for YuTorah Online.

    Uses the official mobile app JSON API — no scraping, no Cloudflare issues.
    Browse the full series directory, search by any term, and stream any shiur.
    """

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    async def handle_async_init(self) -> None:
        """Authenticate with YuTorah; credentials are required."""
        username = self.config.get_value(CONF_USERNAME)
        password = self.config.get_value(CONF_PASSWORD)
        if not username or not password:
            raise SetupFailedError(
                "YuTorah requires a username and password. Sign up free at yutorah.org."
            )
        try:
            await self._login(str(username), str(password))
        except LoginFailed as exc:
            raise SetupFailedError(str(exc)) from exc

    async def _login(self, email: str, password: str) -> None:
        """Authenticate with YuTorah and store the user token.

        :raises LoginFailed: if credentials are rejected by the API.
        """
        try:
            async with self.mass.http_session.post(
                f"{API_BASE}login/default",
                json={"email": email, "password": password},
                headers=API_HEADERS,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise LoginFailed(f"YuTorah login error: {exc}") from exc

        if not (data and data.get("loginSuccess") and data.get("userToken")):
            raise LoginFailed("YuTorah login failed — check your email and password.")

        self._user_token: str = data["userToken"]
        self.logger.info("YuTorah login successful — full episode access enabled.")

    # -----------------------------------------------------------------------
    # Podcast — series
    # -----------------------------------------------------------------------

    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Return a single Podcast (series or teacher) by its provider ID.

        IDs prefixed with ``st_`` identify a series+teacher virtual-podcast;
        IDs prefixed with ``t_`` identify a teacher virtual-podcast;
        plain numeric IDs identify a series.
        """
        if prov_podcast_id.startswith("st_"):
            _, series_id, teacher_id = prov_podcast_id.split("_", 2)
            teachers_map, series_list = await asyncio.gather(
                self._fetch_teachers_map(),
                self._fetch_series_list(),
            )
            return _build_st_podcast(
                series_id, teacher_id, teachers_map, series_list, self.instance_id
            )

        if prov_podcast_id.startswith("t_"):
            teacher_id = prov_podcast_id[2:]
            teachers_map = await self._fetch_teachers_map()
            t = teachers_map.get(teacher_id) or {}
            name = t.get("fullName") or f"Teacher {teacher_id}"
            image_url = t.get("imageURL") or ""
            return Podcast(
                item_id=prov_podcast_id,
                provider=self.instance_id,
                name=name,
                metadata=MediaItemMetadata(
                    images=UniqueList(_make_images(image_url, self.instance_id)) or None,
                ),
                provider_mappings={
                    ProviderMapping(
                        item_id=prov_podcast_id,
                        provider_domain="yutorah",
                        provider_instance=self.instance_id,
                    )
                },
            )

        series_list = await self._fetch_series_list()
        for series in series_list:
            sid = str(series.get("ID") or series.get("seriesID") or "")
            if sid == str(prov_podcast_id):
                return _series_to_podcast(series, self.instance_id)

        raise MediaNotFoundError(f"YuTorah series {prov_podcast_id} not found")

    async def get_podcast_episodes(
        self,
        prov_podcast_id: str,
    ) -> AsyncGenerator[PodcastEpisode, None]:
        """Yield shiurim for a series using the paginated search/get endpoint."""
        if prov_podcast_id.startswith("st_"):
            _, series_id, teacher_id = prov_podcast_id.split("_", 2)
            for ep in await self._browse_series_teacher_episodes(series_id, teacher_id):
                yield ep
            return

        if prov_podcast_id.startswith("t_"):
            teacher_id = prov_podcast_id[2:]
            for ep in await self._fetch_episodes_paged(teacherID=teacher_id):
                yield ep
        else:
            for ep in await self._fetch_episodes_paged(
                seriesID=prov_podcast_id, parent_series_id=prov_podcast_id
            ):
                yield ep

    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """Return a single PodcastEpisode by shiur ID via shiur/details."""
        data = await self._api_get("shiur/details", shiurID=prov_episode_id)
        if not data or not isinstance(data, dict):
            raise MediaNotFoundError(f"YuTorah shiur {prov_episode_id} not found")
        episode = _shiur_to_episode(data, 0, self.instance_id)
        if not episode:
            raise MediaNotFoundError(f"YuTorah shiur {prov_episode_id} has no playable audio")
        return episode

    # -----------------------------------------------------------------------
    # Artist — teachers
    # -----------------------------------------------------------------------

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Return a teacher as an Artist by their numeric ID."""
        teachers_map = await self._fetch_teachers_map()
        t = teachers_map.get(prov_artist_id) or {}
        name = t.get("fullName") or f"Teacher {prov_artist_id}"
        image_url = t.get("imageURL") or ""
        return Artist(
            item_id=prov_artist_id,
            provider=self.instance_id,
            name=name,
            metadata=MediaItemMetadata(
                images=UniqueList(_make_images(image_url, self.instance_id)) or None,
            ),
            provider_mappings={
                ProviderMapping(
                    item_id=prov_artist_id,
                    provider_domain="yutorah",
                    provider_instance=self.instance_id,
                    url=f"{YUTORAH_BASE}/teachers/{_slugify(name)}/",
                )
            },
        )

    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Return the most recent shiurim by a teacher as Track objects."""
        return await self._paginate_search(
            lambda raw, i: _shiur_to_track(raw, i, self.instance_id),
            teacherID=prov_artist_id,
        )

    async def get_track(self, prov_track_id: str) -> Track:
        """Return a single shiur as a Track by its shiurID."""
        data = await self._api_get("shiur/details", shiurID=prov_track_id)
        if not data or not isinstance(data, dict):
            raise MediaNotFoundError(f"YuTorah: shiur {prov_track_id} not found")
        track = _shiur_to_track(data, 0, self.instance_id)
        if not track:
            raise MediaNotFoundError(f"YuTorah: shiur {prov_track_id} has no playable MP3")
        return track

    # -----------------------------------------------------------------------
    # Streaming
    # -----------------------------------------------------------------------

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Return stream details for a shiur.

        Calls shiur/details to retrieve the direct MP3 download URL.
        """
        data = await self._api_get("shiur/details", shiurID=item_id)
        mp3_url = (data.get("shiurFileURL") or "") if data and isinstance(data, dict) else ""

        if not mp3_url:
            raise MediaNotFoundError(f"YuTorah: no MP3 URL found for shiur {item_id}")

        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(content_type=ContentType.MP3),
            media_type=media_type,
            stream_type=StreamType.HTTP,
            path=mp3_url,
            allow_seek=True,
            can_seek=True,
        )

    # -----------------------------------------------------------------------
    # Search
    # -----------------------------------------------------------------------

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 25,
    ) -> SearchResults:
        """Search YuTorah for shiurim (tracks), series (podcasts) and teachers (artists).

        Uses search/get for full-text search. Individual shiurim are returned as Tracks;
        series as Podcasts; teachers as Artists.
        """
        results = SearchResults()

        data = await self._api_get("search/get", searchTerm=search_query or "", getFacets=True)
        if not data:
            return results

        facet_fields = (data.get("facet_counts") or {}).get("facet_fields") or {}

        if MediaType.TRACK in media_types:
            docs = _extract_docs(data)
            tracks: list[Track] = []
            for i, raw in enumerate(docs[:limit]):
                track = _shiur_to_track(raw, i, self.instance_id)
                if track:
                    tracks.append(track)
            results.tracks = tracks

        if MediaType.PODCAST in media_types:
            podcasts: list[Podcast] = []
            for facet in (facet_fields.get("series") or [])[:limit]:
                sid = str(facet.get("SeriesId") or "")
                name = facet.get("SeriesName") or ""
                if not sid or not name:
                    continue
                podcasts.append(
                    Podcast(
                        item_id=sid,
                        provider=self.instance_id,
                        name=name,
                        provider_mappings={
                            ProviderMapping(
                                item_id=sid,
                                provider_domain="yutorah",
                                provider_instance=self.instance_id,
                            )
                        },
                    )
                )
            results.podcasts = podcasts

        if MediaType.ARTIST in media_types:
            teachers_map = await self._fetch_teachers_map()
            artists: list[Artist] = []
            for facet in (facet_fields.get("teachers") or [])[:limit]:
                tid = str(facet.get("TeacherId") or "")
                name = facet.get("TeacherName") or ""
                if not tid or not name:
                    continue
                teacher_data = teachers_map.get(tid) or {}
                image_url = teacher_data.get("imageURL") or ""
                images = _make_images(image_url, self.instance_id)
                artists.append(
                    Artist(
                        item_id=tid,
                        provider=self.instance_id,
                        name=name,
                        metadata=MediaItemMetadata(
                            images=UniqueList(images) if images else None,
                        ),
                        provider_mappings={
                            ProviderMapping(
                                item_id=tid,
                                provider_domain="yutorah",
                                provider_instance=self.instance_id,
                            )
                        },
                    )
                )
            results.artists = artists

        return results

    # -----------------------------------------------------------------------
    # Internal — API calls
    # -----------------------------------------------------------------------

    async def _fetch_episodes_paged(
        self,
        parent_series_id: str | None = None,
        **filter_params: Any,
    ) -> list[PodcastEpisode]:
        """Fetch episodes from search/get with automatic pagination.

        Passes any keyword args as extra filter params (e.g. seriesID, teacherID).
        """
        return await self._paginate_search(
            lambda raw, i: _shiur_to_episode(raw, i, self.instance_id, parent_series_id),
            **filter_params,
        )

    async def _paginate_search(
        self,
        converter: Callable[[dict[str, Any], int], _T | None],
        **filter_params: Any,
    ) -> list[_T]:
        """Paginate search/get results, applying converter to each doc.

        :param converter: Called with (raw_doc, current_count); returns an item or None to skip.
        :param filter_params: Extra filter kwargs forwarded to the API (e.g. seriesID, teacherID).
        """
        results: list[_T] = []
        page = 1
        while len(results) < MAX_EPISODES:
            extra: dict[str, Any] = (
                {"getFacets": True} if page == 1 else {"getFacets": False, "start": page}
            )
            data = await self._api_get("search/get", searchTerm="", **filter_params, **extra)
            docs = _extract_docs(data)
            if not docs:
                break
            for raw in docs:
                item = converter(raw, len(results))
                if item is not None:
                    results.append(item)
            if len(docs) < PAGE_SIZE:
                break
            page += 1
        return results

    async def _api_get(self, endpoint: str, **params: Any) -> Any:
        """Make a GET request to the YuTorah JSON API and return parsed JSON.

        Returns None for 404 responses. Raises ProviderUnavailableError for other errors.
        """
        str_params = {
            k: str(v).lower() if isinstance(v, bool) else str(v)
            for k, v in params.items()
            if v is not None
        }
        # The YuTorah API accepts the auth token as a query parameter named
        # "userToken" (confirmed by OkHttp interceptor in APK source). Sending
        # it only as an HTTP header does not work — the server ignores it.
        str_params["userToken"] = self._user_token
        safe_params = {k: v for k, v in str_params.items() if k != "userToken"}
        try:
            async with self.mass.http_session.get(
                f"{API_BASE}{endpoint}",
                params=str_params,
                headers=API_HEADERS,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 404:
                    return None
                resp.raise_for_status()
                raw_text = await resp.text()
                return json.loads(raw_text)
        except aiohttp.ClientResponseError as exc:
            raise ProviderUnavailableError(
                f"YuTorah API {endpoint} failed (params={safe_params}): {exc}"
            ) from exc
        except aiohttp.ClientError as exc:
            raise ProviderUnavailableError(
                f"YuTorah API {endpoint} network error (params={safe_params}): {exc}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise ProviderUnavailableError(
                f"YuTorah API {endpoint} returned invalid JSON: {exc}"
            ) from exc

    @use_cache(3600)
    async def _fetch_series_list(self) -> list[dict[str, Any]]:
        """Fetch the full list of curated series from browse/series, cached for 1 hour."""
        data = await self._api_get("browse/series", favoritesOnly=False)
        return data if isinstance(data, list) else []

    @use_cache(3600)
    async def _fetch_teachers_map(self) -> dict[str, dict[str, Any]]:
        """Fetch and cache all teachers as a dict keyed by teacher ID string."""
        data = await self._api_get("browse/teachers", favoritesOnly=False)
        if not isinstance(data, list):
            return {}
        return {str(t.get("ID") or ""): t for t in data if t.get("ID")}
