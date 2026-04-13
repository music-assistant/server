"""Configuration descriptor implementation for Nicovideo provider."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ConfigValueType


class ConfigReader(Protocol):
    """Protocol for configuration readers."""

    def get_value(self, key: str) -> ConfigValueType:
        """Retrieve a configuration value by key."""
        ...


class ConfigDescriptor[T]:
    """Typed config descriptor with embedded ConfigEntry."""

    def __init__(
        self,
        cast: Callable[[ConfigValueType], T],
        config_entry: ConfigEntry,
    ) -> None:
        """Initialize descriptor.

        Args:
            cast: Transformation/validation applied to raw value.
            config_entry: ConfigEntry definition for this option.
        """
        self.cast = cast
        self.config_entry = config_entry

    @property
    def key(self) -> str:
        """Get the config key from the embedded ConfigEntry."""
        return self.config_entry.key

    def __get__(self, instance: ConfigReader, owner: type) -> T:
        """Descriptor access."""
        raw = instance.get_value(self.key)
        return self.cast(raw)
