"""Test/Demo provider that creates a collection of fake media items."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from music_assistant.common.models.config_entries import ConfigEntry
from music_assistant.common.models.enums import (
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant.common.models.media_items import (
    Album,
    Artist,
    AudioFormat,
    MediaItemImage,
    MediaItemMetadata,
    ProviderMapping,
    Track,
    UniqueList,
)
from music_assistant.common.models.streamdetails import StreamDetails
from music_assistant.constants import MASS_LOGO, VARIOUS_ARTISTS_FANART
from music_assistant.server.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant.common.models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType


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
    return ()


class TestProvider(MusicProvider):
    """Test/Demo provider that creates a collection of fake media items."""

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        return False

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this Provider."""
        return (ProviderFeature.LIBRARY_TRACKS,)

    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        artist_idx, album_idx, track_idx = prov_track_id.split("_", 3)
        return Track(
            item_id=prov_track_id,
            provider=self.instance_id,
            name=f"Test Track {artist_idx} - {album_idx} - {track_idx}",
            duration=5,
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
            provider=self.instance_id,
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
            provider=self.instance_id,
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

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from the provider."""
        for artist_idx in range(50):
            for album_idx in range(25):
                for track_idx in range(25):
                    track_item_id = f"{artist_idx}_{album_idx}_{track_idx}"
                    yield await self.get_track(track_item_id)

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Get streamdetails for a track/radio."""
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(
                content_type=ContentType.MP3,
                sample_rate=44100,
                bit_depth=16,
                channels=2,
            ),
            media_type=MediaType.TRACK,
            stream_type=StreamType.HTTP,
            path=item_id,
            can_seek=True,
        )
