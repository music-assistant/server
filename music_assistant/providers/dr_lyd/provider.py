"""DR LYD music provider for Music Assistant."""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING, Any

import aiohttp
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import (
    MediaNotFoundError,
    MusicAssistantError,
    ProviderUnavailableError,
    RateLimited,
    UnplayableMediaError,
)
from music_assistant_models.media_items import AudioFormat, BrowseFolder, SearchResults
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata

from music_assistant.constants import CONF_ENTRY_UNOFFICIAL_PROVIDER
from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.datetime import utc_timestamp
from music_assistant.helpers.podcast_parsers import rank_episodes_by_date
from music_assistant.helpers.throttle_retry import Throttler, parse_retry_after
from music_assistant.models.music_provider import MusicProvider

from .constants import (
    API_HEADERS,
    API_RATE_LIMIT,
    API_RATE_PERIOD,
    API_TIMEOUT,
    API_URL,
    BROWSE_FOLDERS,
    BROWSE_RADIO,
    BROWSE_RECENT,
    BROWSE_RECENT_COUNT,
    CATALOGUE_CACHE_SECONDS,
    CHANNELS_CACHE_SECONDS,
    CHANNELS_URL,
    CONF_ENTRY_LIBRARY_SYNC_PODCASTS_HIDDEN,
    EPISODES_URL,
    MAX_PAGES,
    NOW_PLAYING_CACHE_SECONDS,
    PAGE_SIZE,
    PODCAST_CACHE_SECONDS,
    PROGRAMMES_URL,
    SERIES_URL,
    STREAM_DELAY_SECONDS,
    STREAM_METADATA_UPDATE_INTERVAL,
    TRACKS_URL,
)
from .helpers import (
    get_items,
    get_now_playing,
    recently_updated,
    require_id,
    search_channels,
    search_series,
)
from .models import Channel, Episode, Programme, Series, Track
from .parsers import (
    parse_channels,
    parse_episode,
    parse_podcast,
    parse_podcast_episode,
    parse_podcast_reference,
    parse_programmes,
    parse_radio,
    parse_series,
    parse_tracks,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence

    from music_assistant_models.config_entries import ConfigEntry
    from music_assistant_models.media_items import (
        ItemMapping,
        MediaItemType,
        Podcast,
        PodcastEpisode,
        Radio,
    )


class DRLydProvider(MusicProvider):
    """Music provider for the live radio and the podcasts of DR LYD."""

    _throttler: Throttler

    @property
    def max_concurrent_streams(self) -> None:
        """Allow unlimited concurrent upstream source streams."""
        return None

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        return True

    @property
    def supported_media_types(self) -> set[MediaType]:
        """Return the media types this provider can serve."""
        # Without library support for radio the base implementation would not report it, which
        # would keep the provider out of search-based lookups.
        return {MediaType.RADIO, MediaType.PODCAST}

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to setup this provider."""
        return (CONF_ENTRY_UNOFFICIAL_PROVIDER, CONF_ENTRY_LIBRARY_SYNC_PODCASTS_HIDDEN)

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._throttler = Throttler(rate_limit=API_RATE_LIMIT, period=API_RATE_PERIOD)

    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 5
    ) -> SearchResults:
        """
        Search the title and description of every podcast and the name of every channel.

        :param search_query: The words to find.
        :param media_types: The media types to search for.
        :param limit: The maximum number of items to return per media type.
        """
        results = SearchResults()
        if MediaType.PODCAST in media_types:
            series = search_series(await self._get_catalogue(), search_query, limit)
            results.podcasts = [
                parse_podcast(entry, self.instance_id, self.domain) for entry in series
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

        :param path: The path to browse, (e.g. dr_lyd://live_radio).
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
        if subpath == BROWSE_RADIO:
            return [
                parse_radio(channel, self.instance_id, self.domain)
                for channel in await self._get_channels()
            ]
        if subpath not in BROWSE_FOLDERS:
            raise KeyError("Invalid subpath")
        catalogue = await self._get_catalogue()
        if subpath == BROWSE_RECENT:
            series = recently_updated(catalogue, BROWSE_RECENT_COUNT)
        else:
            series = sorted(catalogue, key=lambda entry: entry.title.casefold())
        return [parse_podcast(entry, self.instance_id, self.domain) for entry in series]

    async def get_library_podcasts(self) -> AsyncGenerator[Podcast]:
        """
        Get library podcasts.

        The provider has no library of its own, so this brings up to date the podcasts that
        were added to the Music Assistant library from search or browse. The item_id is the id
        of the podcast at DR. A podcast that DR no longer has is left out, which removes it
        from the library.
        """
        async for podcast in self.mass.music.podcasts.iter_library_items_by_prov_id(
            self.instance_id
        ):
            for mapping in podcast.provider_mappings:
                if mapping.provider_instance != self.instance_id or not mapping.in_library:
                    continue
                try:
                    series = await self._get_series(mapping.item_id)
                except MediaNotFoundError as err:
                    self.logger.warning(
                        "Leaving podcast %s out of the library: %s", mapping.item_id, err
                    )
                    continue
                except ProviderUnavailableError as err:
                    self.report_skipped_sync_item(MediaType.PODCAST, mapping.item_id, err)
                    continue
                yield parse_podcast(series, self.instance_id, self.domain)

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
        return parse_podcast(await self._get_series(prov_podcast_id), self.instance_id, self.domain)

    async def get_podcast_episodes(self, prov_podcast_id: str) -> AsyncGenerator[PodcastEpisode]:
        """
        Get all episodes of a podcast.

        :param prov_podcast_id: The production number of the podcast at DR.
        """
        series = await self._get_series(prov_podcast_id)
        episodes = await self._get_episodes(prov_podcast_id)
        podcast = parse_podcast_reference(series.id, series.title, self.instance_id)
        positions = rank_episodes_by_date([episode.published for episode in episodes])
        for position, episode in zip(positions, episodes, strict=True):
            yield parse_podcast_episode(
                episode, podcast, series.image_url, position, self.instance_id, self.domain
            )

    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """
        Get a single episode, in the podcast DR names for it and at position 0.

        :param prov_episode_id: The production number of the episode at DR.
        """
        episode = await self._get_episode(prov_episode_id)
        if episode.series_id is None:
            raise MediaNotFoundError(f"DR names no podcast for episode {prov_episode_id}")
        podcast = parse_podcast_reference(
            episode.series_id, episode.series_title or episode.title, self.instance_id
        )
        return parse_podcast_episode(episode, podcast, None, 0, self.instance_id, self.domain)

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
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(
                content_type=ContentType.AAC if episode.hls else ContentType.MP3
            ),
            media_type=MediaType.PODCAST_EPISODE,
            stream_type=StreamType.HLS if episode.hls else StreamType.HTTP,
            path=episode.audio_url,
            can_seek=True,
            allow_seek=True,
        )

    async def _update_stream_metadata(
        self, stream_details: StreamDetails, elapsed_time: int
    ) -> None:
        """Show what DR plays on the channel, the player queue controller calls this."""
        try:
            channel = await self._get_channel(stream_details.item_id)
            tracks = await self._get_tracks(channel.urn)
            programmes = await self._get_programmes()
        except MusicAssistantError as err:
            self.logger.debug("Could not update what plays on %s: %s", stream_details.item_id, err)
            return
        playing = get_now_playing(
            channel, tracks, programmes, utc_timestamp() - STREAM_DELAY_SECONDS
        )
        stream_details.stream_metadata = StreamMetadata(
            title=playing.title, artist=playing.artist, image_url=playing.image_url
        )

    async def _get_channel(self, slug: str) -> Channel:
        """Return one channel, or raise when DR does not list it."""
        channel = next((item for item in await self._get_channels() if item.slug == slug), None)
        if channel is None:
            raise MediaNotFoundError(f"Unknown DR radio channel: {slug}")
        return channel

    @use_cache(CHANNELS_CACHE_SECONDS, base_class=Channel, allow_expired_cache=True)
    async def _get_channels(self) -> list[Channel]:
        """Return the live radio channels of DR."""
        channels = parse_channels(await self._get_json(CHANNELS_URL))
        # raising keeps a bad answer out of the cache, where it would replace a working list
        if not channels:
            raise ProviderUnavailableError("DR lists no radio channels")
        return channels

    @use_cache(CATALOGUE_CACHE_SECONDS, base_class=Series, allow_expired_cache=True)
    async def _get_catalogue(self) -> list[Series]:
        """Return all the podcasts of DR."""
        podcasts: dict[str, Series] = {}
        first = await self._get_json(f"{SERIES_URL}?limit={PAGE_SIZE}")
        async for page in self._get_pages(first):
            for series in map(parse_series, get_items(page)):
                if series:
                    podcasts.setdefault(series.id, series)
        if not podcasts:
            raise ProviderUnavailableError("DR lists no podcasts")
        return list(podcasts.values())

    @use_cache(PODCAST_CACHE_SECONDS, base_class=Series)
    async def _get_series(self, series_id: str) -> Series:
        """Return one podcast, or raise when DR has none with the id."""
        series = parse_series(
            await self._get_json(f"{SERIES_URL}/{require_id(series_id, 'podcast')}")
        )
        if series is None:
            raise MediaNotFoundError(f"DR has no podcast {series_id} to listen to")
        return series

    @use_cache(PODCAST_CACHE_SECONDS, base_class=Episode)
    async def _get_episodes(self, series_id: str) -> list[Episode]:
        """Return the episodes of a podcast that can be played."""
        groups = await self._get_json(
            f"{SERIES_URL}/{require_id(series_id, 'podcast')}/episodes/groups?limit={PAGE_SIZE}"
        )
        if not isinstance(groups, list):
            raise ProviderUnavailableError(f"DR sent no episode groups for podcast {series_id}")
        episodes: dict[str, Episode] = {}
        for group in groups:
            async for page in self._get_pages(group):
                for episode in map(parse_episode, get_items(page)):
                    if episode:
                        episodes.setdefault(episode.id, episode)
        return list(episodes.values())

    async def _get_episode(self, episode_id: str) -> Episode:
        """
        Return one episode, or raise when DR has none with the id or no audio for it.

        It is read from DR on every call, so that what is played is what DR serves now.
        """
        episode = parse_episode(
            await self._get_json(f"{EPISODES_URL}/{require_id(episode_id, 'episode')}")
        )
        if episode is None:
            raise MediaNotFoundError(f"DR has no audio for episode {episode_id}")
        return episode

    @use_cache(NOW_PLAYING_CACHE_SECONDS, base_class=Track)
    async def _get_tracks(self, channel_urn: str) -> list[Track]:
        """Return the tracks that DR has played on a channel."""
        return parse_tracks(await self._get_json(f"{TRACKS_URL}/{channel_urn}"))

    @use_cache(NOW_PLAYING_CACHE_SECONDS, base_class=Programme)
    async def _get_programmes(self) -> list[Programme]:
        """Return the programmes on air now, one for every channel."""
        return parse_programmes(await self._get_json(PROGRAMMES_URL))

    async def _get_pages(self, first: Any) -> AsyncGenerator[Any]:
        """Yield a page of a list and then the pages that follow it, up to MAX_PAGES."""
        page = first
        for number in range(1, MAX_PAGES + 1):
            yield page
            next_url = page.get("next") if isinstance(page, dict) else None
            if not next_url or not get_items(page) or number == MAX_PAGES:
                return
            # the API key must not be sent to another host
            if not isinstance(next_url, str) or not next_url.startswith(f"{API_URL}/"):
                raise ProviderUnavailableError(f"DR sent a page that is not on its API: {next_url}")
            page = await self._get_json(next_url)

    async def _get_json(self, url: str) -> Any:
        """Return the JSON document at a url of the radio API of DR."""
        try:
            async with (
                self._throttler,
                self.mass.http_session.get(
                    url, headers=API_HEADERS, timeout=API_TIMEOUT
                ) as response,
            ):
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
