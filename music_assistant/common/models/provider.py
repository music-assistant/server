"""Models for providers and plugins in the MA ecosystem."""

from dataclasses import dataclass, field
from logging import Logger
from typing import TYPE_CHECKING, Any, Dict, List, Tuple

from mashumaro import DataClassDictMixin
from .config_entries import ConfigEntry, CONFIG_ENTRY_ENABLED, ConfigValues
from music_assistant.common.helpers.json import load_json_file


@dataclass
class Provider(DataClassDictMixin):
    """Base model for provider details."""

    domain: str
    name: str
    codeowners: list[str]

    # optional params
    # config_entries: list of config entries required to configure/setup this provider
    config_entries: list[ConfigEntry] = field(default=[CONFIG_ENTRY_ENABLED])
    # requirements: list of (pip style) python packages required for this provider
    requirements: list[str] = field(default_factory=list)
    # documentation: link/url to documentation.
    documentation: str | None = None
    # init_class: class to initialize, within provider's package
    # e.g. `SpotifyProvider`. (autodetect if None)
    init_class: str | None = None
    # multi_instance: whether multiple instances of the same provider are allowed/possible
    multi_instance: bool = True

    # the below attributes are only available for provider instances

    @classmethod
    async def parse_from_manifest(cls: "Provider", manifest_file: str) -> "Provider":
        """Parse Provider from manifest file."""
        manifest_dict = await load_json_file(manifest_file)
        return cls.from_dict(manifest_dict)


@dataclass
class MusicProvider(Provider):
    """Model for a MusicProvider (details)."""

    multi_instance: bool = True


@dataclass
class PlayerProvider(Provider):
    """Model for a PlayerProvider (details)."""

    multi_instance: bool = False


@dataclass
class MetadataProvider(Provider):
    """Model for a MetadataProvider (details)."""

    multi_instance: bool = False


@dataclass
class PluginProvider(Provider):
    """Model for a PluginProvider (details)."""

    multi_instance: bool = False


@dataclass
class ProviderInstance(Provider):
    """Model for an instance of a Provider that is loaded/active in Music Assistant."""

    # instance_id: unique identifier for this provider instance, usually generated at creation
    instance_id: str | None = None
    config: ConfigValues = field(default_factory=dict)


@dataclass
class ProviderInstance(Provider):
    """Model for an instance of a Provider that is loaded/active in Music Assistant."""

    # instance_id: unique identifier for this provider instance, usually generated at creation
    instance_id: str | None = None
    # config: the active config for this provider instance
    config: ConfigValues = field(default_factory=dict)

