"""Base class for configuration categories."""

from __future__ import annotations

from typing import TYPE_CHECKING, override

from music_assistant.providers.nicovideo.config.descriptor import ConfigReader

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType

    from music_assistant.models.provider import Provider


class ConfigCategoryBase(ConfigReader):
    """Base class for config categories."""

    def __init__(self, provider: Provider) -> None:
        """Initialize category with provider instance."""
        self.provider = provider

    @override
    def get_value(self, key: str) -> ConfigValueType:
        """Get config value from provider."""
        return self.provider.get_setup_value(key)
