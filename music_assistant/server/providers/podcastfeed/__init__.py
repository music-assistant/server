"""
Podcast RSS Feed Music Provider for Music Assistant.

A URL to a podcast feed can be configured. The contents of that specific podcast
feed will be forwarded to music assistant. In order to have multiple podcast feeds,
multiple instances with each one feed must exist.

"""

from __future__ import annotations

import urllib.request
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

import podcastparser

from music_assistant.common.models.config_entries import ConfigEntry, ConfigValueType
from music_assistant.common.models.enums import (
    ConfigEntryType,
    ContentType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant.common.models.errors import InvalidProviderURI, MediaNotFoundError
from music_assistant.common.models.media_items import (
    Artist,
    AudioFormat,
    ImageType,
    MediaItemImage,
    MediaItemType,
    ProviderMapping,
    SearchResults,
    Track,
)
from music_assistant.common.models.streamdetails import StreamDetails
from music_assistant.server.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant.common.models.config_entries import ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType

CONF_FEED_URL = "feed_url"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    # ruff: noqa: ARG001
    if not config.get_value(CONF_FEED_URL):
        msg = "No podcast feed set"
        return InvalidProviderURI(msg)
    return PodcastMusicprovider(mass, manifest, config)


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
    return (
        ConfigEntry(
            key=CONF_FEED_URL,
            type=ConfigEntryType.STRING,
            default_value=[],
            label="RSS Feed URL",
            required=True,
        ),
    )


class PodcastMusicprovider(MusicProvider):
    """Podcast RSS Feed Music Provider."""

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this Provider."""
        return (
            ProviderFeature.BROWSE,
            ProviderFeature.SEARCH,
            ProviderFeature.LIBRARY_ARTISTS,
            ProviderFeature.LIBRARY_TRACKS,
            # see the ProviderFeature enum for all available features
        )

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        # ruff: noqa: S310
        # ruff: noqa: ASYNC210
        self.parsed = await podcastparser.parse(
            self.config.get_value(CONF_FEED_URL),
            urllib.request.urlopen(
                podcastparser.normalize_feed_url(self.config.get_value(CONF_FEED_URL))
            ),
        )

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
        return True

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 5,
    ) -> SearchResults:
        """Perform search on musicprovider.

        :param search_query: Search query.
        :param media_types: A list of media_types to include.
        :param limit: Number of items to return in the search (per type).
        """
        result = SearchResults()

        if MediaType.ARTIST in media_types or media_types is None:
            # return podcast if artist matches podcast name
            if search_query in self.parsed["title"]:
                result.artists.append(await self._parse_artist())

        if MediaType.TRACK in media_types or media_types is None:
            if search_query in self.parsed["title"]:
                for episode in self.parsed["episodes"]:
                    result.tracks.append(await self._parse_track(episode))

        return result

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve library artists from the provider."""
        yield await self._parse_artist()

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from the provider."""
        for episode in self.parsed["episodes"]:
            yield await self._parse_track(episode)

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        return await self._parse_artist()

    # type: ignore[return]
    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        for episode in self.parsed["episodes"]:
            if prov_track_id in episode["guid"]:
                return await self._parse_track(episode)
        raise MediaNotFoundError("Track not found")

    async def library_add(self, item: MediaItemType) -> bool:
        """Add item to provider's library. Return true on success."""
        return True

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Remove item from provider's library. Return true on success."""
        return True

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Get streamdetails for a track/radio."""
        for episode in self.parsed["episodes"]:
            if item_id in episode["guid"]:
                return StreamDetails(
                    provider=self.instance_id,
                    item_id=item_id,
                    audio_format=AudioFormat(
                        # hard coded mp3 for now
                        content_type=ContentType.MP3,
                    ),
                    media_type=MediaType.TRACK,
                    stream_type=StreamType.HTTP,
                    path=episode["enclosures"][0]["url"],
                )
        raise MediaNotFoundError("Stream not found")

    async def resolve_image(self, path: str) -> str | bytes:
        """
        Resolve an image from an image path.

        This either returns (a generator to get) raw bytes of the image or
        a string with an http(s) URL or local path that is accessible from the server.
        """
        return path

    async def sync_library(self, media_types: tuple[MediaType, ...]) -> None:
        """Run library sync for this provider."""
        self.parsed = podcastparser.parse(
            self.config.get_value(CONF_FEED_URL),
            urllib.request.urlopen(self.config.get_value(CONF_FEED_URL)),
        )

    async def _parse_artist(self) -> Artist:
        """Parse artist information from podcast feed."""
        artist = Artist(
            item_id=hash(self.parsed["title"]),
            name=self.parsed["title"],
            provider=self.domain,
            provider_mappings={
                ProviderMapping(
                    item_id=self.parsed["title"],
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=self.parsed["link"],
                )
            },
        )

        artist.metadata.description = self.parsed["description"]
        artist.metadata.style = "Podcast"

        if self.parsed["cover_url"]:
            img_url = self.parsed["cover_url"]
            artist.metadata.images = [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=img_url,
                    provider=self.lookup_key,
                    remotely_accessible=True,
                )
            ]

        return artist

    async def _parse_track(self, track_obj: dict, track_position: int = 0) -> Track:
        name = track_obj["title"]
        track_id = track_obj["guid"]
        track = Track(
            item_id=track_id,
            provider=self.domain,
            name=name,
            duration=track_obj["total_time"],
            provider_mappings={
                ProviderMapping(
                    item_id=track_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    audio_format=AudioFormat(
                        content_type=ContentType.MP3,
                    ),
                    url=track_obj["link"],
                )
            },
            position=track_position,
        )

        track.artists.append(await self._parse_artist())

        if "episode_art_url" in track_obj:
            track.metadata.images = [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=track_obj["episode_art_url"],
                    provider=self.lookup_key,
                    remotely_accessible=True,
                )
            ]
        track.metadata.description = track_obj["description"]
        track.metadata.explicit = track_obj["explicit"]

        return track
