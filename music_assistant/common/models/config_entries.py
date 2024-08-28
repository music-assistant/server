"""Model and helpers for Config entries."""

from __future__ import annotations

import logging
import warnings
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from types import NoneType
from typing import Any

from mashumaro import DataClassDictMixin

from music_assistant.common.models.enums import ProviderType
from music_assistant.constants import (
    CONF_ANNOUNCE_VOLUME,
    CONF_ANNOUNCE_VOLUME_MAX,
    CONF_ANNOUNCE_VOLUME_MIN,
    CONF_ANNOUNCE_VOLUME_STRATEGY,
    CONF_AUTO_PLAY,
    CONF_CROSSFADE,
    CONF_CROSSFADE_DURATION,
    CONF_ENABLE_ICY_METADATA,
    CONF_ENFORCE_MP3,
    CONF_EQ_BASS,
    CONF_EQ_MID,
    CONF_EQ_TREBLE,
    CONF_FLOW_MODE,
    CONF_HIDE_PLAYER,
    CONF_HTTP_PROFILE,
    CONF_ICON,
    CONF_LOG_LEVEL,
    CONF_OUTPUT_CHANNELS,
    CONF_SAMPLE_RATES,
    CONF_SYNC_ADJUST,
    CONF_TTS_PRE_ANNOUNCE,
    CONF_VOLUME_NORMALIZATION,
    CONF_VOLUME_NORMALIZATION_TARGET,
    SECURE_STRING_SUBSTITUTE,
)

from .enums import ConfigEntryType

# TEMP: ignore UserWarnings from mashumaro
# https://github.com/Fatal1ty/mashumaro/issues/221
warnings.filterwarnings("ignore", category=UserWarning, module="mashumaro")

LOGGER = logging.getLogger(__name__)

ENCRYPT_CALLBACK: Callable[[str], str] | None = None
DECRYPT_CALLBACK: Callable[[str], str] | None = None

ConfigValueType = (
    str
    | int
    | float
    | bool
    | list[str]
    | list[tuple[int, int]]
    | tuple[int, int]
    | list[int]
    | Enum
    | None
)

ConfigEntryTypeMap: dict[ConfigEntryType, type[ConfigValueType]] = {
    ConfigEntryType.BOOLEAN: bool,
    ConfigEntryType.STRING: str,
    ConfigEntryType.SECURE_STRING: str,
    ConfigEntryType.INTEGER: int,
    ConfigEntryType.INTEGER_TUPLE: tuple[int, int],
    ConfigEntryType.FLOAT: float,
    ConfigEntryType.LABEL: str,
    ConfigEntryType.DIVIDER: str,
    ConfigEntryType.ACTION: str,
    ConfigEntryType.ALERT: str,
    ConfigEntryType.ICON: str,
}

UI_ONLY = (
    ConfigEntryType.LABEL,
    ConfigEntryType.DIVIDER,
    ConfigEntryType.ACTION,
    ConfigEntryType.ALERT,
)


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
    # category: category to group this setting into in the frontend (e.g. advanced)
    category: str = "generic"
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
            if expected_type is float and isinstance(value, int):
                self.value = float(value)
                return self.value
            if expected_type is int and isinstance(value, float):
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
                value = self.default_value
            if not (value is None and allow_none):
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
        if config_value.type == ConfigEntryType.SECURE_STRING and config_value.value:
            assert isinstance(config_value.value, str)
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
            # unpack Enum value in default_value
            if isinstance(entry.default_value, Enum):
                entry.default_value = entry.default_value.value
            # create a copy of the entry
            conf.values[entry.key] = ConfigEntry.from_dict(entry.to_dict())
            conf.values[entry.key].parse_value(
                raw.get("values", {}).get(entry.key), allow_none=True
            )
        return conf

    def to_raw(self) -> dict[str, Any]:
        """Return minimized/raw dict to store in persistent storage."""

        def _handle_value(value: ConfigEntry) -> ConfigValueType:
            if value.type == ConfigEntryType.SECURE_STRING:
                assert isinstance(value.value, str)
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

        for key, new_val in update.items():
            if key in root_values:
                continue
            if key not in self.values:
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
    category="advanced",
)

DEFAULT_PROVIDER_CONFIG_ENTRIES = (CONF_ENTRY_LOG_LEVEL,)
DEFAULT_CORE_CONFIG_ENTRIES = (CONF_ENTRY_LOG_LEVEL,)

