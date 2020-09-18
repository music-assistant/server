"""Models and helpers for a player."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, List

from mashumaro import DataClassDictMixin
from music_assistant.constants import EVENT_SET_PLAYER_CONTROL_STATE
from music_assistant.models.config_entry import ConfigEntry
from music_assistant.utils import CustomIntEnum


class PlayerState(Enum):
    """Enum for the playstate of a player."""

    Stopped = "stopped"
    Paused = "paused"
    Playing = "playing"
    Off = "off"


@dataclass
class DeviceInfo(DataClassDictMixin):
    """Model for a player's deviceinfo."""

    model: str = ""
    address: str = ""
    manufacturer: str = ""


class PlayerFeature(CustomIntEnum):
    """Enum for player features."""

    QUEUE = 0
    GAPLESS = 1
    CROSSFADE = 2


@dataclass
class Player(DataClassDictMixin):
    """Model for a MusicPlayer."""

    player_id: str
    provider_id: str
    name: str = ""
    powered: bool = False
    elapsed_time: int = 0
    state: PlayerState = PlayerState.Stopped
    available: bool = True
    current_uri: str = ""
    volume_level: int = 0
    muted: bool = False
    is_group_player: bool = False
    group_childs: List[str] = field(default_factory=list)
    device_info: DeviceInfo = None
    should_poll: bool = False
    features: List[PlayerFeature] = field(default_factory=list)
    config_entries: List[ConfigEntry] = field(default_factory=list)
    # below attributes are handled by the player manager. No need to set/override them.
    updated_at: datetime = field(default=datetime.utcnow(), init=False)
    active_queue: str = field(default="", init=False)
    group_parents: List[str] = field(init=False, default_factory=list)

    def __setattr__(self, name, value):
        """Watch for attribute updates. Do not override."""
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


class PlayerControlType(CustomIntEnum):
    """Enum with different player control types."""

    POWER = 0
    VOLUME = 1
    UNKNOWN = 99


@dataclass
class PlayerControl:
    """
    Model for a player control.

    Allows for a plugin-like
    structure to override common player commands.
    """

    type: PlayerControlType = PlayerControlType.UNKNOWN
    control_id: str = ""
    provider: str = ""
    name: str = ""
    state: Any = None

    async def async_set_state(self, new_state: Any):
        """Handle command to set the state for a player control."""
        # by default we just signal an event on the eventbus
        # pickup this event (e.g. from the websocket api)
        # or override this method with your own implementation.

        # pylint: disable=no-member
        self.mass.signal_event(
            EVENT_SET_PLAYER_CONTROL_STATE,
            {"control_id": self.control_id, "state": new_state},
        )

    def to_dict(self):
        """Return dict representation of this playercontrol."""
        return {
            "type": int(self.type),
            "control_id": self.control_id,
            "provider": self.provider,
            "name": self.name,
            "state": self.state,
        }
