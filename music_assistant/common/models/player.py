"""Model(s) for Player."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from mashumaro import DataClassDictMixin

from music_assistant.constants import (
    CONF_CROSSFADE,
    CONF_MAX_SAMPLE_RATE,
    CONF_VOLUME_NORMALISATION,
    CONF_VOLUME_NORMALISATION_TARGET,
)

from .config_entries import (
    ConfigEntry,
    ConfigEntryType,
    ConfigEntryValue,
    ConfigValueOption,
)
from .enums import PlayerFeature, PlayerState, PlayerType

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
        type=ConfigEntryType.INT,
        range=(-30, 0),
        default_value=-14,
        label="Target level for volume normalisation",
        description="Adjust average (perceived) loudness to this target level, default is -14 LUFS",
        depends_on=CONF_VOLUME_NORMALISATION,
        advanced=True,
    ),
    ConfigEntry(
        key=CONF_MAX_SAMPLE_RATE,
        type=ConfigEntryType.INT,
        options=(
            ConfigValueOption("44100", 44100),
            ConfigValueOption("48000", 48000),
            ConfigValueOption("88200", 88200),
            ConfigValueOption("96000", 96000),
            ConfigValueOption("176400", 176400),
            ConfigValueOption("192000", 192000),
            ConfigValueOption("352800", 352800),
            ConfigValueOption("384000", 384000),
        ),
        default_value=96000,
        label="Maximum sample rate",
        description="Maximum sample rate that is sent to the player, content with a higher sample rate than this treshold will be downsampled",
        advanced=True,
    ),
)


def default_config_entries() -> list(ConfigEntry):
    """Return default Player config entries to attach to Player object."""


@dataclass(frozen=True)
class DeviceInfo(DataClassDictMixin):
    """Model for a player's deviceinfo."""

    model: str = "unknown"
    address: str = "unknown"
    manufacturer: str = "unknown"


@dataclass
class Player(DataClassDictMixin):
    """Representation of a Player within Music Assistant."""

    player_id: str
    provider: str
    type: PlayerType
    name: str
    available: bool
    powered: bool
    device_info: DeviceInfo
    supported_features: tuple[PlayerFeature] = field(default_factory=tuple)

    elapsed_time: float = 0
    elapsed_time_last_updated: float = time.time()
    current_url: str = ""
    state: PlayerState = PlayerState.IDLE

    volume_level: int = 100
    volume_muted: bool = False

    # group_childs: Return list of player group child id's or synced childs.
    # - If this player is a dedicated group player,
    #   returns all child id's of the players in the group.
    # - If this is a syncgroup of players from the same platform (e.g. sonos),
    #   this will return the id's of players synced to this player.
    group_childs: list[str] = field(default_factory=list)

    # active_queue: return player_id of the active queue for this player
    # if the player is grouped and a group is active, this will be set to the group's player_id
    # otherwise it will be set to the own player_id
    active_queue: str = ""

    # can_sync_with: return list of player_ids that can be synced to/with this player
    # ususally this is just a list of all player_ids within the playerprovider
    can_sync_with: tuple[str] = field(default_factory=tuple)

    # synced_to: plauyer_id of the player this player is currently sunced to
    # also referred to as "sync master"
    synced_to: str | None = None

    # max_sample_rate: maximum supported sample rate the player supports
    max_sample_rate: int = 96000

    # config entries: all config options to configure the player
    # the default set can be extended by the playerprovider if needed
    config_entries: list[ConfigEntry] = field(default_factory=default_config_entries)

    # enabled: if the player is enabled
    # will be set by the player manager based on config
    # a disabled player is hidden in the UI and updates will not be processed
    enabled: bool = True

    @property
    def corrected_elapsed_time(self) -> float:
        """Return the corrected/realtime elapsed time."""
        if self.state == PlayerState.PLAYING:
            return self.elapsed_time + (time.time() - self.elapsed_time_last_updated)
        return self.elapsed_time
