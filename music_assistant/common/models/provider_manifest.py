"""Models for providers and plugins in the MA ecosystem."""

from dataclasses import dataclass, field

from mashumaro import DataClassDictMixin

from music_assistant.common.helpers.json import load_json_file

from .config_entries import CONFIG_ENTRY_ENABLED, ConfigEntry
from .enums import ProviderType


@dataclass
class ProviderManifest(DataClassDictMixin):
    """ProviderManifest, details of a provider."""

    type: ProviderType
    domain: str
    name: str
    codeowners: list[str]

    # optional params
    # config_entries: list of config entries required to configure/setup this provider
    config_entries: list[ConfigEntry] = field(default_factory=list)
    # requirements: list of (pip style) python packages required for this provider
    requirements: list[str] = field(default_factory=list)
    # documentation: link/url to documentation.
    documentation: str | None = None
    # init_class: class to initialize, within provider's package
    # e.g. `SpotifyProvider`. (autodetect if None)
    init_class: str | None = None
    # multi_instance: whether multiple instances of the same provider are allowed/possible
    multi_instance: bool = False
    # builtin: whether this provider is a system/builtin and can not disabled/removed
    builtin: bool = False
    # load_by_default: load this provider by default (mostly used together with `builtin`)
    load_by_default: bool = False

    @classmethod
    async def parse(cls: "ProviderManifest", manifest_file: str) -> "ProviderManifest":
        """Parse ProviderManifest from file."""
        manifest_dict = await load_json_file(manifest_file)
        return cls.from_dict(manifest_dict)
