"""Models and helpers for a player."""

from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import List, Optional

from music_assistant.models.config_entry import ConfigEntry


class PlayerState(str, Enum):
    """Enum for the playstate of a player."""
    Off = "off"
    Stopped = "stopped"
    Paused = "paused"
    Playing = "playing"


@dataclass
class DeviceInfo():
    """Model for a player's deviceinfo."""
    model: Optional[str]
    address: Optional[str]
    manufacturer: Optional[str]


class PlayerFeature(IntEnum):
    """Enum for player features."""
    QUEUE = 0
    GAPLESS = 1
    CROSSFADE = 2


@dataclass
class Player:
    """Model for a MusicPlayer."""
    player_id: str
    provider_id: str
    name: Optional[str]
    powered: bool = True
    elapsed_time: Optional[int]
    state: PlayerState = PlayerState.Off
    available: bool = True
    current_uri: Optional[str]
    volume_level: Optional[int]
    muted: Optional[bool]
    is_group_player: bool = False
    group_childs: List[str] = []
    device_info: Optional[DeviceInfo]
    should_poll: bool = False
    features: List[PlayerFeature] = []
    config_entries: List[ConfigEntry] = []
