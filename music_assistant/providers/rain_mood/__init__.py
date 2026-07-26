"""
Rainy Mood provider for Music Assistant.

Serves a looping rain ambience from rainymood.com as a sound effect, suitable for
direct playback or as the source for a queue's audio overlay. The remote stream is
handed straight to the player pipeline - the audio overlay engine takes care of the
looping, format conversion and volume mixing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import ContentType, MediaType, ProviderFeature, StreamType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import AudioFormat, ProviderMapping, SoundEffect
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.SOUND_EFFECTS,
}

RAIN_ITEM_ID = "rain"
RAIN_URL = "https://media.rainymood.com/0.mp3"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return RainyMoodProvider(mass, manifest, config, SUPPORTED_FEATURES)


class RainyMoodProvider(MusicProvider):
    """Music provider serving a looping rain ambience from rainymood.com."""

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider (none needed)."""
        return ()

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        return True

    async def get_sound_effect(self, prov_sound_effect_id: str) -> SoundEffect:
        """Get full sound effect details by id."""
        if prov_sound_effect_id != RAIN_ITEM_ID:
            raise MediaNotFoundError(f"Unknown sound effect: {prov_sound_effect_id}")
        return self._build_sound_effect()

    async def get_sound_effects(self) -> AsyncGenerator[SoundEffect]:
        """Get all sound effect items this provider offers."""
        yield self._build_sound_effect()

    async def get_stream_details(
        self, item_id: str, media_type: MediaType = MediaType.TRACK
    ) -> StreamDetails:
        """Return the streamdetails to stream the rain sound effect."""
        if item_id != RAIN_ITEM_ID:
            raise MediaNotFoundError(f"Unknown sound effect: {item_id}")
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            # rainymood serves an MP3 stream; declare it explicitly instead of
            # leaving the container type to be probed
            audio_format=AudioFormat(content_type=ContentType.MP3),
            media_type=MediaType.SOUND_EFFECT,
            stream_type=StreamType.HTTP,
            path=RAIN_URL,
            allow_seek=False,
            can_seek=False,
        )

    def _build_sound_effect(self) -> SoundEffect:
        """Create the SoundEffect item for the rain ambience."""
        sound_effect = SoundEffect(
            item_id=RAIN_ITEM_ID,
            provider=self.instance_id,
            name="Rain",
            translation_key=RAIN_ITEM_ID,
            provider_mappings={
                ProviderMapping(
                    item_id=RAIN_ITEM_ID,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )
        sound_effect.metadata.description = "Looping rain ambience from rainymood.com."
        return sound_effect