# some reusable player config entries

CONF_ENTRY_FLOW_MODE = ConfigEntry(
    key=CONF_FLOW_MODE,
    type=ConfigEntryType.BOOLEAN,
    label="Enable queue flow mode",
    default_value=False,
)

CONF_ENTRY_FLOW_MODE_DEFAULT_ENABLED = ConfigEntry.from_dict(
    {**CONF_ENTRY_FLOW_MODE.to_dict(), "default_value": True}
)

CONF_ENTRY_FLOW_MODE_ENFORCED = ConfigEntry.from_dict(
    {**CONF_ENTRY_FLOW_MODE.to_dict(), "default_value": True, "value": True, "hidden": True}
)

CONF_ENTRY_FLOW_MODE_HIDDEN_DISABLED = ConfigEntry.from_dict(
    {**CONF_ENTRY_FLOW_MODE.to_dict(), "default_value": False, "value": False, "hidden": True}
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
    options=(
        ConfigValueOption("Stereo (both channels)", "stereo"),
        ConfigValueOption("Left channel", "left"),
        ConfigValueOption("Right channel", "right"),
        ConfigValueOption("Mono (both channels)", "mono"),
    ),
    default_value="stereo",
    label="Output Channel Mode",
    category="audio",
)

CONF_ENTRY_VOLUME_NORMALIZATION = ConfigEntry(
    key=CONF_VOLUME_NORMALIZATION,
    type=ConfigEntryType.BOOLEAN,
    label="Enable volume normalization",
    default_value=True,
    description="Enable volume normalization (EBU-R128 based)",
    category="audio",
)

CONF_ENTRY_VOLUME_NORMALIZATION_TARGET = ConfigEntry(
    key=CONF_VOLUME_NORMALIZATION_TARGET,
    type=ConfigEntryType.INTEGER,
    range=(-70, -5),
    default_value=-17,
    label="Target level for volume normalization",
    description="Adjust average (perceived) loudness to this target level",
    depends_on=CONF_VOLUME_NORMALIZATION,
    category="advanced",
)

CONF_ENTRY_EQ_BASS = ConfigEntry(
    key=CONF_EQ_BASS,
    type=ConfigEntryType.INTEGER,
    range=(-10, 10),
    default_value=0,
    label="Equalizer: bass",
    description="Use the builtin basic equalizer to adjust the bass of audio.",
    category="audio",
)

CONF_ENTRY_EQ_MID = ConfigEntry(
    key=CONF_EQ_MID,
    type=ConfigEntryType.INTEGER,
    range=(-10, 10),
    default_value=0,
    label="Equalizer: midrange",
    description="Use the builtin basic equalizer to adjust the midrange of audio.",
    category="audio",
)

CONF_ENTRY_EQ_TREBLE = ConfigEntry(
    key=CONF_EQ_TREBLE,
    type=ConfigEntryType.INTEGER,
    range=(-10, 10),
    default_value=0,
    label="Equalizer: treble",
    description="Use the builtin basic equalizer to adjust the treble of audio.",
    category="audio",
)


CONF_ENTRY_CROSSFADE = ConfigEntry(
    key=CONF_CROSSFADE,
    type=ConfigEntryType.BOOLEAN,
    label="Enable crossfade",
    default_value=False,
    description="Enable a crossfade transition between (queue) tracks.",
    category="audio",
)

CONF_ENTRY_CROSSFADE_FLOW_MODE_REQUIRED = ConfigEntry(
    key=CONF_CROSSFADE,
    type=ConfigEntryType.BOOLEAN,
    label="Enable crossfade",
    default_value=False,
    description="Enable a crossfade transition between (queue) tracks.\n\n "
    "Requires flow-mode to be enabled",
    category="audio",
    depends_on=CONF_FLOW_MODE,
)

CONF_ENTRY_CROSSFADE_DURATION = ConfigEntry(
    key=CONF_CROSSFADE_DURATION,
    type=ConfigEntryType.INTEGER,
    range=(1, 10),
    default_value=8,
    label="Crossfade duration",
    description="Duration in seconds of the crossfade between tracks (if enabled)",
    depends_on=CONF_CROSSFADE,
    category="advanced",
)

