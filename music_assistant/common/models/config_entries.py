"""Model and helpers for Config entries."""
from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from types import NoneType
from typing import Any

from mashumaro import DataClassDictMixin

from music_assistant.common.models.enums import ProviderType
from music_assistant.constants import (
    CONF_EQ_BASS,
    CONF_EQ_MID,
    CONF_EQ_TREBLE,
    CONF_FLOW_MODE,
    CONF_LOG_LEVEL,
    CONF_OUTPUT_CHANNELS,
    CONF_VOLUME_NORMALISATION,
    CONF_VOLUME_NORMALISATION_TARGET,
    SECURE_STRING_SUBSTITUTE,
)

from .enums import ConfigEntryType

LOGGER = logging.getLogger(__name__)

ENCRYPT_CALLBACK: callable[[str], str] | None = None
DECRYPT_CALLBACK: callable[[str], str] | None = None

ConfigValueType = str | int | float | bool | None

ConfigEntryTypeMap = {
    ConfigEntryType.BOOLEAN: bool,
    ConfigEntryType.STRING: str,
    ConfigEntryType.SECURE_STRING: str,
    ConfigEntryType.INTEGER: int,
    ConfigEntryType.FLOAT: float,
    ConfigEntryType.LABEL: str,
}


@dataclass
class ConfigValueOption(DataClassDictMixin):
    """Model for a value with separated name/value."""

    title: str
    value: ConfigValueType


