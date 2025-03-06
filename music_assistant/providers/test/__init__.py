"""Test/Demo provider that creates a collection of fake media items."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    Audiobook,
    AudioFormat,
    ItemMapping,
    MediaItemChapter,
    MediaItemImage,
    MediaItemMetadata,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    Track,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import MASS_LOGO, SILENCE_FILE_LONG, VARIOUS_ARTISTS_FANART
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


DEFAULT_THUMB = MediaItemImage(
    type=ImageType.THUMB,
    path=MASS_LOGO,
    provider="builtin",
    remotely_accessible=False,
)

DEFAULT_FANART = MediaItemImage(
    type=ImageType.FANART,
    path=VARIOUS_ARTISTS_FANART,
    provider="builtin",
    remotely_accessible=False,
)

CONF_KEY_NUM_ARTISTS = "num_artists"
CONF_KEY_NUM_ALBUMS = "num_albums"
CONF_KEY_NUM_TRACKS = "num_tracks"
CONF_KEY_NUM_PODCASTS = "num_podcasts"
CONF_KEY_NUM_AUDIOBOOKS = "num_audiobooks"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return TestProvider(mass, manifest, config)


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    return (
        ConfigEntry(
            key=CONF_KEY_NUM_ARTISTS,
            type=ConfigEntryType.INTEGER,
            label="Number of (test) artists",
            description="Number of test artists to generate",
            default_value=5,
            required=False,
        ),
        ConfigEntry(
            key=CONF_KEY_NUM_ALBUMS,
            type=ConfigEntryType.INTEGER,
            label="Number of (test) albums per artist",
            description="Number of test albums to generate per artist",
            default_value=5,
            required=False,
        ),
        ConfigEntry(
            key=CONF_KEY_NUM_TRACKS,
            type=ConfigEntryType.INTEGER,
            label="Number of (test) tracks per album",
            description="Number of test tracks to generate per artist-album",
            default_value=20,
            required=False,
        ),
        ConfigEntry(
            key=CONF_KEY_NUM_PODCASTS,
            type=ConfigEntryType.INTEGER,
            label="Number of (test) podcasts",
            description="Number of test podcasts to generate",
            default_value=5,
            required=False,
        ),
        ConfigEntry(
            key=CONF_KEY_NUM_AUDIOBOOKS,
            type=ConfigEntryType.INTEGER,
            label="Number of (test) audiobooks",
            description="Number of test audiobooks to generate",
            default_value=5,
            required=False,
        ),
    )


class TestProvider(MusicProvider):
    """Test/Demo provider that creates a collection of fake media items."""

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        return False

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        sup_features = {ProviderFeature.BROWSE}
        if self.config.get_value(CONF_KEY_NUM_ARTISTS):
            sup_features.add(ProviderFeature.LIBRARY_ARTISTS)
        if self.config.get_value(CONF_KEY_NUM_ALBUMS):
            sup_features.add(ProviderFeature.LIBRARY_ALBUMS)
        if self.config.get_value(CONF_KEY_NUM_TRACKS):
            sup_features.add(ProviderFeature.LIBRARY_TRACKS)
        if self.config.get_value(CONF_KEY_NUM_PODCASTS):
            sup_features.add(ProviderFeature.LIBRARY_PODCASTS)
        if self.config.get_value(CONF_KEY_NUM_AUDIOBOOKS):
            sup_features.add(ProviderFeature.LIBRARY_AUDIOBOOKS)
        return sup_features

    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        artist_idx, album_idx, track_idx = prov_track_id.split("_", 3)
        return Track(
            item_id=prov_track_id,
            provider=self.lookup_key,
            name=f"Test Track {artist_idx} - {album_idx} - {track_idx}",
            duration=60,
            artists=UniqueList([await self.get_artist(artist_idx)]),
            album=await self.get_album(f"{artist_idx}_{album_idx}"),
            provider_mappings={
                ProviderMapping(
                    item_id=prov_track_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                ),
            },
            metadata=MediaItemMetadata(images=UniqueList([DEFAULT_THUMB])),
            disc_number=1,
            track_number=int(track_idx),
        )

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        return Artist(
            item_id=prov_artist_id,
            provider=self.lookup_key,
            name=f"Test Artist {prov_artist_id}",
            metadata=MediaItemMetadata(images=UniqueList([DEFAULT_THUMB, DEFAULT_FANART])),
            provider_mappings={
                ProviderMapping(
                    item_id=prov_artist_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )

    async def get_album(self, prov_album_id: str) -> Album:
        """Get full artist details by id."""
        artist_idx, album_idx = prov_album_id.split("_", 2)
        return Album(
            item_id=prov_album_id,
            provider=self.lookup_key,
            name=f"Test Album {album_idx}",
            artists=UniqueList([await self.get_artist(artist_idx)]),
            provider_mappings={
                ProviderMapping(
                    item_id=prov_album_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
            metadata=MediaItemMetadata(images=UniqueList([DEFAULT_THUMB])),
        )

    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Get full podcast details by id."""
        return Podcast(
            item_id=prov_podcast_id,
            provider=self.lookup_key,
            name=f"Test Podcast {prov_podcast_id}",
            metadata=MediaItemMetadata(images=UniqueList([DEFAULT_THUMB])),
            provider_mappings={
                ProviderMapping(
                    item_id=prov_podcast_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
            publisher="Test Publisher",
        )

    async def get_audiobook(self, prov_audiobook_id: str) -> Audiobook:
        """Get full audiobook details by id."""
        return Audiobook(
            item_id=prov_audiobook_id,
            provider=self.lookup_key,
            name=f"Test Audiobook {prov_audiobook_id}",
            metadata=MediaItemMetadata(
                images=UniqueList([DEFAULT_THUMB]),
                description="This is a description for Test Audiobook",
                chapters=[
                    MediaItemChapter(position=1, name="Chapter 1", start=10, end=20),
                    MediaItemChapter(position=2, name="Chapter 2", start=20, end=40),
                    MediaItemChapter(position=2, name="Chapter 3", start=40),
                ],
            ),
            provider_mappings={
                ProviderMapping(
                    item_id=prov_audiobook_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
            publisher="Test Publisher",
            authors=UniqueList(["AudioBook Author"]),
            narrators=UniqueList(["AudioBook Narrator"]),
            duration=60,
        )

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve library artists from the provider."""
        num_artists = self.config.get_value(CONF_KEY_NUM_ARTISTS)
        assert isinstance(num_artists, int)
        for artist_idx in range(num_artists):
            yield await self.get_artist(str(artist_idx))

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve library albums from the provider."""
        num_artists = self.config.get_value(CONF_KEY_NUM_ARTISTS) or 5
        assert isinstance(num_artists, int)
        num_albums = self.config.get_value(CONF_KEY_NUM_ALBUMS)
        assert isinstance(num_albums, int)
        for artist_idx in range(num_artists):
            for album_idx in range(num_albums):
                album_item_id = f"{artist_idx}_{album_idx}"
                yield await self.get_album(album_item_id)

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from the provider."""
        num_artists = self.config.get_value(CONF_KEY_NUM_ARTISTS) or 5
        assert isinstance(num_artists, int)
        num_albums = self.config.get_value(CONF_KEY_NUM_ALBUMS) or 5
        assert isinstance(num_albums, int)
        num_tracks = self.config.get_value(CONF_KEY_NUM_TRACKS)
        assert isinstance(num_tracks, int)
        for artist_idx in range(num_artists):
            for album_idx in range(num_albums):
                for track_idx in range(num_tracks):
                    track_item_id = f"{artist_idx}_{album_idx}_{track_idx}"
                    yield await self.get_track(track_item_id)

    async def get_library_podcasts(self) -> AsyncGenerator[Podcast, None]:
        """Retrieve library tracks from the provider."""
        num_podcasts = self.config.get_value(CONF_KEY_NUM_PODCASTS)
        assert isinstance(num_podcasts, int)
        for podcast_idx in range(num_podcasts):
            yield await self.get_podcast(str(podcast_idx))

    async def get_library_audiobooks(self) -> AsyncGenerator[Audiobook, None]:
        """Retrieve library audiobooks from the provider."""
        num_audiobooks = self.config.get_value(CONF_KEY_NUM_AUDIOBOOKS)
        assert isinstance(num_audiobooks, int)
        for audiobook_idx in range(num_audiobooks):
            yield await self.get_audiobook(str(audiobook_idx))

    async def get_podcast_episodes(
        self,
        prov_podcast_id: str,
    ) -> AsyncGenerator[PodcastEpisode, None]:
        """Get all PodcastEpisodes for given podcast id."""
        num_episodes = 25
        for episode_idx in range(num_episodes):
            yield await self.get_podcast_episode(f"{prov_podcast_id}_{episode_idx}")

    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """Get (full) podcast episode details by id."""
        podcast_id, episode_idx = prov_episode_id.split("_", 2)
        return PodcastEpisode(
            item_id=prov_episode_id,
            provider=self.lookup_key,
            name=f"Test PodcastEpisode {podcast_id}-{episode_idx}",
            duration=60,
            podcast=ItemMapping(
                item_id=podcast_id,
                provider=self.lookup_key,
                name=f"Test Podcast {podcast_id}",
                media_type=MediaType.PODCAST,
                image=DEFAULT_THUMB,
            ),
            provider_mappings={
                ProviderMapping(
                    item_id=prov_episode_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
            metadata=MediaItemMetadata(
                description="This is a description for "
                f"Test PodcastEpisode {episode_idx} of Test Podcast {podcast_id}"
            ),
            position=int(episode_idx),
        )

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get streamdetails for a track/radio."""
        return StreamDetails(
            provider=self.lookup_key,
            item_id=item_id,
            audio_format=AudioFormat(
                content_type=ContentType.OGG,
                sample_rate=48000,
                bit_depth=16,
                channels=2,
            ),
            media_type=media_type,
            stream_type=StreamType.HTTP,
            path=SILENCE_FILE_LONG,
            can_seek=True,
            allow_seek=True,
        )
