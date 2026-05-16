"""
Podcast RSS Feed Music Provider for Music Assistant.

A URL to a podcast feed can be configured. The contents of that specific podcast
feed will be forwarded to music assistant. In order to have multiple podcast feeds,
multiple instances with each one feed must exist.

"""

from __future__ import annotations

import socket
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

import podcastparser
from aiohttp import ClientSession, ClientTimeout, TCPConnector
from aiohttp.client_exceptions import ClientError
from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import InvalidDataError, InvalidProviderURI, MediaNotFoundError
from music_assistant_models.media_items import (
    AudioFormat,
    MediaItemImage,
    Podcast,
    PodcastEpisode,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.compare import create_safe_string
from music_assistant.helpers.podcast_parsers import (
    get_podcastparser_dict,
    parse_podcast,
    parse_podcast_episode,
)
from music_assistant.helpers.tags import AudioTags, async_parse_tags
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

CONF_FEED_URL = "feed_url"

CACHE_CATEGORY_PODCASTS = 0
CACHE_CATEGORY_MEDIA_INFO = 1
PODCAST_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Connection": "close",
}

SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.LIBRARY_PODCASTS,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    if not config.get_value(CONF_FEED_URL):
        msg = "No podcast feed set"
        raise InvalidProviderURI(msg)
    return PodcastMusicprovider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    # ruff: noqa: ARG001
    return (
        ConfigEntry(
            key=CONF_FEED_URL,
            type=ConfigEntryType.STRING,
            label="RSS Feed URL",
            required=True,
        ),
    )


