"""HTTP client for the iHeartRadio API."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import aiohttp
from music_assistant_models.errors import (
    InvalidDataError,
    LoginFailed,
    MediaNotFoundError,
    MusicAssistantError,
    ProviderUnavailableError,
    RateLimited,
    ResourceTemporarilyUnavailable,
)

from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.json import json_loads
from music_assistant.helpers.throttle_retry import (
    ThrottlerManager,
    parse_retry_after,
    throttle_with_retries,
)

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
    EPISODE_PAGE_LIMIT,
    HEADER_HOST_NAME,
    HEADER_LOCALE,
    MARKET_PAGE_LIMIT,
    MAX_EPISODE_PAGES,
    PATH_ARTIST_PROFILE,
    PATH_CATALOG_ALBUM,
    PATH_CATALOG_TRACK,
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
    SESSION_EXPIRED_CODES,
    STATION_PAGE_LIMIT,
)

if TYPE_CHECKING:
    import logging

    from music_assistant import MusicAssistant

    from .provider import IHeartRadioProvider


class IHeartRadioApiClient:
    """Talk to the iHeartRadio API of one country, with the provider's session."""

    # Shared by every instance because the API rate-limits the client, not the account.
    throttler: ThrottlerManager = ThrottlerManager(rate_limit=5, period=1)

    def __init__(self, provider: IHeartRadioProvider, country: str) -> None:
        """
        Initialize the client.

        :param provider: The provider owning this client.
        :param country: The country whose catalogue to address.
        """
        self.provider = provider
        self.country = country
        self.base_url = API_BASE_URLS[country]
        self.host_name = f"webapp.{country.upper()}"
        self._headers = {
            "Accept": "application/json",
            HEADER_HOST_NAME: self.host_name,
            HEADER_LOCALE: API_LOCALE,
        }

    @property
    def mass(self) -> MusicAssistant:
        """Return the Music Assistant instance."""
        return self.provider.mass

    @property
    def logger(self) -> logging.Logger:
        """Return the provider's logger."""
        return self.provider.logger

    @property
    def domain(self) -> str:
        """Return the provider domain."""
        return self.provider.domain

    @property
    def instance_id(self) -> str:
        """Return the provider instance id, which scopes the cache."""
        return f"{self.provider.instance_id}:{self.country}"

    @throttle_with_retries
    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        form: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        authenticated: bool = True,
        retry_auth: bool = True,
    ) -> Any:
        """
        Perform a request against the iHeartRadio API and return the decoded JSON body.

        Returns None for a response without a body; raises MediaNotFoundError when the API
        holds nothing for the request (400, 404 or 410) and LoginFailed when it rejects the
        session or the credentials.

        :param method: The HTTP method.
        :param path: The API path to request.
        :param params: Query parameters; entries with a None value are left out.
        :param json: A JSON body to send.
        :param form: A form-encoded body to send.
        :param headers: Extra headers, on top of the API and session headers.
        :param authenticated: Whether to send the session headers.
        :param retry_auth: Whether a rejected session is renewed once and the request retried.
        """
        url = f"{self.base_url}{path}"
        query = {key: str(val) for key, val in (params or {}).items() if val is not None}
        request_headers = dict(self._headers)
        auth = self.provider.auth
        session = auth.session if auth is not None else None
        if authenticated and session is not None:
            request_headers.update(session.headers)
        request_headers.update(headers or {})
        try:
            async with self.mass.http_session.request(
                method,
                url,
                params=query,
                json=json,
                data=form,
                headers=request_headers,
                timeout=API_TIMEOUT,
            ) as response:
                body = await response.read()
                session_rejected = response.status == 401 or (
                    response.status == 400 and _error_code(body) in SESSION_EXPIRED_CODES
                )
                if not session_rejected:
                    if not authenticated and response.status == 400:
                        raise LoginFailed(
                            f"iHeartRadio rejected the login: {_error_description(body)}"
                        )
                    return self._handle_response(url, response, body)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise ResourceTemporarilyUnavailable(
                f"Network error contacting iHeartRadio: {err}"
            ) from err
        if not authenticated:
            raise LoginFailed(f"iHeartRadio rejected the login: {_error_description(body)}")
        if not retry_auth or auth is None:
            raise LoginFailed("iHeartRadio no longer accepts the session")
        # the session has gone stale; open a new one and try once more
        await auth.relogin(stale=session)
        return await self.request(
            method,
            path,
            params=params,
            json=json,
            form=form,
            headers=headers,
            authenticated=authenticated,
            retry_auth=False,
        )

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """
        Perform a GET against the iHeartRadio API and return the decoded JSON body.

        :param path: The API path to request.
        :param params: Query parameters; entries with a None value are left out.
        """
        return await self.request("GET", path, params=params)

    @use_cache(CACHE_TTL_STATION, category=CACHE_CATEGORY_STATIONS)
    async def get_station(self, station_id: str) -> dict[str, Any] | None:
        """
        Return the payload of a single live station, or None when it does not exist.

        :param station_id: The iHeartRadio live station id.
        """
        payload = await self.get_json(PATH_LIVE_STATION.format(station_id=station_id))
        hits = json_items(payload.get("hits")) if isinstance(payload, dict) else []
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
            payload = await self.get_json(
                PATH_LIVE_STATIONS,
                {
                    "countryCode": self.country.upper(),
                    "limit": STATION_PAGE_LIMIT,
                    "offset": len(stations),
                    "marketId": market_id,
                    "genreId": genre_id,
                },
            )
            hits = json_items(payload.get("hits")) if isinstance(payload, dict) else []
            stations.extend(hits)
            if len(hits) < STATION_PAGE_LIMIT:
                return stations

    @use_cache(CACHE_TTL_CATALOG, category=CACHE_CATEGORY_CATALOG)
    async def get_markets(self) -> list[dict[str, Any]]:
        """Return the markets (cities) of the configured country."""
        payload = await self.get_json(
            PATH_MARKETS,
            {"countryCode": self.country.upper(), "limit": MARKET_PAGE_LIMIT},
        )
        return json_items(payload.get("hits")) if isinstance(payload, dict) else []

    @use_cache(CACHE_TTL_CATALOG, category=CACHE_CATEGORY_CATALOG)
    async def get_genres(self) -> list[dict[str, Any]]:
        """Return the genres live stations are grouped by."""
        payload = await self.get_json(PATH_GENRES, {"genreType": "liveStation"})
        return json_items(payload.get("genres")) if isinstance(payload, dict) else []

    @use_cache(CACHE_TTL_CATALOG, category=CACHE_CATEGORY_CATALOG)
    async def get_artist_profile(self, artist_id: str) -> dict[str, Any]:
        """
        Return the profile of an artist.

        :param artist_id: The iHeartRadio artist id.
        """
        payload = await self.get_json(PATH_ARTIST_PROFILE.format(artist_id=artist_id))
        return payload if isinstance(payload, dict) else {}

    @use_cache(CACHE_TTL_CATALOG, category=CACHE_CATEGORY_CATALOG)
    async def get_catalog_track(self, track_id: str) -> dict[str, Any] | None:
        """
        Return the catalog payload of a track, or None when it does not exist.

        :param track_id: The iHeartRadio track id.
        """
        payload = await self.get_json(PATH_CATALOG_TRACK.format(track_id=track_id))
        tracks = json_items(payload.get("tracks")) if isinstance(payload, dict) else []
        return tracks[0] if tracks else None

    @use_cache(CACHE_TTL_CATALOG, category=CACHE_CATEGORY_CATALOG)
    async def get_catalog_album(self, album_id: str) -> dict[str, Any] | None:
        """
        Return the catalog payload of an album, or None when it does not exist.

        :param album_id: The iHeartRadio album id.
        """
        payload = await self.get_json(PATH_CATALOG_ALBUM.format(album_id=album_id))
        return payload if isinstance(payload, dict) and payload else None

    @use_cache(CACHE_TTL_CATALOG, category=CACHE_CATEGORY_CATALOG)
    async def get_podcast_categories(self) -> list[dict[str, Any]]:
        """Return the categories podcasts are grouped by."""
        payload = await self.get_json(PATH_PODCAST_CATEGORIES)
        return json_items(payload.get("categories")) if isinstance(payload, dict) else []

    @use_cache(CACHE_TTL_CATALOG, category=CACHE_CATEGORY_CATALOG)
    async def get_podcast_category(self, category_id: str) -> list[dict[str, Any]]:
        """
        Return the podcasts of a single category.

        :param category_id: The iHeartRadio podcast category id.
        """
        payload = await self.get_json(PATH_PODCAST_CATEGORY.format(category_id=category_id))
        return json_items(payload.get("podcasts")) if isinstance(payload, dict) else []

    @use_cache(CACHE_TTL_PODCAST, category=CACHE_CATEGORY_PODCASTS)
    async def get_podcast_data(self, podcast_id: str) -> dict[str, Any] | None:
        """
        Return the payload of a single podcast, or None when it does not exist.

        :param podcast_id: The iHeartRadio podcast id.
        """
        payload = await self.get_json(PATH_PODCAST.format(podcast_id=podcast_id))
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
            payload = await self.get_json(
                PATH_PODCAST_EPISODES.format(podcast_id=podcast_id),
                {"limit": EPISODE_PAGE_LIMIT, "pageKey": page_key},
            )
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                if episodes:
                    # a broken page past the first would otherwise cache a truncated listing
                    raise InvalidDataError(
                        f"iHeartRadio returned an invalid episode page for podcast {podcast_id}"
                    )
                break
            episodes.extend(json_items(payload["data"]))
            links = payload.get("links") or {}
            if not (page_key := links.get("next") if isinstance(links, dict) else None):
                break
        else:
            self.logger.debug(
                "Listing podcast %s stopped at %s episodes; older episodes are left out",
                podcast_id,
                len(episodes),
            )
        return episodes

    @use_cache(CACHE_TTL_PODCAST, category=CACHE_CATEGORY_PODCASTS)
    async def get_episode(self, episode_id: str) -> dict[str, Any] | None:
        """
        Return the payload of a single podcast episode, or None when it does not exist.

        :param episode_id: The iHeartRadio episode id.
        """
        payload = await self.get_json(PATH_PODCAST_EPISODE.format(episode_id=episode_id))
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
        # deliberately not cached, this is the live signal driving the player's metadata
        payload = await self.get_json(PATH_NOW_PLAYING.format(station_id=station_id))
        return payload if isinstance(payload, dict) and payload else None

    @use_cache(CACHE_TTL_SEARCH, category=CACHE_CATEGORY_SEARCH)
    async def search(
        self, keywords: str, want_radio: bool, want_podcasts: bool, limit: int
    ) -> dict[str, Any]:
        """
        Return the raw search results of the configured country for a query.

        :param keywords: The words to search for.
        :param want_radio: Whether to ask for live stations and artists.
        :param want_podcasts: Whether to ask for podcasts.
        :param limit: The maximum number of hits per kind.
        """
        payload = await self.get_json(
            PATH_SEARCH,
            {
                "keywords": keywords,
                "maxRows": limit,
                "bundle": "false",
                "station": query_flag(want_radio),
                "podcast": query_flag(want_podcasts),
                # an artist hit is offered as its artist radio
                "artist": query_flag(want_radio),
                "track": query_flag(False),
                "album": query_flag(False),
                "playlist": query_flag(False),
            },
        )
        return payload.get("results") or {} if isinstance(payload, dict) else {}

    def _handle_response(self, url: str, response: aiohttp.ClientResponse, body: bytes) -> Any:
        """
        Turn an API response into its decoded body, or the error it stands for.

        :param url: The requested url, for error and log messages.
        :param response: The response.
        :param body: The raw response body.
        """
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
        if response.status in (400, 404, 410):
            raise self._not_found_error(url, body)
        if not 200 <= response.status < 300:
            raise ProviderUnavailableError(
                f"iHeartRadio request to {url} failed ({response.status})"
            )
        if not body:
            return None
        try:
            return json_loads(body)
        except ValueError as err:
            raise InvalidDataError(f"iHeartRadio returned an invalid response for {url}") from err

    def _not_found_error(self, url: str, body: bytes) -> MusicAssistantError:
        """
        Return the error to raise for a request the API holds nothing for.

        :param url: The requested url, for the log message.
        :param body: The raw response body, holding the API's own reason.
        """
        reason = _error_description(body) or "not found"
        self.logger.debug("iHeartRadio holds nothing for %s: %s", url, reason)
        return MediaNotFoundError(f"iHeartRadio: {reason}")


def json_items(value: Any) -> list[dict[str, Any]]:
    """Return the object entries of a (possibly missing) JSON list."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def query_flag(value: bool) -> str:
    """Render a boolean as the API's own query flag."""
    return "true" if value else "false"


def _error_payload(body: bytes) -> dict[str, Any] | None:
    """Return the error object of an API error body, if it carries one."""
    try:
        payload = json_loads(body)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    # a 400 carries an error object or an errors list, a 410 a reason list
    if isinstance(error := payload.get("error"), dict):
        return error
    if errors := json_items(payload.get("errors")):
        return errors[0]
    return payload


def _error_code(body: bytes) -> int | None:
    """Return the API's own error code of an error body, if it carries one."""
    error = _error_payload(body)
    code = error.get("code") if error else None
    return code if isinstance(code, int) else None


def _error_description(body: bytes) -> str:
    """Return the API's own description of an error body, empty when it carries none."""
    if not (error := _error_payload(body)):
        return ""
    if description := error.get("description"):
        return str(description)
    if isinstance(reason := error.get("reason"), list):
        return ", ".join(str(part) for part in reason)
    return ""
