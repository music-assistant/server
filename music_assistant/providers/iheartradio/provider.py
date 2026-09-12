"""iHeartRadio music provider for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import aiohttp
from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.errors import (
    InvalidDataError,
    MediaNotFoundError,
    MusicAssistantError,
    ProviderUnavailableError,
    RateLimited,
    ResourceTemporarilyUnavailable,
)
from music_assistant_models.media_items import SearchResults

from music_assistant.constants import CONF_ENTRY_UNOFFICIAL_PROVIDER
from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.json import json_loads
from music_assistant.helpers.podcast_parsers import rank_episodes_by_date
from music_assistant.helpers.throttle_retry import (
    ThrottlerManager,
    parse_retry_after,
    throttle_with_retries,
)
from music_assistant.models.music_provider import MusicProvider

from .browse import IHeartRadioBrowseManager
from .constants import (
    API_BASE_URLS,
    API_LOCALE,
    API_TIMEOUT,
    CACHE_CATEGORY_CATALOG,
    CACHE_CATEGORY_PODCASTS,
    CACHE_CATEGORY_SEARCH,
    CACHE_CATEGORY_STATIONS,
    CACHE_TTL_CATALOG,
    CACHE_TTL_EPISODES,
    CACHE_TTL_PODCAST,
    CACHE_TTL_SEARCH,
    CACHE_TTL_STATION,
    CONF_COUNTRY,
    DEFAULT_COUNTRY,
    EPISODE_PAGE_LIMIT,
    HEADER_HOST_NAME,
    HEADER_LOCALE,
    MARKET_PAGE_LIMIT,
    MAX_EPISODE_PAGES,
    PATH_GENRES,
    PATH_LIVE_STATION,
    PATH_LIVE_STATIONS,
    PATH_MARKETS,
    PATH_NOW_PLAYING,
    PATH_PODCAST,
    PATH_PODCAST_CATEGORIES,
    PATH_PODCAST_CATEGORY,
    PATH_PODCAST_EPISODE,
    PATH_PODCAST_EPISODES,
    PATH_SEARCH,
    STATION_PAGE_LIMIT,
)
from .parsers import parse_live_station, parse_podcast, parse_podcast_episode, split_episode_item_id
from .streaming import IHeartRadioStreamingManager

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence

    from music_assistant_models.config_entries import ConfigEntry
    from music_assistant_models.media_items import (
        BrowseFolder,
        ItemMapping,
        MediaItemType,
        Podcast,
        PodcastEpisode,
        Radio,
    )
    from music_assistant_models.streamdetails import StreamDetails

SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
}


class IHeartRadioProvider(MusicProvider):
    """iHeartRadio music provider."""

    # Shared by every instance: the API rate-limits the client, not the account, so two
    # instances (e.g. two countries) must not each get a full budget.
    throttler: ThrottlerManager = ThrottlerManager(rate_limit=5, period=1)

    browse_manager: IHeartRadioBrowseManager
    streaming_manager: IHeartRadioStreamingManager

    _country: str
    _base_url: str
    _headers: dict[str, str]

    @property
    def max_concurrent_streams(self) -> None:
        """Allow unlimited concurrent upstream source streams."""
        # iHeartRadio states no limit for live radio or podcasts.
        return None

    @property
    def supported_media_types(self) -> set[MediaType]:
        """Return the media types this provider can serve."""
        # Without library support the base implementation would report none, which would
        # keep the provider out of search-based lookups.
        return {MediaType.RADIO, MediaType.PODCAST, MediaType.PODCAST_EPISODE}

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return the (options) config entries for this provider instance."""
        return (CONF_ENTRY_UNOFFICIAL_PROVIDER,)

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        country = str(self.get_setup_value(CONF_COUNTRY) or DEFAULT_COUNTRY).lower()
        if country not in API_BASE_URLS:
            self.logger.warning("Unknown country %s configured, falling back to US", country)
            country = DEFAULT_COUNTRY
        self._country = country
        self._base_url = API_BASE_URLS[country]
        self._headers = {
            "Accept": "application/json",
            HEADER_HOST_NAME: f"webapp.{country.upper()}",
            HEADER_LOCALE: API_LOCALE,
        }
        self.browse_manager = IHeartRadioBrowseManager(self)
        self.streaming_manager = IHeartRadioStreamingManager(self)

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """
        Get full radio details by id.

        :param prov_radio_id: The iHeartRadio live station id.
        """
        station = await self.get_station(prov_radio_id)
        radio = (
            parse_live_station(station, self.instance_id, self.domain)
            if station is not None
            else None
        )
        if radio is None:
            raise MediaNotFoundError(f"Station {prov_radio_id} not found")
        return radio

    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """
        Get full podcast details by id.

        :param prov_podcast_id: The iHeartRadio podcast id.
        """
        data = await self.get_podcast_data(prov_podcast_id)
        podcast = parse_podcast(data, self.instance_id, self.domain) if data is not None else None
        if podcast is None:
            raise MediaNotFoundError(f"Podcast {prov_podcast_id} not found")
        return podcast

    async def get_podcast_episodes(self, prov_podcast_id: str) -> AsyncGenerator[PodcastEpisode]:
        """
        Get all episodes of a podcast, newest episode holding the highest position.

        :param prov_podcast_id: The iHeartRadio podcast id.
        """
        podcast = await self.get_podcast_data(prov_podcast_id) or {}
        for position, episode in await self._positioned_episodes(prov_podcast_id):
            if mass_episode := parse_podcast_episode(
                episode, prov_podcast_id, position, self.instance_id, self.domain, podcast
            ):
                yield mass_episode

    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """
        Get full podcast episode details by id.

        :param prov_episode_id: The MA episode id, holding the podcast and episode id.
        """
        if (parsed := split_episode_item_id(prov_episode_id)) is None:
            raise MediaNotFoundError(f"Not an iHeartRadio episode: {prov_episode_id}")
        podcast_id, episode_id = parsed
        episode = await self.get_episode(episode_id)
        if episode is None:
            raise MediaNotFoundError(f"Episode {episode_id} not found")
        podcast = await self.get_podcast_data(podcast_id) or {}
        mass_episode = parse_podcast_episode(
            episode,
            podcast_id,
            await self._episode_position(podcast_id, episode_id),
            self.instance_id,
            self.domain,
            podcast,
        )
        if mass_episode is None:
            raise MediaNotFoundError(f"Episode {episode_id} not found")
        return mass_episode

    @use_cache(CACHE_TTL_SEARCH, category=CACHE_CATEGORY_SEARCH)
    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 5,
    ) -> SearchResults:
        """
        Search the iHeartRadio catalogue for stations and podcasts.

        :param search_query: The query to search for.
        :param media_types: The media types to include in the results.
        :param limit: The maximum number of results per media type.
        """
        want_radio = MediaType.RADIO in media_types
        want_podcasts = MediaType.PODCAST in media_types
        if not (want_radio or want_podcasts) or not (keywords := search_query.strip()):
            return SearchResults()
        payload = await self._get_json(
            PATH_SEARCH,
            {
                "keywords": keywords,
                "maxRows": limit,
                "bundle": "false",
                "station": _flag(want_radio),
                "podcast": _flag(want_podcasts),
                "artist": _flag(False),
                "track": _flag(False),
                "album": _flag(False),
                "playlist": _flag(False),
            },
        )
        results = payload.get("results") or {} if isinstance(payload, dict) else {}
        radio: list[Radio] = []
        podcasts: list[Podcast] = []
        if want_radio:
            # a search hit carries no streams; they are resolved when playback starts
            radio = [
                station
                for hit in _items(results.get("stations"))[:limit]
                if (station := parse_live_station(hit, self.instance_id, self.domain))
            ]
        if want_podcasts:
            podcasts = [
                podcast
                for hit in _items(results.get("podcasts"))[:limit]
                if (podcast := parse_podcast(hit, self.instance_id, self.domain))
            ]
        return SearchResults(radio=radio, podcasts=podcasts)

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Browse this provider's items.

        :param path: The path to browse, e.g. ``iheartradio--xx://live/genres``.
        """
        return await self.browse_manager.browse(path)

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """
        Get the stream details for a live station or a podcast episode.

        :param item_id: The live station id, or the MA episode id.
        :param media_type: The media type to stream.
        """
        return await self.streaming_manager.get_stream_details(item_id, media_type)

    @use_cache(CACHE_TTL_STATION, category=CACHE_CATEGORY_STATIONS)
    async def get_station(self, station_id: str) -> dict[str, Any] | None:
        """
        Return the payload of a single live station, or None when it does not exist.

        :param station_id: The iHeartRadio live station id.
        """
        payload = await self._get_json(PATH_LIVE_STATION.format(station_id=station_id))
        hits = _items(payload.get("hits")) if isinstance(payload, dict) else []
        return hits[0] if hits else None

    @use_cache(CACHE_TTL_STATION, category=CACHE_CATEGORY_STATIONS)
    async def get_stations(
        self, market_id: str | None = None, genre_id: str | None = None
    ) -> list[dict[str, Any]]:
        """
        Return the live stations of the configured country.

        :param market_id: Only return the stations of this market (city).
        :param genre_id: Only return the stations of this genre.
        """
        stations: list[dict[str, Any]] = []
        while True:
            payload = await self._get_json(
                PATH_LIVE_STATIONS,
                {
                    "countryCode": self._country.upper(),
                    "limit": STATION_PAGE_LIMIT,
                    "offset": len(stations),
                    "marketId": market_id,
                    "genreId": genre_id,
                },
            )
            hits = _items(payload.get("hits")) if isinstance(payload, dict) else []
            stations.extend(hits)
            if len(hits) < STATION_PAGE_LIMIT:
                return stations

    @use_cache(CACHE_TTL_CATALOG, category=CACHE_CATEGORY_CATALOG)
    async def get_markets(self) -> list[dict[str, Any]]:
        """Return the markets (cities) of the configured country."""
        payload = await self._get_json(
            PATH_MARKETS,
            {"countryCode": self._country.upper(), "limit": MARKET_PAGE_LIMIT},
        )
        return _items(payload.get("hits")) if isinstance(payload, dict) else []

    @use_cache(CACHE_TTL_CATALOG, category=CACHE_CATEGORY_CATALOG)
    async def get_genres(self) -> list[dict[str, Any]]:
        """Return the genres live stations are grouped by."""
        payload = await self._get_json(PATH_GENRES, {"genreType": "liveStation"})
        return _items(payload.get("genres")) if isinstance(payload, dict) else []

    @use_cache(CACHE_TTL_CATALOG, category=CACHE_CATEGORY_CATALOG)
    async def get_podcast_categories(self) -> list[dict[str, Any]]:
        """Return the categories podcasts are grouped by."""
        payload = await self._get_json(PATH_PODCAST_CATEGORIES)
        return _items(payload.get("categories")) if isinstance(payload, dict) else []

    @use_cache(CACHE_TTL_CATALOG, category=CACHE_CATEGORY_CATALOG)
    async def get_podcast_category(self, category_id: str) -> list[dict[str, Any]]:
        """
        Return the podcasts of a single category.

        :param category_id: The iHeartRadio podcast category id.
        """
        payload = await self._get_json(PATH_PODCAST_CATEGORY.format(category_id=category_id))
        return _items(payload.get("podcasts")) if isinstance(payload, dict) else []

    @use_cache(CACHE_TTL_PODCAST, category=CACHE_CATEGORY_PODCASTS)
    async def get_podcast_data(self, podcast_id: str) -> dict[str, Any] | None:
        """
        Return the payload of a single podcast, or None when it does not exist.

        :param podcast_id: The iHeartRadio podcast id.
        """
        payload = await self._get_json(PATH_PODCAST.format(podcast_id=podcast_id))
        return payload if isinstance(payload, dict) and payload else None

    @use_cache(CACHE_TTL_EPISODES, category=CACHE_CATEGORY_PODCASTS)
    async def get_episodes(self, podcast_id: str) -> list[dict[str, Any]]:
        """
        Return the episodes of a podcast, as the API lists them (newest first).

        :param podcast_id: The iHeartRadio podcast id.
        """
        episodes: list[dict[str, Any]] = []
        page_key: str | None = None
        for _ in range(MAX_EPISODE_PAGES):
            payload = await self._get_json(
                PATH_PODCAST_EPISODES.format(podcast_id=podcast_id),
                {"limit": EPISODE_PAGE_LIMIT, "pageKey": page_key},
            )
            if not isinstance(payload, dict):
                break
            episodes.extend(_items(payload.get("data")))
            links = payload.get("links") or {}
            if not (page_key := links.get("next") if isinstance(links, dict) else None):
                break
        return episodes

    @use_cache(CACHE_TTL_PODCAST, category=CACHE_CATEGORY_PODCASTS)
    async def get_episode(self, episode_id: str) -> dict[str, Any] | None:
        """
        Return the payload of a single podcast episode, or None when it does not exist.

        :param episode_id: The iHeartRadio episode id.
        """
        payload = await self._get_json(PATH_PODCAST_EPISODE.format(episode_id=episode_id))
        if not isinstance(payload, dict):
            return None
        # the episode endpoint wraps its payload; only it carries the media url
        episode = payload.get("episode")
        return cast("dict[str, Any]", episode) if isinstance(episode, dict) else None

    async def get_now_playing(self, station_id: str) -> dict[str, Any] | None:
        """
        Return what a live station is playing right now, or None when nothing is playing.

        Raises MediaNotFoundError for a station that publishes no track metadata at all.

        :param station_id: The iHeartRadio live station id.
        """
        # deliberately not cached: this is the live signal driving the player's metadata
        payload = await self._get_json(PATH_NOW_PLAYING.format(station_id=station_id))
        return payload if isinstance(payload, dict) and payload else None

    async def _episode_position(self, podcast_id: str, episode_id: str) -> int:
        """
        Return the listing position of one episode, or 0 when it cannot be placed.

        :param podcast_id: The iHeartRadio podcast id.
        :param episode_id: The iHeartRadio episode id.
        """
        # the episode endpoint reports no episode number, so the position comes from the
        # (cached) listing
        for position, episode in await self._positioned_episodes(podcast_id):
            if str(episode.get("id")) == episode_id:
                return position
        return 0

    async def _positioned_episodes(self, podcast_id: str) -> list[tuple[int, dict[str, Any]]]:
        """
        Return a podcast's episodes with their listing position, oldest to newest.

        :param podcast_id: The iHeartRadio podcast id.
        """
        episodes = await self.get_episodes(podcast_id)
        positions = rank_episodes_by_date(
            [episode.get("startDate") or None for episode in episodes]
        )
        return list(zip(positions, episodes, strict=True))

    @throttle_with_retries
    async def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """
        Perform a GET against the iHeartRadio API and return the decoded JSON body.

        Returns None for an empty (204) response; raises MediaNotFoundError when the API
        holds nothing for the request (400 or 410).

        :param path: The API path to request.
        :param params: Query parameters; entries with a None value are left out.
        """
        url = f"{self._base_url}{path}"
        query = {key: str(val) for key, val in (params or {}).items() if val is not None}
        try:
            async with self.mass.http_session.get(
                url, params=query, headers=self._headers, timeout=API_TIMEOUT
            ) as response:
                if response.status == 204:
                    return None
                if response.status == 429:
                    raise RateLimited(
                        "iHeartRadio is rate limiting us",
                        backoff_time=parse_retry_after(response.headers.get("Retry-After")),
                    )
                if response.status >= 500:
                    raise ResourceTemporarilyUnavailable(
                        f"iHeartRadio is temporarily unavailable ({response.status})",
                        backoff_time=parse_retry_after(response.headers.get("Retry-After")),
                    )
                if response.status in (400, 410):
                    raise self._not_found_error(url, await response.read())
                if response.status != 200:
                    raise ProviderUnavailableError(
                        f"iHeartRadio request to {path} failed ({response.status})"
                    )
                try:
                    return await response.json(loads=json_loads, content_type=None)
                except ValueError as err:
                    raise InvalidDataError(
                        f"iHeartRadio returned an invalid response for {path}"
                    ) from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise ResourceTemporarilyUnavailable(
                f"Network error contacting iHeartRadio: {err}"
            ) from err

    def _not_found_error(self, url: str, body: bytes) -> MusicAssistantError:
        """
        Return the error to raise for a request the API holds nothing for.

        :param url: The requested url, for the log message.
        :param body: The raw response body, holding the API's own reason.
        """
        try:
            payload = json_loads(body)
        except ValueError:
            payload = None
        # a 400 carries {"error": {"description": ..., "code": ...}}, a 410 {"reason": [...]}
        reason = "not found"
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                reason = str(error.get("description") or reason)
            elif isinstance(payload.get("reason"), list):
                reason = ", ".join(str(part) for part in payload["reason"]) or reason
        self.logger.debug("iHeartRadio holds nothing for %s: %s", url, reason)
        return MediaNotFoundError(f"iHeartRadio: {reason}")


def _flag(value: bool) -> str:
    """Render a boolean as the API's own query flag."""
    return "true" if value else "false"


def _items(value: Any) -> list[dict[str, Any]]:
    """Return the object entries of a (possibly missing) JSON list."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]
