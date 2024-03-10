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
    CONF_AUTO_PLAY,
    CONF_CROSSFADE,
    CONF_CROSSFADE_DURATION,
    CONF_ENFORCE_MP3,
    CONF_EQ_BASS,
    CONF_EQ_MID,
    CONF_EQ_TREBLE,
    CONF_FLOW_MODE,
    CONF_HIDE_PLAYER,
    CONF_LOG_LEVEL,
    CONF_OUTPUT_CHANNELS,
    CONF_SYNC_ADJUST,
    CONF_VOLUME_NORMALIZATION,
    CONF_VOLUME_NORMALIZATION_TARGET,
    SECURE_STRING_SUBSTITUTE,
)

from .enums import ConfigEntryType

LOGGER = logging.getLogger(__name__)

ENCRYPT_CALLBACK: callable[[str], str] | None = None
DECRYPT_CALLBACK: callable[[str], str] | None = None

ConfigValueType = str | int | float | bool | list[str] | list[int] | None

ConfigEntryTypeMap = {
    ConfigEntryType.BOOLEAN: bool,
    ConfigEntryType.STRING: str,
    ConfigEntryType.SECURE_STRING: str,
    ConfigEntryType.INTEGER: int,
    ConfigEntryType.FLOAT: float,
    ConfigEntryType.LABEL: str,
    ConfigEntryType.DIVIDER: str,
    ConfigEntryType.ACTION: str,
}

UI_ONLY = (ConfigEntryType.LABEL, ConfigEntryType.DIVIDER, ConfigEntryType.ACTION)


@dataclass
class ConfigValueOption(DataClassDictMixin):
    """Model for a value with separated name/value."""

    title: str
    value: ConfigValueType


