"""Model and helpers for Config entries."""
from __future__ import annotations

from dataclasses import dataclass
from types import NoneType
from typing import Any

from mashumaro import DataClassDictMixin

from music_assistant.common.models.enums import ProviderType
from music_assistant.constants import (
    CONF_CROSSFADE,
    CONF_MAX_SAMPLE_RATE,
    CONF_VOLUME_NORMALISATION,
    CONF_VOLUME_NORMALISATION_TARGET,
)

from .enums import ConfigEntryType

ConfigValueType = str | int | float | bool | None

ConfigEntryTypeMap = {
    ConfigEntryType.BOOLEAN: bool,
    ConfigEntryType.STRING: str,
    ConfigEntryType.PASSWORD: str,
    ConfigEntryType.INTEGER: int,
    ConfigEntryType.FLOAT: float,
    ConfigEntryType.LABEL: str,
}


@dataclass
class ConfigValueOption(DataClassDictMixin):
    """Model for a value with seperated name/value."""

    title: str
    value: ConfigValueType


@dataclass
class ConfigEntry(DataClassDictMixin):
    """
    Model for a Config Entry.

    The definition of something that can be configured for an opbject (e.g. provider or player)
    within Music Assistant (without the value).
    """

    # key: used as identifier for the entry, also for localization
    key: str
    type: ConfigEntryType
    # label: default label when no translation for the key is present
    label: str
    default_value: ConfigValueType = None
    required: bool = True
    # options [optional]: select from list of possible values/options
    options: list[ConfigValueOption] | None = None
    # range [optional]: select values within range
    range: tuple[int, int] | None = None
    # description [optional]: extended description of the setting.
    description: str | None = None
    # help_link [optional]: link to help article.
    help_link: str | None = None
    # multi_value [optional]: allow multiple values from the list
    multi_value: bool = False
    # depends_on [optional]: needs to be set before this setting shows up in frontend
    depends_on: str | None = None
    # hidden: hide from UI
    hidden: bool = False
    # advanced: this is an advanced setting (frontend hides it in some corner)
    advanced: bool = False


@dataclass
class ConfigEntryValue(ConfigEntry):
    """Config Entry with its value parsed."""

    value: ConfigValueType = None

    @classmethod
    def parse(
        cls, entry: ConfigEntry, value: ConfigValueType, allow_none: bool = False
    ) -> "ConfigEntryValue":
        """Parse ConfigEntryValue from the config entry and plain value."""
        result = ConfigEntryValue.from_dict(entry.to_dict())
        result.value = value
        expected_type = ConfigEntryTypeMap.get(result.type, NoneType)
        if result.value is None:
            result.value = entry.default_value
        if result.value is None and not entry.required:
            expected_type = NoneType
        if not isinstance(result.value, expected_type) and not (
            result.value is None and allow_none
        ):
            # NOTE: in some cases we allow this (e.g. create default config), hence the allow_none
            raise ValueError(f"{result.key} has unexpected type: {type(result.value)}")
        return result


@dataclass
class Config(DataClassDictMixin):
    """Base Configuration object."""

    values: dict[str, ConfigEntryValue]

    def get_value(self, key: str) -> ConfigValueType:
        """Return config value for given key."""
        return self.values[key].value

    @classmethod
    def parse(
        cls: object,
        config_entries: list[ConfigEntry],
        raw: dict[str, Any],
        allow_none: bool = False,
    ) -> "Config":
        """Parse Config from the raw values (as stored in persistent storage)."""
        values = {
            x.key: ConfigEntryValue.parse(
                x, raw.get("values", {}).get(x.key), allow_none
            ).to_dict()
            for x in config_entries
        }
        return cls.from_dict({**raw, "values": values})

    def to_raw(self) -> dict[str, Any]:
        """Return minimized/raw dict to store in persistent storage."""
        return {
            **self.to_dict(),
            "values": {
                x.key: x.value
                for x in self.values.values()
                if x.value != x.default_value
            },
        }


@dataclass
class ProviderConfig(Config):
    """Provider(instance) Configuration."""

    type: ProviderType
    domain: str
    instance_id: str
    # enabled: boolean to indicate if the provider is enabled
    enabled: bool = True
    # name: an (optional) custom name for this provider instance/config
    name: str | None = None


@dataclass
class PlayerConfig(Config):
    """Player Configuration."""

    provider: str
    player_id: str
    # enabled: boolean to indicate if the player is enabled
    enabled: bool = True
    # name: an (optional) custom name for this player
    name: str | None = None


DEFAULT_PLAYER_CONFIG_ENTRIES = (
    ConfigEntry(
        key=CONF_VOLUME_NORMALISATION,
        type=ConfigEntryType.BOOLEAN,
        label="Enable volume normalization (EBU-R128 based)",
        default_value=True,
        description="Enable volume normalization based on the EBU-R128 standard without affecting dynamic range",
    ),
    ConfigEntry(
        key=CONF_CROSSFADE,
        type=ConfigEntryType.BOOLEAN,
        label="Enable crossfade between tracks",
        default_value=True,
        description="Enable a crossfade transition between tracks (of different albums)",
    ),
    ConfigEntry(
        key=CONF_VOLUME_NORMALISATION_TARGET,
        type=ConfigEntryType.INTEGER,
        range=(-30, 0),
        default_value=-14,
        label="Target level for volume normalisation",
        description="Adjust average (perceived) loudness to this target level, default is -14 LUFS",
        depends_on=CONF_VOLUME_NORMALISATION,
        advanced=True,
    ),
    ConfigEntry(
        key=CONF_MAX_SAMPLE_RATE,
        type=ConfigEntryType.INTEGER,
        options=[
            ConfigValueOption("44100", 44100),
            ConfigValueOption("48000", 48000),
            ConfigValueOption("88200", 88200),
            ConfigValueOption("96000", 96000),
            ConfigValueOption("176400", 176400),
            ConfigValueOption("192000", 192000),
            ConfigValueOption("352800", 352800),
            ConfigValueOption("384000", 384000),
        ],
        default_value=96000,
        label="Maximum sample rate",
        description="Maximum sample rate that is sent to the player, content with a higher sample rate than this treshold will be downsampled",
        advanced=True,
    ),
)
