"""DR LYD music provider for Music Assistant."""

from __future__ import annotations

from datetime import timedelta
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

import aiohttp
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import (
    MediaNotFoundError,
    ProviderUnavailableError,
    RateLimited,
    RetriesExhausted,
    UnplayableMediaError,
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
from yarl import URL

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
    BROWSE_RADIOS,
    BROWSE_RECENT,
    BROWSE_RECENT_COUNT,
    CATALOGUE_CACHE_SECONDS,
    CHANNELS_CACHE_SECONDS,
    CHANNELS_URL,
    EPISODES_URL,
    MAX_PAGES,
    NOW_PLAYING_CACHE_SECONDS,
    PAGE_SIZE,
    PODCAST_CACHE_SECONDS,
    PROGRAMMES_URL,
    SEARCH_URL,
    SERIES_URL,
    STREAM_DELAY_SECONDS,
    STREAM_METADATA_UPDATE_INTERVAL,
    TRACKS_URL,
)
from .helpers import get_audio, get_stream_metadata, search_channels
from .models import (
    Channel,
    DRChannel,
    DREpisode,
    DRProgramme,
    DRSeries,
    DRTrack,
    EpisodePage,
    Page,
    SeriesPage,
    TrackPage,
)
from .parsers import parse_channels, parse_podcast, parse_podcast_episode, parse_radio

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence

    from music_assistant_models.config_entries import ConfigEntry
    from music_assistant_models.media_items import MediaItemType, Radio


