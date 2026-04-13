"""Base class for configuration categories."""

from __future__ import annotations

from typing import TYPE_CHECKING, override

from music_assistant.controllers.config import ConfigController
from music_assistant.providers.nicovideo.config.descriptor import ConfigReader

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig

    from music_assistant.models.provider import Provider


class ConfigCategoryBase(ConfigReader):
    """Base class for config categories."""

    def __init__(self, provider: Provider) -> None:
        """Initialize category with provider instance."""
        self.provider = provider

    @property
    def reader(self) -> ProviderConfig:
        """Get the config reader interface."""
        return self.provider.config

    @property
    def writer(self) -> ConfigController:
        """Get the config writer interface."""
        return self.provider.mass.config

    @override
    def get_value(self, key: str) -> ConfigValueType:
        """Get config value from provider."""
        return self.reader.get_value(key)