class PodcastMusicprovider(MusicProvider):
    """Podcast RSS Feed Music Provider."""

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.feed_url = podcastparser.normalize_feed_url(str(self.config.get_value(CONF_FEED_URL)))
        if self.feed_url is None:
            raise MediaNotFoundError("The specified feed url cannot be used.")

        self.podcast_id = create_safe_string(self.feed_url.replace("http", ""))

        try:
            self.parsed_podcast: dict[str, Any] = await self._cache_get_podcast()
        except ClientError as exc:
            raise MediaNotFoundError("Invalid URL") from exc

    @property
    def is_streaming_provider(self) -> bool:
        """
        Return True if the provider is a streaming provider.

        This literally means that the catalog is not the same as the library contents.
        For local based providers (files, plex), the catalog is the same as the library content.
        It also means that data is if this provider is NOT a streaming provider,
        data cross instances is unique, the catalog and library differs per instance.

        Setting this to True will only query one instance of the provider for search and lookups.
        Setting this to False will query all instances of this provider for search and lookups.
        """
        return False

    @property
    def instance_name_postfix(self) -> str | None:
        """Return a (default) instance name postfix for this provider instance."""
        return self.parsed_podcast.get("title")

    async def get_library_podcasts(self) -> AsyncGenerator[Podcast, None]:
        """Retrieve library/subscribed podcasts from the provider."""
        """
        Only one podcast per rss feed is supported. The data format of the rss feed supports
        only one podcast.
        """
        # on sync we renew
        self.parsed_podcast = await self._get_podcast()
        await self._cache_set_podcast()
        yield await self._parse_podcast()

    @use_cache(3600 * 24 * 7)  # Cache for 7 days
    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Get full artist details by id."""
        if prov_podcast_id != self.podcast_id:
            raise RuntimeError(f"Podcast id not in provider: {prov_podcast_id}")
        return await self._parse_podcast()

    @use_cache(3600)  # Cache for 1 hour
    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """Get (full) podcast episode details by id."""
        for idx, episode in enumerate(self.parsed_podcast["episodes"]):
            if prov_episode_id == episode["guid"]:
                if mass_episode := self._parse_episode(episode, idx):
                    return mass_episode
        raise MediaNotFoundError("Episode not found")

    async def get_podcast_episodes(
        self,
        prov_podcast_id: str,
    ) -> AsyncGenerator[PodcastEpisode, None]:
        """List all episodes for the podcast."""
        if prov_podcast_id != self.podcast_id:
            raise Exception(f"Podcast id not in provider: {prov_podcast_id}")
        # sort episodes by published date
        episodes: list[dict[str, Any]] = self.parsed_podcast["episodes"]
        if episodes and episodes[0].get("published", 0) != 0:
            episodes.sort(key=lambda x: x.get("published", 0))
        for idx, episode in enumerate(episodes):
            if mass_episode := self._parse_episode(episode, idx):
                yield mass_episode

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get streamdetails for a track/radio."""
        for episode in self.parsed_podcast["episodes"]:
            if item_id == episode["guid"]:
                stream_url = episode["enclosures"][0]["url"]
                media_info = None
                duration = int(episode.get("total_time") or 0) or None
                if duration is None:
                    media_info = await self._get_stream_media_info(stream_url)
                if duration is None and media_info and media_info.duration:
                    duration = int(media_info.duration)
                    episode["total_time"] = duration
                    await self._cache_set_podcast()
                size = self._get_media_info_size(media_info)

                return StreamDetails(
                    provider=self.instance_id,
                    item_id=item_id,
                    audio_format=AudioFormat(
                        content_type=ContentType.try_parse(
                            media_info.format if media_info else stream_url.split("?", 1)[0]
                        ),
                    ),
                    media_type=MediaType.PODCAST_EPISODE,
                    stream_type=StreamType.CUSTOM,
                    path=stream_url,
                    duration=duration,
                    size=size,
                    data={"duration": duration, "size": size},
                    can_seek=True,
                    allow_seek=True,
                )
        raise MediaNotFoundError("Stream not found")

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """Return podcast audio using HTTP range requests."""
        assert isinstance(streamdetails.path, str)
        if seek_position:
            assert streamdetails.duration, "Duration required for seek requests"

        headers = {**PODCAST_HTTP_HEADERS}
        timeout = ClientTimeout(total=None, connect=5, sock_connect=5, sock_read=4)
        stream_data = streamdetails.data if isinstance(streamdetails.data, dict) else {}
        original_duration = await self._update_stream_original_metadata(
            streamdetails,
            stream_data,
            seek_position,
        )
        seek_supported = await self._update_stream_size_from_head(
            streamdetails,
            stream_data,
            headers,
            timeout,
            seek_position,
        )
        seek_position = self._prepare_seek_headers(
            streamdetails,
            headers,
            original_duration,
            seek_position,
            seek_supported,
        )

        bytes_received = 0
        async for chunk in self._iter_podcast_http_stream(
            streamdetails,
            headers,
            timeout,
            seek_position,
        ):
            bytes_received += len(chunk)
            yield chunk

        if not streamdetails.size:
            streamdetails.size = bytes_received

    async def _update_stream_original_metadata(
        self,
        streamdetails: StreamDetails,
        stream_data: dict[str, Any],
        seek_position: int,
    ) -> int | None:
        """Restore original duration and size before a new podcast seek."""
        assert isinstance(streamdetails.path, str)
        original_duration = stream_data.get("duration") or streamdetails.duration
        original_size = stream_data.get("size") or streamdetails.size

        if seek_position and (
            not original_duration or int(original_duration) < seek_position
        ):
            media_info = await self._get_stream_media_info(streamdetails.path)
            if media_info and media_info.duration:
                original_duration = int(media_info.duration)
                stream_data["duration"] = original_duration
            if media_info and (media_size := self._get_media_info_size(media_info)):
                original_size = media_size
                stream_data["size"] = original_size
        if original_duration:
            streamdetails.duration = int(original_duration)
        if original_size:
            streamdetails.size = int(original_size)
        streamdetails.data = stream_data
        return int(original_duration) if original_duration else None

    async def _update_stream_size_from_head(
        self,
        streamdetails: StreamDetails,
        stream_data: dict[str, Any],
        headers: dict[str, str],
        timeout: ClientTimeout,
        seek_position: int,
    ) -> bool:
        """Update podcast stream size and check byte range support."""
        seek_supported = streamdetails.can_seek
        if seek_position or not streamdetails.size:
            assert isinstance(streamdetails.path, str)
            try:
                async with ClientSession(
                    timeout=timeout,
                    connector=TCPConnector(
                        force_close=True,
                        enable_cleanup_closed=True,
                    ),
                ) as http_session, http_session.head(
                    streamdetails.path,
                    allow_redirects=True,
                    headers=headers,
                ) as resp:
                    resp.raise_for_status()
                    if size := resp.headers.get("Content-Length"):
                        streamdetails.size = int(size)
                        stream_data["size"] = streamdetails.size
                        streamdetails.data = stream_data
                    seek_supported = resp.headers.get("Accept-Ranges", "").lower() == "bytes"
            except (ClientError, TimeoutError) as err:
                self.logger.debug(
                    "Failed to read podcast stream headers for %s: %s",
                    streamdetails.uri,
                    err,
                )
                return bool(streamdetails.size and seek_supported)
        return seek_supported

    def _prepare_seek_headers(
        self,
        streamdetails: StreamDetails,
        headers: dict[str, str],
        original_duration: int | None,
        seek_position: int,
        seek_supported: bool,
    ) -> int:
        """Prepare byte range headers for podcast seek when possible."""
        if seek_position and (
            not seek_supported
            or not streamdetails.size
            or not original_duration
            or streamdetails.audio_format.content_type
            in (ContentType.UNKNOWN, ContentType.M4A, ContentType.M4B)
        ):
            self.logger.warning(
                "Seeking in %s (%s) not possible.",
                streamdetails.uri,
                streamdetails.audio_format.output_format_str,
            )
            seek_position = 0
            streamdetails.seek_position = 0

        if seek_position:
            assert streamdetails.size is not None
            assert original_duration is not None
            skip_bytes = min(
                int(streamdetails.size / original_duration * seek_position),
                streamdetails.size - 1,
            )
            end_byte = streamdetails.size - 1
            headers["Range"] = f"bytes={skip_bytes}-{end_byte}"
        return seek_position

    async def _iter_podcast_http_stream(
        self,
        streamdetails: StreamDetails,
        headers: dict[str, str],
        timeout: ClientTimeout,
        seek_position: int,
    ) -> AsyncGenerator[bytes, None]:
        """Read the podcast HTTP stream with one normal attempt and IPv4 fallback."""
        assert isinstance(streamdetails.path, str)
        bytes_sent = False
        for attempt, ipv4_only in enumerate((False, True, True), start=1):
            try:
                async with ClientSession(
                    timeout=timeout,
                    connector=TCPConnector(
                        force_close=True,
                        enable_cleanup_closed=True,
                        family=socket.AF_INET if ipv4_only else socket.AF_UNSPEC,
                    ),
                ) as http_session, http_session.get(
                    streamdetails.path,
                    allow_redirects=True,
                    headers=headers,
                ) as resp:
                    is_partial = resp.status == 206
                    if seek_position and not is_partial:
                        raise InvalidDataError("HTTP source does not support seeking")
                    resp.raise_for_status()
                    async for chunk in resp.content.iter_any():
                        bytes_sent = True
                        yield chunk
                break
            except (ClientError, TimeoutError) as err:
                if bytes_sent:
                    raise
                log = self.logger.warning if attempt == 3 else self.logger.debug
                log(
                    "Podcast range stream GET failed before first chunk: attempt=%s error=%s",
                    attempt,
                    err,
                )
                if attempt == 3:
                    raise InvalidProviderURI("Podcast stream did not return audio data") from err

    async def _get_stream_media_info(self, stream_url: str) -> AudioTags | None:
        """Retrieve and cache media info for podcast streams without RSS duration."""
        cached_info = await self.mass.cache.get(
            key=stream_url,
            provider=self.instance_id,
            category=CACHE_CATEGORY_MEDIA_INFO,
        )
        if cached_info:
            return AudioTags.parse(cached_info)

        try:
            media_info = await async_parse_tags(stream_url, require_duration=True)
        except Exception as err:
            self.logger.debug("Failed to probe podcast stream %s: %s", stream_url, err)
            return None

        await self.mass.cache.set(
            key=stream_url,
            provider=self.instance_id,
            category=CACHE_CATEGORY_MEDIA_INFO,
            data=media_info.raw,
            expiration=60 * 60 * 24 * 30,
        )
        return media_info

    @staticmethod
    def _get_media_info_size(media_info: AudioTags | None) -> int | None:
        """Return stream size from ffprobe media info when present."""
        if not media_info:
            return None
        try:
            return int(media_info.raw.get("format", {}).get("size") or 0) or None
        except (TypeError, ValueError):
            return None

    async def _parse_podcast(self) -> Podcast:
        """Parse podcast information from podcast feed."""
        assert self.feed_url is not None
        return parse_podcast(
            feed_url=self.feed_url,
            parsed_feed=self.parsed_podcast,
            instance_id=self.instance_id,
            domain=self.domain,
            mass_item_id=self.podcast_id,
        )

    def _parse_episode(
        self, episode_obj: dict[str, Any], fallback_position: int
    ) -> PodcastEpisode | None:
        episode_result = parse_podcast_episode(
            episode=episode_obj,
            prov_podcast_id=self.podcast_id,
            episode_cnt=fallback_position,
            podcast_cover=self.parsed_podcast.get("cover_url"),
            instance_id=self.instance_id,
            domain=self.domain,
            mass_item_id=episode_obj["guid"],
        )
        # Override remotely_accessible as these providers can have unreliable image URLs
        if episode_result and episode_result.metadata.images:
            new_images = []
            for img in episode_result.metadata.images:
                new_images.append(
                    MediaItemImage(
                        type=img.type,
                        path=img.path,
                        provider=img.provider,
                        remotely_accessible=False,  # Force through imageproxy
                    )
                )
            episode_result.metadata.images = UniqueList(new_images)

        return episode_result

    async def _get_podcast(self) -> dict[str, Any]:
        assert self.feed_url is not None
        return await get_podcastparser_dict(session=self.mass.http_session, feed_url=self.feed_url)

    async def _cache_get_podcast(self) -> dict[str, Any]:
        parsed_podcast = await self.mass.cache.get(
            key=self.podcast_id,
            provider=self.instance_id,
            category=CACHE_CATEGORY_PODCASTS,
            default=None,
        )
        if parsed_podcast is None:
            parsed_podcast = await self._get_podcast()

        # this is a dictionary from podcastparser
        return parsed_podcast  # type: ignore[no-any-return]

    async def _cache_set_podcast(self) -> None:
        await self.mass.cache.set(
            key=self.podcast_id,
            provider=self.instance_id,
            category=CACHE_CATEGORY_PODCASTS,
            data=self.parsed_podcast,
            expiration=60 * 60 * 24,  # 1 day
        )

    async def resolve_image(self, path: str) -> str | bytes:
        """Resolve image for RSS provider with fallback to podcast cover."""
        if not path.startswith("http"):
            return path

        try:
            async with self.mass.http_session.get(path, raise_for_status=True) as response:
                # Check if we got actual image content
                content_type = response.headers.get("content-type", "").lower()
                if not content_type.startswith(("image/", "application/octet-stream")):
                    # Not an image - likely redirected to error page
                    raise ClientError(f"Invalid content type: {content_type}")

                return await response.read()

        except (ClientError, Exception):
            # Try podcast cover fallback
            podcast_cover = self.parsed_podcast.get("cover_url")
            if podcast_cover and isinstance(podcast_cover, str) and podcast_cover != path:
                async with self.mass.http_session.get(
                    podcast_cover, raise_for_status=True
                ) as response:
                    return await response.read()

            raise MediaNotFoundError(f"Episode image not found: {path}")
