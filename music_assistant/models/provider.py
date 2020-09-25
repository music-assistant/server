"""Generic Models and helpers for providers/plugins."""

from abc import abstractmethod
from enum import Enum
from typing import List

from music_assistant.helpers.typing import MusicAssistantType
from music_assistant.models.config_entry import ConfigEntry


class ProviderType(Enum):
    """Enum with plugin types."""

    MUSIC_PROVIDER = "music_provider"
    PLAYER_PROVIDER = "player_provider"
    GENERIC = "generic"


class Provider:
    """Base model for a provider/plugin."""

    mass: MusicAssistantType = (
        None  # will be set automagically while loading the provider
    )
    available: bool = False  # will be set automagically while loading the provider

    @property
    @abstractmethod
    def type(self) -> ProviderType:
        """Return ProviderType."""
        return ProviderType.GENERIC

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

    @abstractmethod
    async def async_on_start(self) -> bool:
        """
        Handle initialization of the provider based on config.

        Return bool if start was succesfull. Called on startup.
        """
        raise NotImplementedError

    @abstractmethod
    async def async_on_stop(self) -> None:
        """Handle correct close/cleanup of the provider on exit. Called on shutdown."""

    async def async_on_reload(self) -> None:
        """Handle configuration changes for this provider. Called on reload."""
        await self.async_on_stop()
        await self.async_on_start()
