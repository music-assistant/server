"""Models and helpers for a player."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Awaitable, Callable, List, Optional, Union

from music_assistant.models.config_entry import ConfigEntry
from music_assistant.constants import EVENT_PLAYER_CONTROL_UPDATED, EVENT_PLAYER_CHANGED


class PlayerState(str, Enum):
    """Enum for the playstate of a player."""

    Off = "off"
    Stopped = "stopped"
    Paused = "paused"
    Playing = "playing"


@dataclass
class DeviceInfo:
    """Model for a player's deviceinfo."""

    model: Optional[str]
    address: Optional[str]
    manufacturer: Optional[str]


class PlayerFeature(int, Enum):
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
    powered: bool = False
    elapsed_time: int = 0
    state: PlayerState = PlayerState.Off
    available: bool = True
    current_uri: str = ""
    volume_level: int = 0
    muted: bool = False
    is_group_player: bool = False
    group_childs: List[str] = field(default_factory=list)
    device_info: Optional[DeviceInfo] = None
    should_poll: bool = False
    features: List[PlayerFeature] = field(default_factory=list)
    config_entries: List[ConfigEntry] = field(default_factory=list)
    updated_at: datetime = datetime.utcnow()  # managed by playermanager!
    active_queue: str = ""  # managed by playermanager!
    group_parents: List[str] = field(default_factory=list) # managed by playermanager!
    cur_queue_item_id: str = None # managed by playermanager!

    def __setattr__(self, name, value):
        """Event when control is updated. Do not override"""
        if name == "updated_at":
            # updated at is set by the on_update callback
            # make sure we do not hit an endless loop
            super().__setattr__(name, value)
            return
        value_changed = hasattr(self, name) and getattr(self, name) != value
        super().__setattr__(name, value)
        if value_changed and hasattr(self, "_on_update"):
            # pylint: disable=no-member
            self._on_update(self.player_id, name)


class PlayerControlType(int, Enum):
    """Enum with different player control types."""

    POWER = 0
    VOLUME = 1
    UNKNOWN = 99


@dataclass
class PlayerControl:
    """Model for a player control which allows for a
    plugin-like structure to override common player commands."""

    type: PlayerControlType = PlayerControlType.UNKNOWN
    id: str = ""
    name: str = ""
    state: Optional[Any] = None
    set_state: Callable[..., Union[None, Awaitable]] = None
