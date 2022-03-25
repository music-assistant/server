"""Models for providers/plugins."""

from abc import abstractmethod
from enum import Enum
from typing import Dict, List, Optional

from music_assistant.helpers.typing import MusicAssistant, Players
from music_assistant.config.models import ConfigEntry
from music_assistant.music.models import (
    Album,
    Artist,
    MediaType,
    Playlist,
    Radio,
    SearchResult,
    Track,
)
from music_assistant.models.streamdetails import StreamDetails


class ProviderType(Enum):
    """Enum with plugin types."""

    MUSIC_PROVIDER = "music_provider"
    PLAYER_PROVIDER = "player_provider"
    METADATA_PROVIDER = "metadata_provider"
    PLUGIN = "plugin"


class Provider:
    """Base model for a provider/plugin."""

    mass: MusicAssistant = None  # will be set automagically while loading the provider
    available: bool = False  # will be set automagically while loading the provider

    @property
    @abstractmethod
    def type(self) -> ProviderType:
        """Return ProviderType."""

    @property
    @abstractmethod
    def id(self) -> str:
        """Return provider ID for this provider."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Return provider Name for this provider."""

    @property
    @abstractmethod
    def config_entries(self) -> List[ConfigEntry]:
        """Return Config Entries for this provider."""

    async def setup(self) -> bool:
        """Handle async initialization of the provider."""

    @abstractmethod
    async def on_start(self) -> bool:
        """
        Handle initialization of the provider based on config.

        Return bool if start was succesfull. Called on startup.
        """
        raise NotImplementedError

    @abstractmethod
    async def on_stop(self) -> None:
        """Handle correct close/cleanup of the provider on exit. Called on shutdown/reload."""




class PlayerProvider(Provider):
    """
    Base class for a Playerprovider.

    Should be overridden/subclassed by provider specific implementation.
    """

    @property
    def type(self) -> ProviderType:
        """Return ProviderType."""
        return ProviderType.PLAYER_PROVIDER

    @property
    def players(self) -> Players:
        """Return all players belonging to this provider."""
        # pylint: disable=no-member
        return [player for player in self.mass.players if player.provider_id == self.id]

