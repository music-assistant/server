"""iHeartRadio music provider for Music Assistant."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, cast

import aiohttp
from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.errors import (
    InvalidDataError,
    LoginFailed,
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

from .auth import IHeartRadioAuthManager
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
    PATH_ARTIST_PROFILE,
    PATH_ARTIST_STATION,
    PATH_CATALOG_ALBUM,
    PATH_CATALOG_TRACK,
    PATH_GENRES,
    PATH_LIVE_STATION,
    PATH_LIVE_STATIONS,
    PATH_MARKETS,
    PATH_NOW_PLAYING,
    PATH_PLAYBACK_REPORTING,
    PATH_PLAYBACK_STREAMS,
    PATH_PODCAST,
    PATH_PODCAST_CATEGORIES,
    PATH_PODCAST_CATEGORY,
    PATH_PODCAST_EPISODE,
    PATH_PODCAST_EPISODES,
    PATH_SEARCH,
    PLAYED_FROM,
    REPORT_STATUS_DONE,
    REPORT_STATUS_SKIP,
    SESSION_EXPIRED_CODES,
    STATION_OUT_OF_SONGS_CODE,
    STATION_PAGE_LIMIT,
    STATION_TYPE_RADIO,
)
from .library import IHeartRadioLibraryManager
from .parsers import (
    parse_album,
    parse_artist,
    parse_artist_radio,
    parse_live_station,
    parse_podcast,
    parse_podcast_episode,
    parse_track,
    split_artist_radio_item_id,
    split_episode_item_id,
)
from .stations import ArtistRadioStation, ArtistRadioStore
from .streaming import IHeartRadioStreamingManager

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence

    from music_assistant_models.config_entries import ConfigEntry
    from music_assistant_models.media_items import (
        Album,
        Artist,
        BrowseFolder,
        ItemMapping,
        MediaItemType,
        Podcast,
        PodcastEpisode,
        Radio,
        Track,
    )
    from music_assistant_models.streamdetails import StreamDetails

SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
}
# What a signed-in account adds: its followed stations, artist radios and podcasts.
LIBRARY_FEATURES = {
    ProviderFeature.LIBRARY_RADIOS,
    ProviderFeature.LIBRARY_RADIOS_EDIT,
    ProviderFeature.LIBRARY_PODCASTS,
    ProviderFeature.LIBRARY_PODCASTS_EDIT,
}


class IHeartRadioProvider(MusicProvider):
    """iHeartRadio music provider."""

    # Shared by every instance: the API rate-limits the client, not the account, so two
    # instances (e.g. two countries) must not each get a full budget.
    throttler: ThrottlerManager = ThrottlerManager(rate_limit=5, period=1)

    auth: IHeartRadioAuthManager | None = None
    browse_manager: IHeartRadioBrowseManager
    library_manager: IHeartRadioLibraryManager
    streaming_manager: IHeartRadioStreamingManager
    stations: ArtistRadioStore

    host_name: str
    _country: str
    _base_url: str
    _headers: dict[str, str]

    @property
    def max_concurrent_streams(self) -> None:
        """Allow unlimited concurrent upstream source streams."""
        # iHeartRadio states no limit for live radio or podcasts.
        return None

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the supported features, library sync only with a signed-in account."""
        if self.auth is not None and self.auth.is_account:
            return SUPPORTED_FEATURES | LIBRARY_FEATURES
        return set(SUPPORTED_FEATURES)

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
        self.host_name = f"webapp.{country.upper()}"
        self._headers = {
            "Accept": "application/json",
            HEADER_HOST_NAME: self.host_name,
            HEADER_LOCALE: API_LOCALE,
        }
        self.stations = ArtistRadioStore()
        self.browse_manager = IHeartRadioBrowseManager(self)
        self.library_manager = IHeartRadioLibraryManager(self)
        self.streaming_manager = IHeartRadioStreamingManager(self)
        self.auth = IHeartRadioAuthManager(self)
        await self.auth.login()

    async def get_library_radios(self) -> AsyncGenerator[Radio]:
        """Retrieve the followed live stations and artist radios."""
        async for radio in self.library_manager.get_library_radios():
            yield radio

    async def get_library_podcasts(self) -> AsyncGenerator[Podcast]:
        """Retrieve the followed podcasts."""
        async for podcast in self.library_manager.get_library_podcasts():
            yield podcast

    async def library_add(self, item: MediaItemType) -> bool:
        """
        Follow a station, artist radio or podcast on iHeartRadio.

        :param item: The item to follow.
        """
        return await self.library_manager.library_add(item)

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """
        Unfollow a station, artist radio or podcast on iHeartRadio.

        :param prov_item_id: The provider item id.
        :param media_type: The media type of the item.
        """
        return await self.library_manager.library_remove(prov_item_id, media_type)

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """
        Get full radio details by id.

        :param prov_radio_id: The iHeartRadio live station id, or an artist radio id.
        """
        if artist_id := split_artist_radio_item_id(prov_radio_id):
            profile = await self.get_artist_profile(artist_id)
            radio = parse_artist_radio(profile.get("artist") or {}, self.instance_id, self.domain)
        else:
            station = await self.get_station(prov_radio_id)
            radio = (
                parse_live_station(station, self.instance_id, self.domain)
                if station is not None
                else None
            )
        if radio is None:
            raise MediaNotFoundError(f"Station {prov_radio_id} not found")
        return radio

    async def get_dynamic_radio_tracks(self, prov_radio_id: str) -> list[Track]:
        """
        Return a fresh batch of tracks for an artist radio.

        :param prov_radio_id: The artist radio id.
        """
        if (artist_id := split_artist_radio_item_id(prov_radio_id)) is None:
            raise MediaNotFoundError(f"Not an artist radio: {prov_radio_id}")
        now = time.time()
        station = self.stations.get(artist_id, now) or await self._create_station(artist_id, now)
        payload = await self.request(
            "POST",
            PATH_PLAYBACK_STREAMS,
            json={
                "contentIds": [],
                "hostName": self.host_name,
                "playedFrom": PLAYED_FROM,
                "stationId": station.station_id,
                "stationType": STATION_TYPE_RADIO,
            },
        )
        if not isinstance(payload, dict):
            raise InvalidDataError("iHeartRadio returned no tracks for the station")
        if isinstance(error := payload.get("error"), dict):
            if error.get("code") == STATION_OUT_OF_SONGS_CODE:
                raise MediaNotFoundError("This station has run out of songs to play")
            raise InvalidDataError(f"iHeartRadio refused the station: {error.get('description')}")
        items = {
            str(content["id"]): item
            for item in _items(payload.get("items"))
            if isinstance(content := item.get("content"), dict)
            and content.get("id")
            and item.get("streamUrl")
        }
        if not items:
            raise MediaNotFoundError("iHeartRadio returned no playable tracks for the station")
        station.add_batch(items, now)
        return [
            track
            for item in items.values()
            if (track := parse_track(item["content"], self.instance_id, self.domain))
        ]

    async def get_track(self, prov_track_id: str) -> Track:
        """
        Get full track details by id.

        :param prov_track_id: The iHeartRadio track id.
        """
        # a track served by an artist radio is still retained; the catalog answers for the rest
        if found := self.stations.find(prov_track_id):
            payload = found[1].get("content") or {}
        else:
            payload = await self.get_catalog_track(prov_track_id) or {}
        if (track := parse_track(payload, self.instance_id, self.domain)) is None:
            raise MediaNotFoundError(f"Track {prov_track_id} not found")
        return track

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """
        Get full artist details by id.

        :param prov_artist_id: The iHeartRadio artist id.
        """
        profile = await self.get_artist_profile(prov_artist_id)
        if (
            artist := parse_artist(profile.get("artist") or {}, self.instance_id, self.domain)
        ) is None:
            raise MediaNotFoundError(f"Artist {prov_artist_id} not found")
        return artist

    async def get_album(self, prov_album_id: str) -> Album:
        """
        Get full album details by id.

        :param prov_album_id: The iHeartRadio album id.
        """
        payload = await self.get_catalog_album(prov_album_id) or {}
        if (album := parse_album(payload, self.instance_id, self.domain)) is None:
            raise MediaNotFoundError(f"Album {prov_album_id} not found")
        return album

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """
        Get the tracks of an album, listed but not playable.

        :param prov_album_id: The iHeartRadio album id.
        """
        album = await self.get_catalog_album(prov_album_id) or {}
        # the album's own tracks carry no artwork or album reference of their own
        shared = {
            "albumId": album.get("albumId"),
            "albumName": album.get("title"),
            "imageUrl": album.get("image"),
        }
        # on-demand playback of an album needs iHeartRadio's All Access tier, which this
        # provider does not stream, so the tracks are shown but marked unavailable
        return [
            track
            for item in _items(album.get("tracks"))
            if (track := parse_track({**shared, **item}, self.instance_id, self.domain, False))
        ]

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
        Search the iHeartRadio catalogue for stations, artist radios and podcasts.

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
                # an artist hit is offered as its artist radio
                "artist": _flag(want_radio),
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
            radio += [
                artist_radio
                for hit in _items(results.get("artists"))[:limit]
                if (artist_radio := parse_artist_radio(hit, self.instance_id, self.domain))
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
        Get the stream details for a live station, a podcast episode or a radio track.

        :param item_id: The live station id, the MA episode id or the track id.
        :param media_type: The media type to stream.
        """
        return await self.streaming_manager.get_stream_details(item_id, media_type)

    async def on_played(
        self,
        media_type: MediaType,
        prov_item_id: str,
        fully_played: bool,
        position: int,
        media_item: MediaItemType,
        is_playing: bool = False,
    ) -> None:
        """
        Report a finished or skipped artist radio track to iHeartRadio.

        :param media_type: The media type of the played item.
        :param prov_item_id: The provider item id.
        :param fully_played: Whether the item was played to the end.
        :param position: The last known position in seconds.
        :param media_item: The played item.
        :param is_playing: Whether the item is still playing.
        """
        if media_type != MediaType.TRACK or is_playing:
            return
        if not fully_played and position == 0:
            # the user marked the item as unplayed; nothing was heard
            return
        status = REPORT_STATUS_DONE if fully_played else REPORT_STATUS_SKIP
        await self.report_play(prov_item_id, status, position)

    async def report_play(self, track_id: str, status: str, seconds_played: int) -> None:
        """
        Tell iHeartRadio how far an artist radio track got, the way its own player does.

        Does nothing for a track that is no longer retained.

        :param track_id: The iHeartRadio track id.
        :param status: The playback status to report.
        :param seconds_played: How many seconds of the track were played.
        """
        if (found := self.stations.find(track_id)) is None:
            return
        batch, item = found
        if not (report_payload := item.get("reportPayload")):
            return
        try:
            result = await self.request(
                "POST",
                PATH_PLAYBACK_REPORTING,
                json={
                    "modes": [],
                    "offline": False,
                    "playedDate": int(time.time() * 1000),
                    "replay": False,
                    "reportPayload": report_payload,
                    "secondsPlayed": seconds_played,
                    "stationId": batch.station_id,
                    "stationType": STATION_TYPE_RADIO,
                    "status": status,
                },
            )
        except MusicAssistantError as err:
            self.logger.debug("Could not report %s of track %s: %s", status, track_id, err)
            return
        if isinstance(result, dict):
            self.logger.debug(
                "Reported %s of track %s, skips remaining: %s this hour, %s today",
                status,
                track_id,
                result.get("hourSkipsRemaining"),
                result.get("daySkipsRemaining"),
            )

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
    async def get_artist_profile(self, artist_id: str) -> dict[str, Any]:
        """
        Return the profile of an artist.

        :param artist_id: The iHeartRadio artist id.
        """
        payload = await self._get_json(PATH_ARTIST_PROFILE.format(artist_id=artist_id))
        return payload if isinstance(payload, dict) else {}

    @use_cache(CACHE_TTL_CATALOG, category=CACHE_CATEGORY_CATALOG)
    async def get_catalog_track(self, track_id: str) -> dict[str, Any] | None:
        """
        Return the catalog payload of a track, or None when it does not exist.

        :param track_id: The iHeartRadio track id.
        """
        payload = await self._get_json(PATH_CATALOG_TRACK.format(track_id=track_id))
        tracks = _items(payload.get("tracks")) if isinstance(payload, dict) else []
        return tracks[0] if tracks else None

    @use_cache(CACHE_TTL_CATALOG, category=CACHE_CATEGORY_CATALOG)
    async def get_catalog_album(self, album_id: str) -> dict[str, Any] | None:
        """
        Return the catalog payload of an album, or None when it does not exist.

        :param album_id: The iHeartRadio album id.
        """
        payload = await self._get_json(PATH_CATALOG_ALBUM.format(album_id=album_id))
        return payload if isinstance(payload, dict) and payload else None

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
        url = f"{self._base_url}{path}"
        query = {key: str(val) for key, val in (params or {}).items() if val is not None}
        request_headers = dict(self._headers)
        if authenticated and self.auth is not None:
            request_headers.update(self.auth.headers)
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
                    return self._handle_response(path, url, response, body)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise ResourceTemporarilyUnavailable(
                f"Network error contacting iHeartRadio: {err}"
            ) from err
        if not authenticated:
            raise LoginFailed(f"iHeartRadio rejected the login: {_error_description(body)}")
        if not retry_auth or self.auth is None:
            raise LoginFailed("iHeartRadio no longer accepts the session")
        # the session has gone stale; open a new one and try once more
        await self.auth.relogin()
        return await self.request(
            method,
            path,
            params=params,
            json=json,
            form=form,
            headers=headers,
            retry_auth=False,
        )

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

    async def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """
        Perform a GET against the iHeartRadio API and return the decoded JSON body.

        :param path: The API path to request.
        :param params: Query parameters; entries with a None value are left out.
        """
        return await self.request("GET", path, params=params)

    async def _create_station(self, artist_id: str, now: float) -> ArtistRadioStation:
        """
        Register the artist radio station of an artist and start holding its batches.

        :param artist_id: The seed artist id.
        :param now: Current wall-clock time.
        """
        if self.auth is None:
            raise LoginFailed("Not signed in to iHeartRadio")
        payload = await self.request(
            "POST",
            PATH_ARTIST_STATION.format(profile_id=self.auth.profile_id, artist_id=artist_id),
            form={"playedFrom": str(PLAYED_FROM)},
        )
        station_id = payload.get("id") if isinstance(payload, dict) else None
        if not station_id:
            raise MediaNotFoundError(f"iHeartRadio has no radio station for artist {artist_id}")
        return self.stations.register(artist_id, str(station_id), now)

    def _handle_response(
        self, path: str, url: str, response: aiohttp.ClientResponse, body: bytes
    ) -> Any:
        """
        Turn an API response into its decoded body, or the error it stands for.

        :param path: The requested path, for error messages.
        :param url: The requested url, for the log message.
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
                f"iHeartRadio request to {path} failed ({response.status})"
            )
        if not body:
            return None
        try:
            return json_loads(body)
        except ValueError as err:
            raise InvalidDataError(f"iHeartRadio returned an invalid response for {path}") from err

    def _not_found_error(self, url: str, body: bytes) -> MusicAssistantError:
        """
        Return the error to raise for a request the API holds nothing for.

        :param url: The requested url, for the log message.
        :param body: The raw response body, holding the API's own reason.
        """
        reason = _error_description(body) or "not found"
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


def _error_payload(body: bytes) -> dict[str, Any] | None:
    """
    Return the error object of an API error body, if it carries one.

    A 400 carries either ``{"error": {...}}`` or ``{"errors": [{...}]}``, a 410 ``{"reason": [...]}``.
    """
    try:
        payload = json_loads(body)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    if isinstance(error := payload.get("error"), dict):
        return error
    if errors := _items(payload.get("errors")):
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
