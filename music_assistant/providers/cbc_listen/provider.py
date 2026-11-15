"""CBC Listen music provider for Music Assistant."""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING, Any

import aiohttp
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import (
    MediaNotFoundError,
    ProviderUnavailableError,
    RateLimited,
    ResourceTemporarilyUnavailable,
    RetriesExhausted,
)
from music_assistant_models.media_items import (
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    Podcast,
    PodcastEpisode,
    SearchResults,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import CONF_ENTRY_UNOFFICIAL_PROVIDER
from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.datetime import utc
from music_assistant.helpers.podcast_parsers import rank_episodes_by_date
from music_assistant.helpers.throttle_retry import (
    ThrottlerManager,
    parse_retry_after,
    throttle_with_retries,
)
from music_assistant.models.music_provider import MusicProvider

from .constants import (
    API_HEADERS,
    API_RATE_LIMIT,
    API_RATE_PERIOD,
    API_TIMEOUT,
    BROWSE_FOLDERS,
    BROWSE_PODCASTS,
    BROWSE_RADIO_SHOWS,
    BROWSE_RADIOS,
    CATALOGUE_CACHE_SECONDS,
    CHANNELS_CACHE_SECONDS,
    CLIPS_URL,
    DEFAULT_IMAGE_URL,
    FREE_TIER,
    IMAGE_HEADERS,
    MAX_PAGES,
    NOW_PLAYING_CACHE_SECONDS,
    PAGE_SIZE,
    PODCAST_CACHE_SECONDS,
    PROGRAM_QUEUE_URL,
    SHOW_URL,
    SHOWS_URL,
    STATIONS_URL,
    STREAM_METADATA_UPDATE_INTERVAL,
)
from .helpers import find_program, get_stream_metadata, search_channels, search_shows
from .models import (
    CBCClip,
    CBCClipPage,
    CBCNetwork,
    CBCPlaylogPage,
    CBCProgram,
    CBCShow,
    CBCTrack,
    Channel,
)
from .parsers import parse_channels, parse_podcast, parse_podcast_episode, parse_radio

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence

    from music_assistant_models.config_entries import ConfigEntry
    from music_assistant_models.media_items import MediaItemType, Radio


class CBCListenProvider(MusicProvider):
    """Music provider for live radio, podcasts, and radio shows from CBC Listen."""

    throttler: ThrottlerManager
    _playlog_dates: dict[tuple[int, str, int, int], int | None]

    @property
    def max_concurrent_streams(self) -> None:
        """Allow unlimited concurrent upstream source streams."""
        return None

    @property
    def supported_media_types(self) -> set[MediaType]:
        """Return the media types this provider can serve."""
        return {MediaType.RADIO, MediaType.PODCAST}

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to setup this provider."""
        return (CONF_ENTRY_UNOFFICIAL_PROVIDER,)

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.throttler = ThrottlerManager(rate_limit=API_RATE_LIMIT, period=API_RATE_PERIOD)
        self._playlog_dates = {}

    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 5
    ) -> SearchResults:
        """
        Search CBC Listen live radio stations, podcasts, and radio shows.

        :param search_query: Search query string.
        :param media_types: Media types to search for.
        :param limit: Maximum number of items to return per media type.
        """
        results = SearchResults()
        if MediaType.PODCAST in media_types:
            shows = search_shows(await self._get_shows(), search_query, limit)
            results.podcasts = [
                parse_podcast(show, self.instance_id, self.domain) for show in shows
            ]
        if MediaType.RADIO in media_types:
            channels = search_channels(await self._get_channels(), search_query, limit)
            results.radio = [
                parse_radio(channel, self.instance_id, self.domain) for channel in channels
            ]
        return results

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Browse live radio stations, podcasts, and on-demand radio shows.

        :param path: The path to browse (e.g. cbc_listen://radios).
        """
        subpath = path.split("://", 1)[1].strip("/")
        if not subpath:
            return [
                BrowseFolder(
                    item_id=folder_id,
                    provider=self.instance_id,
                    path=f"{self.instance_id}://{folder_id}",
                    name=name,
                    translation_key=folder_id,
                )
                for folder_id, name in BROWSE_FOLDERS.items()
            ]
        if subpath == BROWSE_RADIOS:
            return [
                parse_radio(channel, self.instance_id, self.domain)
                for channel in await self._get_channels()
            ]
        if subpath not in (BROWSE_PODCASTS, BROWSE_RADIO_SHOWS):
            raise KeyError(f"Invalid browse subpath: {subpath}")
        want_podcast = subpath == BROWSE_PODCASTS
        shows = [
            show
            for show in await self._get_shows()
            if show.has_audio and show.original_podcast == want_podcast
        ]
        shows.sort(key=lambda show: show.title.casefold())
        return [parse_podcast(show, self.instance_id, self.domain) for show in shows]

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """
        Get radio station details by ID.

        :param prov_radio_id: Station ID at CBC Listen.
        """
        return parse_radio(await self._get_channel(prov_radio_id), self.instance_id, self.domain)

    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """
        Get podcast or radio show details by ID.

        :param prov_podcast_id: Show ID at CBC Listen.
        """
        return await self._get_series(prov_podcast_id)

    async def get_podcast_episodes(self, prov_podcast_id: str) -> AsyncGenerator[PodcastEpisode]:
        """
        Yield all playable episodes or segments for a podcast or radio show.

        :param prov_podcast_id: Show ID at CBC Listen.
        """
        for episode in await self._get_episodes(prov_podcast_id):
            yield episode

    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """
        Get a single podcast episode or radio show clip by ID.

        :param prov_episode_id: Clip ID at CBC Listen.
        """
        clip = await self._get_clip(prov_episode_id)
        podcast = await self._get_series(str(clip.show_id))
        return parse_podcast_episode(
            clip, ItemMapping.from_item(podcast), 0, self.instance_id, self.domain
        )

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """
        Get stream details for a live radio station or podcast/show episode.

        :param item_id: Station ID or clip ID at CBC Listen.
        :param media_type: Media type of the item.
        """
        if media_type == MediaType.RADIO:
            channel = await self._get_channel(item_id)
            return StreamDetails(
                provider=self.instance_id,
                item_id=channel.station_id,
                audio_format=AudioFormat(content_type=ContentType.AAC, channels=2),
                media_type=MediaType.RADIO,
                stream_type=StreamType.HLS,
                path=channel.stream_url,
                can_seek=False,
                allow_seek=False,
                stream_metadata_update_callback=self._update_stream_metadata,
                stream_metadata_update_interval=STREAM_METADATA_UPDATE_INTERVAL,
            )
        clip = await self._get_clip(item_id)
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(content_type=ContentType.MP3),
            media_type=MediaType.PODCAST_EPISODE,
            stream_type=StreamType.HTTP,
            path=clip.src,
            can_seek=True,
            allow_seek=True,
        )

    async def resolve_image(self, path: str) -> str | bytes | None:
        """
        Fetch an image from CBC Listen or Radio-Canada using the provider User-Agent.

        :param path: Image URL to fetch.
        """
        # CBC's Akamai WAF drops image proxy requests whose User-Agent contains a URL.
        if (image := await self._fetch_image(path)) is not None or path == DEFAULT_IMAGE_URL:
            return image
        return await self._fetch_image(DEFAULT_IMAGE_URL)

    async def _fetch_image(self, url: str) -> bytes | None:
        """Return raw image bytes for ``url``, or ``None`` on a 4xx/5xx HTTP status."""
        try:
            async with self.mass.http_session.get(
                url, headers=IMAGE_HEADERS, timeout=API_TIMEOUT
            ) as response:
                if response.status >= HTTPStatus.BAD_REQUEST:
                    return None
                return await response.read()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise MediaNotFoundError(f"Could not load image {url}: {err}") from err

    async def _update_stream_metadata(
        self, stream_details: StreamDetails, elapsed_time: int
    ) -> None:
        """Update live stream metadata with the current program and track on air."""
        try:
            channel = await self._get_channel(stream_details.item_id)
            programs = (
                await self._get_programs(channel.network_id, channel.location_key)
                if channel.location_key
                else []
            )
            at_ms = int(utc().timestamp() * 1000)
            program = find_program(programs, at_ms)
            tracks = (
                await self._get_program_tracks(program, channel.location_key, at_ms)
                if program and channel.location_key
                else []
            )
        except (MediaNotFoundError, ProviderUnavailableError, RetriesExhausted) as err:
            self.logger.debug(
                "Could not update stream metadata for %s: %s", stream_details.item_id, err
            )
            return
        stream_details.stream_metadata = get_stream_metadata(channel, programs, tracks, at_ms)

    async def _get_channel(self, station_id: str) -> Channel:
        """Return one live radio channel by station ID, or raise MediaNotFoundError."""
        channel = next(
            (item for item in await self._get_channels() if item.station_id == station_id),
            None,
        )
        if channel is None:
            raise MediaNotFoundError(f"Unknown CBC Listen radio station: {station_id}")
        return channel

    @use_cache(CHANNELS_CACHE_SECONDS, base_class=Channel, allow_expired_cache=True)
    async def _get_channels(self) -> list[Channel]:
        """Return all live radio channels from CBC Listen."""
        data = await self._get_data(STATIONS_URL)
        networks = [CBCNetwork.from_dict(item) for item in data]
        channels = parse_channels(networks)
        if not channels:
            raise ProviderUnavailableError("CBC Listen returned no live radio stations")
        return channels

    @use_cache(CATALOGUE_CACHE_SECONDS, base_class=CBCShow, allow_expired_cache=True)
    async def _get_shows(self) -> list[CBCShow]:
        """Return the full catalogue of podcasts and radio shows from CBC Listen."""
        data = await self._get_data(SHOWS_URL)
        shows = [CBCShow.from_dict(item) for item in data]
        if not shows:
            raise ProviderUnavailableError("CBC Listen returned no shows")
        return shows

    @use_cache(PODCAST_CACHE_SECONDS, base_class=Podcast)
    async def _get_series(self, show_id: str) -> Podcast:
        """Return one podcast or radio show by ID."""
        show = CBCShow.from_dict(await self._get_data(f"{SHOW_URL}/{show_id}"))
        if not show.has_audio:
            raise MediaNotFoundError(f"CBC Listen show {show_id} has no playable episodes")
        return parse_podcast(show, self.instance_id, self.domain)

    @use_cache(PODCAST_CACHE_SECONDS, base_class=PodcastEpisode)
    async def _get_episodes(self, show_id: str) -> list[PodcastEpisode]:
        """Return the playable episodes or segments of a podcast or radio show."""
        podcast = ItemMapping.from_item(await self._get_series(show_id))
        clips = [
            clip
            for clip in await self._get_clips(show_id)
            if clip.src and clip.content_tier == FREE_TIER
        ]
        positions = rank_episodes_by_date([clip.released_at or None for clip in clips])
        return [
            parse_podcast_episode(clip, podcast, position, self.instance_id, self.domain)
            for position, clip in zip(positions, clips, strict=True)
        ]

    async def _get_clips(self, show_id: str) -> list[CBCClip]:
        """Fetch all clip pages for a show up to ``MAX_PAGES``."""
        clips: list[CBCClip] = []
        for page_number in range(1, MAX_PAGES + 1):
            url = f"{SHOW_URL}/{show_id}/clips?pageNumber={page_number}&pageSize={PAGE_SIZE}"
            page = CBCClipPage.from_dict(await self._get_data(url))
            clips.extend(page.clips)
            if len(page.clips) < PAGE_SIZE or (page.clip_count and len(clips) >= page.clip_count):
                break
        return clips

    async def _get_clip(self, clip_id: str) -> CBCClip:
        """Return one playable clip by ID, or raise MediaNotFoundError."""
        # Not cached so that the stream URL is always fetched fresh on playback.
        clip = CBCClip.from_dict(await self._get_data(f"{CLIPS_URL}/{clip_id}"))
        if not clip.src or clip.content_tier != FREE_TIER:
            raise MediaNotFoundError(f"CBC Listen has no playable audio for clip {clip_id}")
        return clip

    @use_cache(NOW_PLAYING_CACHE_SECONDS, base_class=CBCProgram)
    async def _get_programs(self, network_id: int, location_key: str) -> list[CBCProgram]:
        """Return the scheduled programs for a network and location."""
        data = await self._get_data(f"{PROGRAM_QUEUE_URL}/{network_id}/{location_key}")
        return [CBCProgram.from_dict(item) for item in data]

    async def _get_program_tracks(
        self, program: CBCProgram, location_key: str, at_ms: int
    ) -> list[CBCTrack]:
        """Return the playlog tracks for a scheduled program slot."""
        if not isinstance(program.show_id, int):
            return []
        key = (program.show_id, location_key, program.epoch_start, program.epoch_end)
        if key in self._playlog_dates:
            date_id = self._playlog_dates[key]
            return (
                await self._get_tracks(program.show_id, date_id, location_key)
                if date_id is not None
                else []
            )
        self._playlog_dates = {k: v for k, v in self._playlog_dates.items() if k[3] > at_ms}
        date_id = await self._get_playlog_date(program.show_id, location_key)
        if date_id is None:
            self._playlog_dates[key] = None
            return []
        tracks = await self._get_tracks(program.show_id, date_id, location_key)
        # Only cache the date once a track from this broadcast slot has been logged,
        # since right as a show starts CBC still lists the previous broadcast's date.
        if any(track.broadcasted_time >= program.epoch_start for track in tracks):
            self._playlog_dates[key] = date_id
        return tracks

    @use_cache(NOW_PLAYING_CACHE_SECONDS)
    async def _get_playlog_date(self, show_id: int, location_key: str) -> int | None:
        """Return the latest playlog date ID for a show and location, or ``None`` if none exist."""
        url = f"{SHOW_URL}/{show_id}/playlogs?pageNumber=1&locationKey={location_key}&pageSize=1"
        page = CBCPlaylogPage.from_dict(await self._get_data(url))
        return page.episodes[0].episode_date_id if page.episodes else None

    @use_cache(NOW_PLAYING_CACHE_SECONDS, base_class=CBCTrack)
    async def _get_tracks(self, show_id: int, date_id: int, location_key: str) -> list[CBCTrack]:
        """Return the daily playlog tracks for a show and location."""
        url = f"{SHOW_URL}/{show_id}/playlogs/day/{date_id}?locationKey={location_key}"
        data = await self._get_data(url)
        return [CBCTrack.from_dict(item) for item in data]

    @throttle_with_retries
    async def _get_data(self, url: str) -> Any:
        """Return the data payload at a URL of the CBC Listen API."""
        try:
            async with self.mass.http_session.get(
                url, headers=API_HEADERS, timeout=API_TIMEOUT
            ) as response:
                if response.status in (HTTPStatus.NOT_FOUND, HTTPStatus.BAD_REQUEST):
                    raise MediaNotFoundError(f"CBC Listen has nothing at {url}")
                if response.status == HTTPStatus.TOO_MANY_REQUESTS:
                    raise RateLimited(
                        f"CBC Listen is rate limiting {url}",
                        backoff_time=parse_retry_after(response.headers.get("Retry-After")),
                    )
                if response.status >= HTTPStatus.INTERNAL_SERVER_ERROR:
                    raise ResourceTemporarilyUnavailable(
                        f"CBC Listen server error {response.status} at {url}",
                        backoff_time=parse_retry_after(response.headers.get("Retry-After")),
                    )
                if response.status >= HTTPStatus.BAD_REQUEST:
                    raise ProviderUnavailableError(
                        f"CBC Listen rejected {url} with status {response.status}"
                    )
                payload = await response.json()
        except (aiohttp.ContentTypeError, ValueError) as err:
            raise ProviderUnavailableError(f"Could not parse {url}: {err!r}") from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise ResourceTemporarilyUnavailable(f"Could not load {url}: {err!r}") from err
        if not isinstance(payload, dict) or "data" not in payload or payload["data"] is None:
            raise ProviderUnavailableError(f"Unexpected response payload from {url}")
        return payload["data"]
