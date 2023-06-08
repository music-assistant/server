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
    CONF_CROSSFADE_DURATION,
    CONF_EQ_BASS,
    CONF_EQ_MID,
    CONF_EQ_TREBLE,
    CONF_FLOW_MODE,
    CONF_GROUPED_POWER_ON,
    CONF_HIDE_GROUP_CHILDS,
    CONF_LOG_LEVEL,
    CONF_OUTPUT_CHANNELS,
    CONF_OUTPUT_CODEC,
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
            raise ValueError(f"{self.key} has unexpected type: {type(value)}")
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
            conf.values[entry.key].parse_value(raw["values"].get(entry.key), allow_none=True)
        return conf

    def to_raw(self) -> dict[str, Any]:
        """Return minimized/raw dict to store in persistent storage."""

        def _handle_value(value: ConfigEntry):
            if value.type == ConfigEntryType.SECURE_STRING:
                assert ENCRYPT_CALLBACK is not None
                return ENCRYPT_CALLBACK(value.value)
            return value.value

        return {
            **self.to_dict(),
            "values": {
                x.key: _handle_value(x)
                for x in self.values.values()
                if (x.value != x.default_value and x.type not in UI_ONLY)
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

    def update(self, update: dict[str, ConfigValueType]) -> set[str]:
        """Update Config with updated values."""
        changed_keys: set[str] = set()

        # root values (enabled, name)
        root_values = ("enabled", "name")
        for key in root_values:
            cur_val = getattr(self, key)
            if key not in update:
                continue
            new_val = update[key]
            if new_val == cur_val:
                continue
            setattr(self, key, new_val)
            changed_keys.add(key)

        # config entry values
        for key, new_val in update.items():
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


CONF_ENTRY_LOG_LEVEL = ConfigEntry(
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
)

DEFAULT_PROVIDER_CONFIG_ENTRIES = (CONF_ENTRY_LOG_LEVEL,)

# some reusable player config entries

CONF_ENTRY_OUTPUT_CODEC = ConfigEntry(
    key=CONF_OUTPUT_CODEC,
    type=ConfigEntryType.STRING,
    label="Output codec",
    options=[
        ConfigValueOption("FLAC (lossless, compact file size)", "flac"),
        ConfigValueOption("AAC (lossy, superior quality)", "aac"),
        ConfigValueOption("MP3 (lossy, average quality)", "mp3"),
        ConfigValueOption("WAV (lossless, huge file size)", "wav"),
        ConfigValueOption("PCM (lossless, huge file size)", "pcm"),
    ],
    default_value="flac",
    description="Define the codec that is sent to the player when streaming audio. "
    "By default Music Assistant prefers FLAC because it is lossless, has a "
    "respectable filesize and is supported by most player devices. "
    "Change this setting only if needed for your device/environment.",
    advanced=True,
)

CONF_ENTRY_FLOW_MODE = ConfigEntry(
    key=CONF_FLOW_MODE,
    type=ConfigEntryType.BOOLEAN,
    label="Enable queue flow mode",
    default_value=False,
    description='Enable "flow" mode where all queue tracks are sent as a continuous '
    "audio stream. Use for players that do not natively support gapless and/or "
    "crossfading or if the player has trouble transitioning between tracks.",
    advanced=False,
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
    description="You can configure this player to play only the left or right channel, "
    "for example to a create a stereo pair with 2 players.",
    advanced=True,
)

CONF_ENTRY_VOLUME_NORMALIZATION = ConfigEntry(
    key=CONF_VOLUME_NORMALIZATION,
    type=ConfigEntryType.BOOLEAN,
    label="Enable volume normalization (EBU-R128 based)",
    default_value=True,
    description="Enable volume normalization based on the EBU-R128 "
    "standard without affecting dynamic range",
)

CONF_ENTRY_VOLUME_NORMALIZATION_TARGET = ConfigEntry(
    key=CONF_VOLUME_NORMALIZATION_TARGET,
    type=ConfigEntryType.INTEGER,
    range=(-30, 0),
    default_value=-14,
    label="Target level for volume normalization",
    description="Adjust average (perceived) loudness to this target level, " "default is -14 LUFS",
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

CONF_ENTRY_HIDE_GROUP_MEMBERS = ConfigEntry(
    key=CONF_HIDE_GROUP_CHILDS,
    type=ConfigEntryType.STRING,
    options=[
        ConfigValueOption("Always", "always"),
        ConfigValueOption("Only if the group is active/powered", "active"),
        ConfigValueOption("Never", "never"),
    ],
    default_value="active",
    label="Hide playergroup members in UI",
    description="Hide the individual player entry for the members of this group "
    "in the user interface.",
    advanced=False,
)

CONF_ENTRY_CROSSFADE_DURATION = ConfigEntry(
    key=CONF_CROSSFADE_DURATION,
    type=ConfigEntryType.INTEGER,
    range=(0, 12),
    default_value=8,
    label="Crossfade duration",
    description="Duration in seconds of the crossfade between tracks (if enabled)",
    advanced=True,
)

CONF_ENTRY_GROUPED_POWER_ON = ConfigEntry(
    key=CONF_GROUPED_POWER_ON,
    type=ConfigEntryType.BOOLEAN,
    default_value=True,
    label="Forced Power ON of all group members",
    description="Power ON all child players when the group player is powered on "
    "(or playback started). \n"
    "If this setting is disabled, playback will only start on players that "
    "are already powered ON at the time of playback start.\n"
    "When turning OFF the group player, all group members are turned off, "
    "regardless of this setting.",
    advanced=False,
)

DEFAULT_PLAYER_CONFIG_ENTRIES = (
    CONF_ENTRY_VOLUME_NORMALIZATION,
    CONF_ENTRY_FLOW_MODE,
    CONF_ENTRY_OUTPUT_CODEC,
    CONF_ENTRY_VOLUME_NORMALIZATION_TARGET,
    CONF_ENTRY_EQ_BASS,
    CONF_ENTRY_EQ_MID,
    CONF_ENTRY_EQ_TREBLE,
    CONF_ENTRY_OUTPUT_CHANNELS,
)
