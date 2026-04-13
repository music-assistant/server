"""
Podcast RSS Feed Music Provider for Music Assistant.

A URL to a podcast feed can be configured. The contents of that specific podcast
feed will be forwarded to music assistant. In order to have multiple podcast feeds,
multiple instances with each one feed must exist.

"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

import podcastparser
from aiohttp.client_exceptions import ClientError
from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import InvalidProviderURI, MediaNotFoundError
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
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

CONF_FEED_URL = "feed_url"

CACHE_CATEGORY_PODCASTS = 0

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
                return StreamDetails(
                    provider=self.instance_id,
                    item_id=item_id,
                    audio_format=AudioFormat(
                        content_type=ContentType.try_parse(stream_url),
                    ),
                    media_type=MediaType.PODCAST_EPISODE,
                    stream_type=StreamType.HTTP,
                    path=stream_url,
                    can_seek=True,
                    allow_seek=True,
                    extra_input_args=[
                        "-user_agent",
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    ],
                )
        raise MediaNotFoundError("Stream not found")

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