@dataclass
class ConfigEntry(DataClassDictMixin):
    """Model for a Config Entry.

    The definition of something that can be configured
    for an object (e.g. provider or player)
    within Music Assistant.
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
    # action: (configentry)action that is needed to get the value for this entry
    action: str | None = None
    # action_label: default label for the action when no translation for the action is present
    action_label: str | None = None
    # value: set by the config manager/flow (or in rare cases by the provider itself)
    value: ConfigValueType = None

    def parse_value(
        self,
        value: ConfigValueType,
        allow_none: bool = True,
    ) -> ConfigValueType:
        """Parse value from the config entry details and plain value."""
        expected_type = list if self.multi_value else ConfigEntryTypeMap.get(self.type, NoneType)
        if value is None:
            value = self.default_value
        if value is None and (not self.required or allow_none):
            expected_type = NoneType
        if self.type == ConfigEntryType.LABEL:
            value = self.label
        if not isinstance(value, expected_type):
            # handle common conversions/mistakes
            if expected_type == float and isinstance(value, int):
                self.value = float(value)
                return self.value
            if expected_type == int and isinstance(value, float):
                self.value = int(value)
                return self.value
            for val_type in (int, float):
                # convert int/float from string
                if expected_type == val_type and isinstance(value, str):
                    try:
                        self.value = val_type(value)
                        return self.value
                    except ValueError:
                        pass
            if self.type in UI_ONLY:
                self.value = self.default_value
                return self.value
            # fallback to default
            if self.default_value is not None:
                LOGGER.warning(
                    "%s has unexpected type: %s, fallback to default",
                    self.key,
                    type(self.value),
                )
                self.value = self.default_value
                return self.value
            msg = f"{self.key} has unexpected type: {type(value)}"
            raise ValueError(msg)
        self.value = value
        return self.value


@dataclass
class Config(DataClassDictMixin):
    """Base Configuration object."""

    values: dict[str, ConfigEntry]

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
        conf = cls.from_dict({**raw, "values": {}})
        for entry in config_entries:
            # create a copy of the entry
            conf.values[entry.key] = ConfigEntry.from_dict(entry.to_dict())
            conf.values[entry.key].parse_value(
                raw.get("values", {}).get(entry.key), allow_none=True
            )
        return conf

    def to_raw(self) -> dict[str, Any]:
        """Return minimized/raw dict to store in persistent storage."""

        def _handle_value(value: ConfigEntry):
            if value.type == ConfigEntryType.SECURE_STRING:
                assert ENCRYPT_CALLBACK is not None
                return ENCRYPT_CALLBACK(value.value)
            return value.value

        res = self.to_dict()
        res["values"] = {
            x.key: _handle_value(x)
            for x in self.values.values()
            if (x.value != x.default_value and x.type not in UI_ONLY)
        }
        return res

    def __post_serialize__(self, d: dict[str, Any]) -> dict[str, Any]:
        """Adjust dict object after it has been serialized."""
        for key, value in self.values.items():
            # drop all password values from the serialized dict
            # API consumers (including the frontend) are not allowed to retrieve it
            # (even if its encrypted) but they can only set it.
            if value.value and value.type == ConfigEntryType.SECURE_STRING:
                d["values"][key]["value"] = SECURE_STRING_SUBSTITUTE
        return d

    def update(self, update: dict[str, ConfigValueType]) -> set[str]:
        """Update Config with updated values."""
        changed_keys: set[str] = set()

        # root values (enabled, name)
        root_values = ("enabled", "name")
        for key in root_values:
            if key not in update:
                continue
            cur_val = getattr(self, key)
            new_val = update[key]
            if new_val == cur_val:
                continue
            setattr(self, key, new_val)
            changed_keys.add(key)

        # config entry values
        values = update.get("values", update)
        for key, new_val in values.items():
            if key in root_values:
                continue
            cur_val = self.values[key].value if key in self.values else None
            # parse entry to do type validation
            parsed_val = self.values[key].parse_value(new_val)
            if cur_val != parsed_val:
                changed_keys.add(f"values/{key}")

        return changed_keys

    def validate(self) -> None:
        """Validate if configuration is valid."""
        # For now we just use the parse method to check for not allowed None values
        # this can be extended later
        for value in self.values.values():
            value.parse_value(value.value, allow_none=False)


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
    # default_name: default name to use when there is no name available
    default_name: str | None = None


@dataclass
class CoreConfig(Config):
    """CoreController Configuration."""

    domain: str  # domain/name of the core module
    # last_error: an optional error message if the module could not be setup with this config
    last_error: str | None = None


CONF_ENTRY_LOG_LEVEL = ConfigEntry(
    key=CONF_LOG_LEVEL,
    type=ConfigEntryType.STRING,
    label="Log level",
    options=(
        ConfigValueOption("global", "GLOBAL"),
        ConfigValueOption("info", "INFO"),
        ConfigValueOption("warning", "WARNING"),
        ConfigValueOption("error", "ERROR"),
        ConfigValueOption("debug", "DEBUG"),
        ConfigValueOption("verbose", "VERBOSE"),
    ),
    default_value="GLOBAL",
    advanced=True,
)

DEFAULT_PROVIDER_CONFIG_ENTRIES = (CONF_ENTRY_LOG_LEVEL,)
DEFAULT_CORE_CONFIG_ENTRIES = (CONF_ENTRY_LOG_LEVEL,)

# some reusable player config entries

CONF_ENTRY_FLOW_MODE = ConfigEntry(
    key=CONF_FLOW_MODE,
    type=ConfigEntryType.BOOLEAN,
    label="Enable queue flow mode",
    default_value=False,
    advanced=False,
)


CONF_ENTRY_AUTO_PLAY = ConfigEntry(
    key=CONF_AUTO_PLAY,
    type=ConfigEntryType.BOOLEAN,
    label="Automatically play/resume on power on",
    default_value=False,
    description="When this player is turned ON, automatically start playing "
    "(if there are items in the queue).",
)

CONF_ENTRY_OUTPUT_CHANNELS = ConfigEntry(
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
    advanced=True,
)

CONF_ENTRY_VOLUME_NORMALIZATION = ConfigEntry(
    key=CONF_VOLUME_NORMALIZATION,
    type=ConfigEntryType.BOOLEAN,
    label="Enable volume normalization",
    default_value=True,
    description="Enable volume normalization (EBU-R128 based)",
)

CONF_ENTRY_VOLUME_NORMALIZATION_TARGET = ConfigEntry(
    key=CONF_VOLUME_NORMALIZATION_TARGET,
    type=ConfigEntryType.INTEGER,
    range=(-30, 0),
    default_value=-17,
    label="Target level for volume normalization",
    description="Adjust average (perceived) loudness to this target level",
    depends_on=CONF_VOLUME_NORMALIZATION,
    advanced=True,
)

CONF_ENTRY_EQ_BASS = ConfigEntry(
    key=CONF_EQ_BASS,
    type=ConfigEntryType.INTEGER,
    range=(-10, 10),
    default_value=0,
    label="Equalizer: bass",
    description="Use the builtin basic equalizer to adjust the bass of audio.",
    advanced=True,
)

CONF_ENTRY_EQ_MID = ConfigEntry(
    key=CONF_EQ_MID,
    type=ConfigEntryType.INTEGER,
    range=(-10, 10),
    default_value=0,
    label="Equalizer: midrange",
    description="Use the builtin basic equalizer to adjust the midrange of audio.",
    advanced=True,
)

CONF_ENTRY_EQ_TREBLE = ConfigEntry(
    key=CONF_EQ_TREBLE,
    type=ConfigEntryType.INTEGER,
    range=(-10, 10),
    default_value=0,
    label="Equalizer: treble",
    description="Use the builtin basic equalizer to adjust the treble of audio.",
    advanced=True,
)


CONF_ENTRY_CROSSFADE = ConfigEntry(
    key=CONF_CROSSFADE,
    type=ConfigEntryType.BOOLEAN,
    label="Enable crossfade",
    default_value=False,
    description="Enable a crossfade transition between (queue) tracks.",
    advanced=False,
)

CONF_ENTRY_CROSSFADE_DURATION = ConfigEntry(
    key=CONF_CROSSFADE_DURATION,
    type=ConfigEntryType.INTEGER,
    range=(1, 10),
    default_value=8,
    label="Crossfade duration",
    description="Duration in seconds of the crossfade between tracks (if enabled)",
    depends_on=CONF_CROSSFADE,
    advanced=True,
)

CONF_ENTRY_HIDE_PLAYER = ConfigEntry(
    key=CONF_HIDE_PLAYER,
    type=ConfigEntryType.BOOLEAN,
    label="Hide this player in the user interface",
    default_value=False,
    advanced=True,
)

CONF_ENTRY_ENFORCE_MP3 = ConfigEntry(
    key=CONF_ENFORCE_MP3,
    type=ConfigEntryType.BOOLEAN,
    label="Enforce (lossy) mp3 stream",
    default_value=False,
    description="By default, Music Assistant sends lossless, high quality audio "
    "to all players. Some players can not deal with that and require the stream to be packed "
    "into a lossy mp3 codec. \n\n "
    "Only enable when needed. Saves some bandwidth at the cost of audio quality.",
    advanced=True,
)

CONF_ENTRY_SYNC_ADJUST = ConfigEntry(
    key=CONF_SYNC_ADJUST,
    type=ConfigEntryType.INTEGER,
    range=(-500, 500),
    default_value=0,
    label="Audio synchronization delay correction",
    description="If this player is playing audio synced with other players "
    "and you always hear the audio too early or late on this player, "
    "you can shift the audio a bit.",
    advanced=True,
)