CONF_ENTRY_HIDE_PLAYER = ConfigEntry(
    key=CONF_HIDE_PLAYER,
    type=ConfigEntryType.BOOLEAN,
    label="Hide this player in the user interface",
    default_value=False,
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
    category="audio",
)

CONF_ENTRY_ENFORCE_MP3_DEFAULT_ENABLED = ConfigEntry.from_dict(
    {**CONF_ENTRY_ENFORCE_MP3.to_dict(), "default_value": True}
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
    category="advanced",
)


CONF_ENTRY_TTS_PRE_ANNOUNCE = ConfigEntry(
    key=CONF_TTS_PRE_ANNOUNCE,
    type=ConfigEntryType.BOOLEAN,
    default_value=True,
    label="Pre-announce TTS announcements",
    category="announcements",
)


CONF_ENTRY_ANNOUNCE_VOLUME_STRATEGY = ConfigEntry(
    key=CONF_ANNOUNCE_VOLUME_STRATEGY,
    type=ConfigEntryType.STRING,
    options=(
        ConfigValueOption("Absolute volume", "absolute"),
        ConfigValueOption("Relative volume increase", "relative"),
        ConfigValueOption("Volume increase by fixed percentage", "percentual"),
        ConfigValueOption("Do not adjust volume", "none"),
    ),
    default_value="percentual",
    label="Volume strategy for Announcements",
    category="announcements",
)

CONF_ENTRY_ANNOUNCE_VOLUME = ConfigEntry(
    key=CONF_ANNOUNCE_VOLUME,
    type=ConfigEntryType.INTEGER,
    default_value=85,
    label="Volume for Announcements",
    category="announcements",
)

CONF_ENTRY_ANNOUNCE_VOLUME_MIN = ConfigEntry(
    key=CONF_ANNOUNCE_VOLUME_MIN,
    type=ConfigEntryType.INTEGER,
    default_value=15,
    label="Minimum Volume level for Announcements",
    description="The volume (adjustment) of announcements should no go below this level.",
    category="announcements",
)

CONF_ENTRY_ANNOUNCE_VOLUME_MAX = ConfigEntry(
    key=CONF_ANNOUNCE_VOLUME_MAX,
    type=ConfigEntryType.INTEGER,
    default_value=75,
    label="Maximum Volume level for Announcements",
    description="The volume (adjustment) of announcements should no go above this level.",
    category="announcements",
)

CONF_ENTRY_PLAYER_ICON = ConfigEntry(
    key=CONF_ICON,
    type=ConfigEntryType.ICON,
    default_value="mdi-speaker",
    label="Icon",
    description="Material design icon for this player. "
    "\n\nSee https://pictogrammers.com/library/mdi/",
    category="generic",
)

CONF_ENTRY_PLAYER_ICON_GROUP = ConfigEntry.from_dict(
    {**CONF_ENTRY_PLAYER_ICON.to_dict(), "default_value": "mdi-speaker-multiple"}
)

CONF_ENTRY_SAMPLE_RATES = ConfigEntry(
    key=CONF_SAMPLE_RATES,
    type=ConfigEntryType.INTEGER_TUPLE,
    options=(
        ConfigValueOption("44.1kHz / 16 bits", (44100, 16)),
        ConfigValueOption("44.1kHz / 24 bits", (44100, 24)),
        ConfigValueOption("48kHz / 16 bits", (48000, 16)),
        ConfigValueOption("48kHz / 24 bits", (48000, 24)),
        ConfigValueOption("88.2kHz / 16 bits", (88200, 16)),
        ConfigValueOption("88.2kHz / 24 bits", (88200, 24)),
        ConfigValueOption("96kHz / 16 bits", (96000, 16)),
        ConfigValueOption("96kHz / 24 bits", (96000, 24)),
        ConfigValueOption("176.4kHz / 16 bits", (176400, 16)),
        ConfigValueOption("176.4kHz / 24 bits", (176400, 24)),
        ConfigValueOption("192kHz / 16 bits", (192000, 16)),
        ConfigValueOption("192kHz / 24 bits", (192000, 24)),
        ConfigValueOption("352.8kHz / 16 bits", (352800, 16)),
        ConfigValueOption("352.8kHz / 24 bits", (352800, 24)),
        ConfigValueOption("384kHz / 16 bits", (384000, 16)),
        ConfigValueOption("384kHz / 24 bits", (384000, 24)),
    ),
    default_value=[(44100, 16), (48000, 16)],
    required=True,
    multi_value=True,
    label="Sample rates supported by this player",
    category="advanced",
    description="The sample rates (and bit depths) supported by this player.\n"
    "Content with unsupported sample rates will be automatically resampled.",
)


