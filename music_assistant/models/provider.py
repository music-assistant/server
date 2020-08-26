"""Generic Models and helpers for plugins."""

from abc import abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable, List, Optional, Union, Any

from music_assistant.models.config_entry import ConfigEntry


class ProviderType(Enum):
    """Enum with plugin types."""
    POWER_CONTROL = "power_control"
    VOLUME_CONTROL = "volume_control"
    MUSIC_PROVIDER = "music_provider"
    PLAYER_PROVIDER = "player_provider"
    GENERIC = "generic"


@dataclass
class Provider:
    """Base model for a provider/plugin."""
    id: str
    name: str
    type: ProviderType = ProviderType.GENERIC
    config_entries: List[ConfigEntry] = field(default_factory=list)  # custom config entries for this provider
    mass: Optional[Any] = None

    @abstractmethod
    async def async_on_start(self):
        """Called on startup. Handle initialization of the provider based on config."""
        raise NotImplementedError

    @abstractmethod
    async def async_on_stop(self):
        """Called on shutdown. Handle correct close/cleanup of the provider on exit."""
        raise NotImplementedError

    async def async_on_reload(self):
        """Called on reload. Handle configuration changes for this provider."""
        await self.async_on_stop()
        await self.async_on_start()


@dataclass
class PowerControl():
    """Model for a power control."""
    provider_id: str
    state: bool
    power_on: Awaitable
    power_off: Awaitable


@dataclass
class PowerControlProvider(Provider):
    """Model for a Power Control plugin."""
    type: ProviderType.POWER_CONTROL
    controls: List[PowerControl] = field(default_factory=list)


@dataclass
class VolumeControl():
    """Model for a power control."""
    provider_id: str
    volume_level: int
    volume_muted: bool
    set_volume: Callable[..., Union[None, Awaitable]]
    set_mute: Callable[..., Union[None, Awaitable]]


@dataclass
class VolumeControlProvider(Provider):
    """Model for a volume control plugin."""
    type: ProviderType.VOLUME_CONTROL
    controls: List[VolumeControl] = field(default_factory=list)
