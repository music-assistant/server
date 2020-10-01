"""Models and helpers for a player."""

from abc import abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, List, Optional

from music_assistant.constants import EVENT_SET_PLAYER_CONTROL_STATE
from music_assistant.helpers.typing import MusicAssistantType, QueueItems
from music_assistant.helpers.util import CustomIntEnum, callback
from music_assistant.models.config_entry import ConfigEntry


class PlaybackState(Enum):
    """Enum for the playstate of a player."""

    Stopped = "stopped"
    Paused = "paused"
    Playing = "playing"
    Off = "off"


@dataclass
class DeviceInfo:
    """Model for a player's deviceinfo."""

    model: str = ""
    address: str = ""
    manufacturer: str = ""


class PlayerFeature(CustomIntEnum):
    """Enum for player features."""

    QUEUE = 0
    GAPLESS = 1
    CROSSFADE = 2


class Player:
    """Model for a music player."""

    mass: MusicAssistantType = None  # will be set by player manager

    # Public properties: should be overriden with provider specific implementation

    @property
    @abstractmethod
    def player_id(self) -> str:
        """Return player id of this player."""
        return None

    @property
    @abstractmethod
    def provider_id(self) -> str:
        """Return provider id of this player."""
        return None

    @property
    def name(self) -> str:
        """Return name of the player."""
        return None

    @property
    @abstractmethod
    def powered(self) -> bool:
        """Return current power state of player."""
        return False

    @property
    @abstractmethod
    def elapsed_time(self) -> int:
        """Return elapsed time of current playing media in seconds."""
        return 0

    @property
    def elapsed_milliseconds(self) -> Optional[int]:
        """
        Return elapsed time of current playing media in milliseconds.

        This is an optional property.
        If provided, the property must return the REALTIME value while playing.
        Used for synced playback in player groups.
        """
        return None

    @property
    @abstractmethod
    def state(self) -> PlaybackState:
        """Return current PlaybackState of player."""
        return PlaybackState.Stopped

    @property
    def available(self) -> bool:
        """Return current availablity of player."""
        return True

    @property
    @abstractmethod
    def current_uri(self) -> Optional[str]:
        """Return currently loaded uri of player (if any)."""
        return None

    @property
    @abstractmethod
    def volume_level(self) -> int:
        """Return current volume level of player (scale 0..100)."""
        return 0

    @property
    @abstractmethod
    def muted(self) -> bool:
        """Return current mute state of player."""
        return False

    @property
    @abstractmethod
    def is_group_player(self) -> bool:
        """Return True if this player is a group player."""
        return False

    @property
    def group_childs(self) -> List[str]:
        """Return list of child player id's if player is a group player."""
        return []

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device info for this player."""
        return DeviceInfo()

    @property
    def should_poll(self) -> bool:
        """Return True if this player should be polled for state updates."""
        return False

    @property
    def features(self) -> List[PlayerFeature]:
        """Return list of features this player supports."""
        return []

    @property
    def config_entries(self) -> List[ConfigEntry]:
        """Return player specific config entries (if any)."""
        return []

    # Public methods / player commands: should be overriden with provider specific implementation

    async def async_on_update(self) -> None:
        """Call when player is periodically polled by the player manager (should_poll=True)."""
        self.update_state()

    async def async_on_remove(self) -> None:
        """Call when player is removed from the player manager."""

    async def async_cmd_play_uri(self, uri: str) -> None:
        """
        Play the specified uri/url on the player.

            :param uri: uri/url to send to the player.
        """
        raise NotImplementedError

    async def async_cmd_stop(self) -> None:
        """Send STOP command to player."""
        raise NotImplementedError

    async def async_cmd_play(self) -> None:
        """Send PLAY command to player."""
        raise NotImplementedError

    async def async_cmd_pause(self) -> None:
        """Send PAUSE command to player."""
        raise NotImplementedError

    async def async_cmd_next(self) -> None:
        """Send NEXT TRACK command to player."""
        raise NotImplementedError

    async def async_cmd_previous(self) -> None:
        """Send PREVIOUS TRACK command to player."""
        raise NotImplementedError

    async def async_cmd_power_on(self) -> None:
        """Send POWER ON command to player."""
        raise NotImplementedError

    async def async_cmd_power_off(self) -> None:
        """Send POWER OFF command to player."""
        raise NotImplementedError

    async def async_cmd_volume_set(self, volume_level: int) -> None:
        """
        Send volume level command to player.

            :param volume_level: volume level to set (0..100).
        """
        raise NotImplementedError

    async def async_cmd_volume_mute(self, is_muted: bool = False) -> None:
        """
        Send volume MUTE command to given player.

            :param is_muted: bool with new mute state.
        """
        raise NotImplementedError

    # OPTIONAL: QUEUE SERVICE CALLS/COMMANDS - OVERRIDE ONLY IF SUPPORTED BY PROVIDER

    async def async_cmd_queue_play_index(self, index: int) -> None:
        """
        Play item at index X on player's queue.

            :param index: (int) index of the queue item that should start playing
        """
        if PlayerFeature.QUEUE in self.features:
            raise NotImplementedError

    async def async_cmd_queue_load(self, queue_items: QueueItems) -> None:
        """
        Load/overwrite given items in the player's queue implementation.

            :param queue_items: a list of QueueItems
        """
        if PlayerFeature.QUEUE in self.features:
            raise NotImplementedError

    async def async_cmd_queue_insert(
        self, queue_items: QueueItems, insert_at_index: int
    ) -> None:
        """
        Insert new items at position X into existing queue.

        If insert_at_index 0 or None, will start playing newly added item(s)
            :param queue_items: a list of QueueItems
            :param insert_at_index: queue position to insert new items
        """
        if PlayerFeature.QUEUE in self.features:
            raise NotImplementedError

    async def async_cmd_queue_append(self, queue_items: QueueItems) -> None:
        """
        Append new items at the end of the queue.

            :param queue_items: a list of QueueItems
        """
        if PlayerFeature.QUEUE in self.features:
            raise NotImplementedError

    async def async_cmd_queue_update(self, queue_items: QueueItems) -> None:
        """
        Overwrite the existing items in the queue, used for reordering.

            :param queue_items: a list of QueueItems
        """
        if PlayerFeature.QUEUE in self.features:
            raise NotImplementedError

    async def async_cmd_queue_clear(self) -> None:
        """Clear the player's queue."""
        if PlayerFeature.QUEUE in self.features:
            raise NotImplementedError

    # Do not override below this point

    @callback
    def update_state(self) -> None:
        """Call to store current player state in the player manager."""
        self.mass.add_job(self.mass.players.async_update_player(self))


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
    mass: MusicAssistantType = None  # will be set by player manager

    async def async_set_state(self, new_state: Any) -> None:
        """Handle command to set the state for a player control."""
        # by default we just signal an event on the eventbus
        # pickup this event (e.g. from the websocket api)
        # or override this method with your own implementation.

        self.mass.signal_event(
            EVENT_SET_PLAYER_CONTROL_STATE,
            {"control_id": self.control_id, "state": new_state},
        )

    def to_dict(self) -> dict:
        """Return dict representation of this playercontrol."""
        return {
            "type": int(self.type),
            "control_id": self.control_id,
            "provider": self.provider,
            "name": self.name,
            "state": self.state,
        }