CONF_ENTRY_HTTP_PROFILE = ConfigEntry(
    key=CONF_HTTP_PROFILE,
    type=ConfigEntryType.STRING,
    options=(
        ConfigValueOption("Profile 1 - chunked", "chunked"),
        ConfigValueOption("Profile 2 - no content length", "no_content_length"),
        ConfigValueOption("Profile 3 - forced content length", "forced_content_length"),
    ),
    default_value="chunked",
    label="HTTP Profile used for sending audio",
    category="advanced",
    description="This is considered to be a very advanced setting, only adjust this if needed, "
    "for example if your player stops playing halfway streams or if you experience "
    "other playback related issues. In most cases the default setting, "
    "'chunked transfer encoding', works just fine. \n\n",
)

CONF_ENTRY_HTTP_PROFILE_DEFAULT_2 = ConfigEntry.from_dict(
    {**CONF_ENTRY_HTTP_PROFILE.to_dict(), "default_value": "no_content_length"}
)
CONF_ENTRY_HTTP_PROFILE_FORCED_1 = ConfigEntry.from_dict(
    {**CONF_ENTRY_HTTP_PROFILE.to_dict(), "default_value": "chunked", "hidden": True}
)
CONF_ENTRY_HTTP_PROFILE_FORCED_2 = ConfigEntry.from_dict(
    {**CONF_ENTRY_HTTP_PROFILE.to_dict(), "default_value": "no_content_length", "hidden": True}
)

CONF_ENTRY_ENABLE_ICY_METADATA = ConfigEntry(
    key=CONF_ENABLE_ICY_METADATA,
    type=ConfigEntryType.STRING,
    options=(
        ConfigValueOption("Disabled - do not send ICY metadata", "disabled"),
        ConfigValueOption("Profile 1 - basic info", "basic"),
        ConfigValueOption("Profile 2 - full info (including image)", "full"),
    ),
    depends_on=CONF_FLOW_MODE,
    default_value="basic",
    label="Try to ingest metadata into stream (ICY)",
    category="advanced",
    description="Try to ingest metadata into the stream (ICY) to show track info on the player, "
    "even when flow mode is enabled.\n\nThis is called ICY metadata and its what is also used by "
    "online radio station to inform you what is playing. \n\nBe aware that not all players support "
    "this correctly. If you experience issues with playback, try to disable this setting.",
)


def create_sample_rates_config_entry(
    max_sample_rate: int,
    max_bit_depth: int,
    safe_max_sample_rate: int = 48000,
    safe_max_bit_depth: int = 16,
    hidden: bool = False,
) -> ConfigEntry:
    """Create sample rates config entry based on player specific helpers."""
    assert CONF_ENTRY_SAMPLE_RATES.options
    conf_entry = ConfigEntry.from_dict(CONF_ENTRY_SAMPLE_RATES.to_dict())
    conf_entry.hidden = hidden
    options: list[ConfigValueOption] = []
    default_value: list[tuple[int, int]] = []
    for option in CONF_ENTRY_SAMPLE_RATES.options:
        if not isinstance(option.value, tuple):
            continue
        sample_rate, bit_depth = option.value
        if sample_rate <= max_sample_rate and bit_depth <= max_bit_depth:
            options.append(option)
        if sample_rate <= safe_max_sample_rate and bit_depth <= safe_max_bit_depth:
            default_value.append(option.value)
    conf_entry.options = tuple(options)
    conf_entry.default_value = default_value
    return conf_entry


BASE_PLAYER_CONFIG_ENTRIES = (
    # config entries that are valid for all players
    CONF_ENTRY_PLAYER_ICON,
    CONF_ENTRY_FLOW_MODE,
    CONF_ENTRY_VOLUME_NORMALIZATION,
    CONF_ENTRY_AUTO_PLAY,
    CONF_ENTRY_VOLUME_NORMALIZATION_TARGET,
    CONF_ENTRY_HIDE_PLAYER,
    CONF_ENTRY_TTS_PRE_ANNOUNCE,
    CONF_ENTRY_SAMPLE_RATES,
    CONF_ENTRY_HTTP_PROFILE_FORCED_1,
)