@dataclass
class ConfigEntry(DataClassDictMixin):
    """Model for a Config Entry.

    The definition of something that can be configured for an object (e.g. provider or player)
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
    options: tuple[ConfigValueOption, ...] | None = None
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
    # encrypt: store string value encrypted and do not send its value in the api
    encrypt: bool = False


@dataclass
class ConfigEntryValue(ConfigEntry):
    """Config Entry with its value parsed."""

    value: ConfigValueType = None

    @classmethod
    def parse(
        cls,
        entry: ConfigEntry,
        value: ConfigValueType,
        allow_none: bool = True,
    ) -> ConfigEntryValue:
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
            # handle common conversions/mistakes
            if expected_type == float and isinstance(result.value, int):
                result.value = float(result.value)
                return result
            if expected_type == int and isinstance(result.value, float):
                result.value = int(result.value)
                return result
            if expected_type == int and isinstance(result.value, str) and result.value.isnumeric():
                result.value = int(result.value)
                return result
            if (
                expected_type == float
                and isinstance(result.value, str)
                and result.value.isnumeric()
            ):
                result.value = float(result.value)
                return result
            # fallback to default
            if result.value is None and allow_none:
                # In some cases we allow this (e.g. create default config)
                result.value = None
                return result
            if entry.default_value:
                LOGGER.warning(
                    "%s has unexpected type: %s, fallback to default",
                    result.key,
                    type(result.value),
                )
                result.value = entry.default_value
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
        if config_value.type == ConfigEntryType.SECURE_STRING:
            assert DECRYPT_CALLBACK is not None
            return DECRYPT_CALLBACK(config_value.value)
        return config_value.value

    @classmethod
    def parse(
        cls,
        config_entries: Iterable[ConfigEntry],
        raw: dict[str, Any],
    ) -> Config:
        """Parse Config from the raw values (as stored in persistent storage)."""
        values = {
            x.key: ConfigEntryValue.parse(x, raw.get("values", {}).get(x.key)).to_dict()
            for x in config_entries
        }
        conf = cls.from_dict({**raw, "values": values})
        return conf

    def to_raw(self) -> dict[str, Any]:
        """Return minimized/raw dict to store in persistent storage."""

        def _handle_value(value: ConfigEntryValue):
            if value.type == ConfigEntryType.SECURE_STRING:
                assert ENCRYPT_CALLBACK is not None
                return ENCRYPT_CALLBACK(value.value)
            return value.value

        return {
            **self.to_dict(),
            "values": {
                x.key: _handle_value(x) for x in self.values.values() if x.value != x.default_value
            },
        }

    def __post_serialize__(self, d: dict[str, Any]) -> dict[str, Any]:
        """Adjust dict object after it has been serialized."""
        for key, value in self.values.items():
            # drop all password values from the serialized dict
            # API consumers (including the frontend) are not allowed to retrieve it
            # (even if its encrypted) but they can only set it.
            if value.value and value.type == ConfigEntryType.SECURE_STRING:
                d["values"][key]["value"] = SECURE_STRING_SUBSTITUTE
        return d

    def update(self, update: ConfigUpdate) -> set[str]:
        """Update Config with updated values."""
        changed_keys: set[str] = set()

        # root values (enabled, name)
        for key in ("enabled", "name"):
            cur_val = getattr(self, key, None)
            new_val = getattr(update, key, None)
            if new_val is None:
                continue
            if new_val == cur_val:
                continue
            setattr(self, key, new_val)
            changed_keys.add(key)

        # update values
        if update.values is not None:
            for key, new_val in update.values.items():
                cur_val = self.values[key].value
                if cur_val == new_val:
                    continue
                if new_val is None:
                    self.values[key].value = self.values[key].default_value
                else:
                    self.values[key].value = new_val
                changed_keys.add(f"values/{key}")

        return changed_keys

    def validate(self) -> None:
        """Validate if configuration is valid."""
        # For now we just use the parse method to check for not allowed None values
        # this can be extended later
        for value in self.values.values():
            value.parse(value, value.value, allow_none=False)


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
    # last_error: an optional error message if the provider could not be setup with this config
    last_error: str | None = None


@dataclass
class PlayerConfig(Config):
    """Player Configuration."""

    provider: str
    player_id: str
    # enabled: boolean to indicate if the player is enabled
    enabled: bool = True
    # name: an (optional) custom name for this player
    name: str | None = None
    # available: boolean to indicate if the player is available
    available: bool = True
    # default_name: default name to use when there is name available
    default_name: str | None = None


@dataclass
class ConfigUpdate(DataClassDictMixin):
    """Config object to send when updating some/all values through the API."""

    enabled: bool | None = None
    name: str | None = None
    values: dict[str, ConfigValueType] | None = None


DEFAULT_PROVIDER_CONFIG_ENTRIES = (
    ConfigEntry(
        key=CONF_LOG_LEVEL,
        type=ConfigEntryType.STRING,
        label="Log level",
        options=[
            ConfigValueOption("global", "GLOBAL"),
            ConfigValueOption("info", "INFO"),
            ConfigValueOption("warning", "WARNING"),
            ConfigValueOption("error", "ERROR"),
            ConfigValueOption("debug", "DEBUG"),
        ],
        default_value="GLOBAL",
        description="Set the log verbosity for this provider",
        advanced=True,
    ),
)


DEFAULT_PLAYER_CONFIG_ENTRIES = (
    ConfigEntry(
        key=CONF_VOLUME_NORMALISATION,
        type=ConfigEntryType.BOOLEAN,
        label="Enable volume normalization (EBU-R128 based)",
        default_value=True,
        description="Enable volume normalization based on the EBU-R128 "
        "standard without affecting dynamic range",
    ),
    ConfigEntry(
        key=CONF_FLOW_MODE,
        type=ConfigEntryType.BOOLEAN,
        label="Enable queue flow mode",
        default_value=False,
        description='Enable "flow" mode where all queue tracks are sent as a continuous '
        "audio stream. Use for players that do not natively support gapless and/or "
        "crossfading or if the player has trouble transitioning between tracks.",
        advanced=True,
    ),
    ConfigEntry(
        key=CONF_VOLUME_NORMALISATION_TARGET,
        type=ConfigEntryType.INTEGER,
        range=(-30, 0),
        default_value=-14,
        label="Target level for volume normalisation",
        description="Adjust average (perceived) loudness to this target level, "
        "default is -14 LUFS",
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
        description="You can configure this player to play only the left or right channel, "
        "for example to a create a stereo pair with 2 players.",
        advanced=True,
    ),
)
