"""Model and helpers for Config entries."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from types import NoneType
from typing import Any, Callable, Self

from mashumaro import DataClassDictMixin

from music_assistant.common.models.enums import ProviderType
from music_assistant.constants import (
    CONF_EQ_BASS,
    CONF_EQ_MID,
    CONF_EQ_TREBLE,
    CONF_OUTPUT_CHANNELS,
    CONF_VOLUME_NORMALISATION,
    CONF_VOLUME_NORMALISATION_TARGET,
)

from .enums import ConfigEntryType

LOGGER = logging.getLogger(__name__)

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
        cls,
        entry: ConfigEntry,
        value: ConfigValueType,
        allow_none: bool = False,
    ) -> "ConfigEntryValue":
        """Parse ConfigEntryValue from the config entry and plain value."""
        result = ConfigEntryValue.from_dict(entry.to_dict())
        result.value = value
        expected_type = ConfigEntryTypeMap.get(result.type, NoneType)
        if result.value is None:
            result.value = entry.default_value
        if result.value is None and not entry.required:
            expected_type = NoneType
        if entry.type == ConfigEntryType.LABEL:
            result.value = result.label
        if not isinstance(result.value, expected_type):
            if result.value is None and allow_none:
                # In some cases we allow this (e.g. create default config), hence the allow_none
                return result
            # handle common coversions/mistakes
            if expected_type == float and isinstance(result.value, int):
                result.value = float(result.value)
                return result
            if expected_type == int and isinstance(result.value, float):
                result.value = int(result.value)
                return result
            if entry.default_value:
                LOGGER.warning(
                    "%s has unexpected type: %s, fallback to default",
                    result.key,
                    type(result.value),
                )
                return result
            raise ValueError(f"{result.key} has unexpected type: {type(result.value)}")
        return result


@dataclass
class Config(DataClassDictMixin):
    """Base Configuration object."""

    values: dict[str, ConfigEntryValue]

    def get_value(self, key: str) -> ConfigValueType:
        """Return config value for given key."""
        config_value = self.values[key]
        if config_value.type == ConfigEntryType.PASSWORD:
            if decrypt_callback := self.get_decrypt_callback():
                return decrypt_callback(config_value.value)
        return config_value.value

    @classmethod
    def parse(
        cls: Self,
        config_entries: list[ConfigEntry],
        raw: dict[str, Any],
        allow_none: bool = False,
        decrypt_callback: Callable[[str], str] | None = None
    ) -> Self:
        """Parse Config from the raw values (as stored in persistent storage)."""
        values = {
            x.key: ConfigEntryValue.parse(
                x, raw.get("values", {}).get(x.key), allow_none
            ).to_dict()
            for x in config_entries
        }
        conf = cls.from_dict({**raw, "values": values})
        if decrypt_callback:
            conf.set_decrypt_callback(decrypt_callback)
        return conf

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

    def set_decrypt_callback(self, callback: Callable[[str], str]) -> None:
        """Register callback to decrypt (password) strings."""
        setattr(self, "decrypt_callback", callback)

    def get_decrypt_callback(self) -> Callable[[str], str] | None:
        """Get optional callback to decrypt (password) strings."""
        return getattr(self, "decrypt_callback", None)


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
        key=CONF_EQ_BASS,
        type=ConfigEntryType.INTEGER,
        range=(-10, 10),
        default_value=0,
        label="Equalizer: bass",
        description="Use the builtin basic equalizer to adjust the bass of audio.",
        advanced=True,
    ),
    ConfigEntry(
        key=CONF_EQ_MID,
        type=ConfigEntryType.INTEGER,
        range=(-10, 10),
        default_value=0,
        label="Equalizer: midrange",
        description="Use the builtin basic equalizer to adjust the midrange of audio.",
        advanced=True,
    ),
    ConfigEntry(
        key=CONF_EQ_TREBLE,
        type=ConfigEntryType.INTEGER,
        range=(-10, 10),
        default_value=0,
        label="Equalizer: treble",
        description="Use the builtin basic equalizer to adjust the treble of audio.",
        advanced=True,
    ),
    ConfigEntry(
        key=CONF_OUTPUT_CHANNELS,
        type=ConfigEntryType.STRING,
        options=[
            ConfigValueOption("Stereo (both channels)", "stereo"),
            ConfigValueOption("Left channel", "left"),
            ConfigValueOption("Right channel", "right"),
            ConfigValueOption("Mono (both channels)", "mono"),
        ],
        default_value="stereo",
        label="Output Channel Mode",
        description="You can configure this player to play only the left or right channel, for example to a create a stereo pair with 2 players.",
        advanced=True,
    ),
)
