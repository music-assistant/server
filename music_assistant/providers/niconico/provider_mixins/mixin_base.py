"""Abstract base class for NiconicoMusicProvider mixins."""

from abc import ABC, abstractmethod
from typing import ClassVar

from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.niconico.adapter import NicoNicoMusicAssistantAdapter


class NiconicoMusicProviderMixinBase(ABC):
    """Abstract base class for NiconicoMusicProvider mixins."""

    # Class variable where each mixin declares its supported features
    _supported_features: ClassVar[set[ProviderFeature]] = set()

    @classmethod
    def get_supported_features_for_mixin(cls) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        return cls._supported_features.copy()

    @property
    @abstractmethod
    def provider(self) -> MusicProvider:
        """Return the MusicProvider instance associated with this Provider."""

    @property
    @abstractmethod
    def niconico_adapter(self) -> NicoNicoMusicAssistantAdapter:
        """Return the NicoNicoMusicAssistantAdapter instance associated with this Provider."""

    @abstractmethod
    async def get_stream_details_for_mixin(
        self, item_id: str, media_type: MediaType
    ) -> StreamDetails | None:
        """Get stream details (streaming URL and format) for given item."""