class DRLydProvider(MusicProvider):
    """Music provider for the live radio and the podcasts of DR LYD."""

    throttler: ThrottlerManager

    @property
    def max_concurrent_streams(self) -> None:
        """Allow unlimited concurrent upstream source streams."""
        return None

    @property
    def supported_media_types(self) -> set[MediaType]:
        """Return the media types this provider can serve."""
        # Without library support for radio and podcasts the base implementation would not
        # report them, which would keep the provider out of search-based lookups.
        return {MediaType.RADIO, MediaType.PODCAST}

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to setup this provider."""
        return (CONF_ENTRY_UNOFFICIAL_PROVIDER,)

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.throttler = ThrottlerManager(rate_limit=API_RATE_LIMIT, period=API_RATE_PERIOD)

    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 5
    ) -> SearchResults:
        """
        Search the podcasts and the radio channels of DR LYD.

        :param search_query: The words to find.
        :param media_types: The media types to search for.
        :param limit: The maximum number of items to return per media type.
        """
        results = SearchResults()
        if MediaType.PODCAST in media_types:
            url = URL(SEARCH_URL).with_query(q=search_query, limit=limit)
            page = SeriesPage.from_dict(await self._get_json(str(url)))
            results.podcasts = [
                parse_podcast(series, self.instance_id, self.domain)
                for series in page.items
                if series.is_available_on_demand
            ]
        if MediaType.RADIO in media_types:
            channels = search_channels(await self._get_channels(), search_query, limit)
            results.radio = [
                parse_radio(channel, self.instance_id, self.domain) for channel in channels
            ]
        return results

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Browse the podcasts by latest episode or by title, and the live radio channels.

        :param path: The path to browse, (e.g. dr_lyd://radios).
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
        if subpath not in BROWSE_FOLDERS:
            raise KeyError("Invalid subpath")
        catalogue = await self._get_catalogue()
        if subpath == BROWSE_RECENT:
            return catalogue[:BROWSE_RECENT_COUNT]
        return sorted(catalogue, key=lambda podcast: podcast.name.casefold())

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """
        Get radio details by id.

        :param prov_radio_id: The slug of the channel.
        """
        return parse_radio(await self._get_channel(prov_radio_id), self.instance_id, self.domain)

    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """
        Get podcast details by id.

        :param prov_podcast_id: The production number of the podcast at DR.
        """
        return await self._get_series(prov_podcast_id)

    async def get_podcast_episodes(self, prov_podcast_id: str) -> AsyncGenerator[PodcastEpisode]:
        """
        Get all episodes of a podcast.

        :param prov_podcast_id: The production number of the podcast at DR.
        """
        for episode in await self._get_episodes(prov_podcast_id):
            yield episode

    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """
        Get a single podcast episode.

        :param prov_episode_id: The production number of the episode at DR.
        """
        episode = await self._get_episode(prov_episode_id)
        podcast = await self._get_series(episode.series.production_number)
        return parse_podcast_episode(
            episode, ItemMapping.from_item(podcast), 0, self.instance_id, self.domain
        )

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """
        Get the stream details of a radio channel or a podcast episode.

        :param item_id: The slug of the channel or the production number of the episode.
        :param media_type: The media type of the item.
        """
        if media_type == MediaType.RADIO:
            return self._get_radio_stream_details(await self._get_channel(item_id))
        if media_type != MediaType.PODCAST_EPISODE:
            raise UnplayableMediaError(f"Unsupported media type: {media_type}")
        episode = await self._get_episode(item_id)
        if (audio := get_audio(episode.audio_assets)) is None:
            raise MediaNotFoundError(f"DR has no audio for episode {item_id}")
        hls = audio.format == "HLS"
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(content_type=ContentType.AAC if hls else ContentType.MP3),
            media_type=MediaType.PODCAST_EPISODE,
            stream_type=StreamType.HLS if hls else StreamType.HTTP,
            path=audio.url,
            can_seek=True,
            allow_seek=True,
        )

    async def _update_stream_metadata(
        self, stream_details: StreamDetails, elapsed_time: int
    ) -> None:
        """Update the stream metadata with what DR is playing."""
        try:
            channel = await self._get_channel(stream_details.item_id)
            tracks = await self._get_tracks(channel.urn)
            programmes = await self._get_programmes()
        except (MediaNotFoundError, ProviderUnavailableError, RateLimited, RetriesExhausted) as err:
            self.logger.debug("Could not update what plays on %s: %s", stream_details.item_id, err)
            return
        at = utc() - timedelta(seconds=STREAM_DELAY_SECONDS)
        stream_details.stream_metadata = get_stream_metadata(channel, tracks, programmes, at)

    async def _get_channel(self, slug: str) -> Channel:
        """Return one channel, or raise when DR does not list it."""
        channel = next((item for item in await self._get_channels() if item.slug == slug), None)
        if channel is None:
            raise MediaNotFoundError(f"Unknown DR radio channel: {slug}")
        return channel

    @use_cache(CHANNELS_CACHE_SECONDS, base_class=Channel, allow_expired_cache=True)
    async def _get_channels(self) -> list[Channel]:
        """Return the live radio channels of DR."""
        documents = [DRChannel.from_dict(item) for item in await self._get_json(CHANNELS_URL)]
        channels = parse_channels(documents)
        # raising keeps a bad answer out of the cache, where it would replace a working list
        if not channels:
            raise ProviderUnavailableError("DR lists no radio channels")
        return channels

    @use_cache(CATALOGUE_CACHE_SECONDS, base_class=Podcast, allow_expired_cache=True)
    async def _get_catalogue(self) -> list[Podcast]:
        """Return all the podcasts of DR, the one with the newest episode first."""
        series: dict[str, DRSeries] = {}
        first = SeriesPage.from_dict(await self._get_json(f"{SERIES_URL}?limit={PAGE_SIZE}"))
        async for page in self._get_pages(first):
            for item in page.items:
                if item.is_available_on_demand:
                    series.setdefault(item.production_number, item)
        if not series:
            raise ProviderUnavailableError("DR lists no podcasts")
        newest = sorted(series.values(), key=lambda item: item.latest_episode_start_time)
        return [parse_podcast(item, self.instance_id, self.domain) for item in reversed(newest)]

    @use_cache(PODCAST_CACHE_SECONDS, base_class=Podcast)
    async def _get_series(self, series_id: str) -> Podcast:
        """Return one podcast, or raise when DR has none with the id."""
        series = DRSeries.from_dict(await self._get_json(f"{SERIES_URL}/{series_id}"))
        if not series.is_available_on_demand:
            raise MediaNotFoundError(f"DR has no podcast {series_id} to listen to")
        return parse_podcast(series, self.instance_id, self.domain)

    @use_cache(PODCAST_CACHE_SECONDS, base_class=PodcastEpisode)
    async def _get_episodes(self, series_id: str) -> list[PodcastEpisode]:
        """Return the episodes of a podcast that can be played."""
        podcast = ItemMapping.from_item(await self._get_series(series_id))
        groups = await self._get_json(f"{SERIES_URL}/{series_id}/episodes/groups?limit={PAGE_SIZE}")
        episodes: dict[str, DREpisode] = {}
        for group in groups:
            async for page in self._get_pages(EpisodePage.from_dict(group)):
                for item in page.items:
                    if item.is_available_on_demand:
                        episodes.setdefault(item.production_number, item)
        positions = rank_episodes_by_date([item.start_time for item in episodes.values()])
        return [
            parse_podcast_episode(item, podcast, position, self.instance_id, self.domain)
            for position, item in zip(positions, episodes.values(), strict=True)
        ]

    async def _get_episode(self, episode_id: str) -> DREpisode:
        """Return one episode, or raise when DR has none with the id."""
        # not cached, so that what is played is what DR serves now
        return DREpisode.from_dict(await self._get_json(f"{EPISODES_URL}/{episode_id}"))

    @use_cache(NOW_PLAYING_CACHE_SECONDS, base_class=DRTrack)
    async def _get_tracks(self, channel_urn: str) -> list[DRTrack]:
        """Return the tracks that DR has played on a channel."""
        return TrackPage.from_dict(await self._get_json(f"{TRACKS_URL}/{channel_urn}")).items

    @use_cache(NOW_PLAYING_CACHE_SECONDS, base_class=DRProgramme)
    async def _get_programmes(self) -> list[DRProgramme]:
        """Return the programmes on air now, one for every channel."""
        return [DRProgramme.from_dict(item) for item in await self._get_json(PROGRAMMES_URL)]

    async def _get_pages[PageT: Page[Any]](self, first: PageT) -> AsyncGenerator[PageT]:
        """Yield a page of a list and then the pages that follow it."""
        page = first
        for number in range(1, MAX_PAGES + 1):
            yield page
            if not page.next or not page.items or number == MAX_PAGES:
                return
            page = type(page).from_dict(await self._get_json(page.next))

    @throttle_with_retries
    async def _get_json(self, url: str) -> Any:
        """Return the JSON document at a url of the radio API of DR."""
        try:
            async with self.mass.http_session.get(
                url, headers=API_HEADERS, timeout=API_TIMEOUT
            ) as response:
                if response.status == HTTPStatus.NOT_FOUND:
                    raise MediaNotFoundError(f"DR has nothing at {url}")
                if response.status == HTTPStatus.TOO_MANY_REQUESTS:
                    raise RateLimited(
                        f"DR is rate limiting {url}",
                        backoff_time=parse_retry_after(response.headers.get("Retry-After")),
                    )
                response.raise_for_status()
                return await response.json()
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise ProviderUnavailableError(
                f"Could not load {url}: {str(err) or type(err).__name__}"
            ) from err

    def _get_radio_stream_details(self, channel: Channel) -> StreamDetails:
        """Return the stream details of a channel."""
        if channel.hls:
            stream_type, content_type = StreamType.HLS, ContentType.AAC
        else:
            stream_type, content_type = StreamType.ICY, ContentType.try_parse(channel.stream_url)
        return StreamDetails(
            provider=self.instance_id,
            item_id=channel.slug,
            audio_format=AudioFormat(content_type=content_type, channels=2),
            media_type=MediaType.RADIO,
            stream_type=stream_type,
            path=channel.stream_url,
            can_seek=False,
            allow_seek=False,
            stream_metadata_update_callback=self._update_stream_metadata,
            stream_metadata_update_interval=STREAM_METADATA_UPDATE_INTERVAL,
        )
