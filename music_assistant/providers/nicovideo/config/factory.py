"""Configuration factory for creating typed config descriptors."""

from __future__ import annotations

from collections.abc import Callable
from typing import overload

from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import ConfigEntryType

from .descriptor import ConfigDescriptor

# Global registry for all config entries
_registry: list[ConfigEntry] = []


class ConfigFactory:
    """Factory class for creating config options with automatic category assignment."""

    def __init__(self, category: str) -> None:
        """Initialize factory with a specific category name."""
        self.category = category

    def bool_config(
        self,
        key: str,
        label: str,
        default: bool = False,
        description: str = "",
    ) -> ConfigDescriptor[bool]:
        """Create boolean config options."""
        return ConfigDescriptor(
            cast=ConfigFactory.as_bool(default),
            config_entry=self._create_entry(
                key=key,
                entry_type=ConfigEntryType.BOOLEAN,
                label=label,
                default_value=default,
                description=description,
            ),
        )

    def int_config(
        self,
        key: str,
        label: str,
        default: int = 25,
        min_val: int = 1,
        max_val: int = 100,
        description: str = "",
    ) -> ConfigDescriptor[int]:
        """Create integer config options."""
        return ConfigDescriptor(
            cast=ConfigFactory.as_int(default, min_val, max_val),
            config_entry=self._create_entry(
                key=key,
                entry_type=ConfigEntryType.INTEGER,
                label=label,
                default_value=default,
                description=description,
                value_range=(min_val, max_val),
            ),
        )

    def str_list_config(
        self, key: str, label: str, description: str = ""
    ) -> ConfigDescriptor[list[str]]:
        """Create string list config options (comma-separated tags)."""
        return ConfigDescriptor(
            cast=ConfigFactory.as_str_list(),
            config_entry=self._create_entry(
                key=key,
                entry_type=ConfigEntryType.STRING,
                label=label,
                default_value="",
                description=description,
            ),
        )

    @overload
    def str_config(
        self, key: str, label: str, default: str, description: str = ""
    ) -> ConfigDescriptor[str]: ...

    @overload
    def str_config(
        self, key: str, label: str, default: None = None, description: str = ""
    ) -> ConfigDescriptor[str | None]: ...

    def str_config(
        self, key: str, label: str, default: str | None = None, description: str = ""
    ) -> ConfigDescriptor[str] | ConfigDescriptor[str | None]:
        """Create string config options that can be None."""
        return ConfigDescriptor(
            cast=ConfigFactory.as_str(default),
            config_entry=self._create_entry(
                key=key,
                entry_type=ConfigEntryType.STRING,
                label=label,
                default_value=default,
                description=description,
            ),
        )

    def secure_str_or_none_config(
        self, key: str, label: str, description: str = ""
    ) -> ConfigDescriptor[str | None]:
        """Create secure string config options that can be None."""
        return ConfigDescriptor(
            cast=ConfigFactory.as_str(None),
            config_entry=self._create_entry(
                key=key,
                entry_type=ConfigEntryType.SECURE_STRING,
                label=label,
                default_value="",
                description=description,
            ),
        )

    def _create_entry(
        self,
        key: str,
        entry_type: ConfigEntryType,
        label: str,
        default_value: ConfigValueType,
        description: str,
        value_range: tuple[int, int] | None = None,
    ) -> ConfigEntry:
        """Create and register a ConfigEntry."""
        entry = ConfigEntry(
            key=key,
            type=entry_type,
            label=label,
            required=False,
            default_value=default_value,
            description=description,
            category=self.category,
            range=value_range,
        )
        _registry.append(entry)
        return entry

    @classmethod
    def as_bool(cls, default: bool = False) -> Callable[[ConfigValueType], bool]:
        """Return a caster that converts a raw value to bool with default."""

        def _cast(v: ConfigValueType) -> bool:
            return bool(v) if v is not None else default

        return _cast

    @classmethod
    def as_int(
        cls, default: int = 0, min_val: int = 1, max_val: int = 100
    ) -> Callable[[ConfigValueType], int]:
        """Return a caster that converts a raw value to int with validation and default."""

        def _cast(v: ConfigValueType) -> int:
            if not isinstance(v, int) or v < min_val:
                return default
            return min(v, max_val)

        return _cast

    @classmethod
    @overload
    def as_str(cls, default: str) -> Callable[[ConfigValueType], str]: ...

    @classmethod
    @overload
    def as_str(cls, default: str | None = None) -> Callable[[ConfigValueType], str | None]: ...

    @classmethod
    def as_str(cls, default: str | None = None) -> Callable[[ConfigValueType], str | None]:
        """Return a caster that converts a raw value to str or None (no default)."""

        def _cast(v: ConfigValueType) -> str | None:
            return str(v) if v is not None else default

        return _cast

    @classmethod
    def as_str_list(cls) -> Callable[[ConfigValueType], list[str]]:
        """Return a caster that converts a raw value to list of strings."""

        def _cast(v: ConfigValueType) -> list[str]:
            if not v or not isinstance(v, str):
                return []
            # Split by comma and clean up whitespace
            return [tag.strip() for tag in v.split(",") if tag.strip()]

        return _cast


async def get_config_entries_impl() -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    # Combine entries from logical categories
    return tuple(_registry)
