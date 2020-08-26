"""Models and helpers for a player."""

from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Awaitable, List, Optional

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
    name: str = ""
    powered: bool = True
    elapsed_time: int = 0
    state: PlayerState = PlayerState.Off
    available: bool = True
    current_uri: Optional[str] = None
    volume_level: int = 0
    muted: bool = False
    is_group_player: bool = False
    group_childs: List[str] = field(default_factory=list)
    device_info: Optional[DeviceInfo] = None
    should_poll: bool = False
    features: List[PlayerFeature] = field(default_factory=list)
    config_entries: List[ConfigEntry] = field(default_factory=list)

