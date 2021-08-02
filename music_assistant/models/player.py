"""Models and helpers for a player."""

from abc import abstractmethod
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Any, Optional, Set

from mashumaro import DataClassDictMixin
from music_assistant.constants import (
    CONF_ENABLED,
    CONF_NAME,
    CONF_POWER_CONTROL,
    CONF_VOLUME_CONTROL,
    EVENT_PLAYER_CHANGED,
)
from music_assistant.helpers.typing import ConfigSubItem, MusicAssistant, QueueItems
from music_assistant.helpers.util import callback, create_task
from music_assistant.models.config_entry import ConfigEntry


class PlayerState(Enum):
    """Enum for the (playback)state of a player."""

    IDLE = "idle"
    PAUSED = "paused"
    PLAYING = "playing"
    OFF = "off"


@dataclass(frozen=True)
class DeviceInfo(DataClassDictMixin):
    """Model for a player's deviceinfo."""

    model: str = ""
    address: str = ""
    manufacturer: str = ""


class PlayerFeature(IntEnum):
    """Enum for player features."""

    QUEUE = 0
    GAPLESS = 1
    CROSSFADE = 2


class PlayerControlType(Enum):
    """Enum with different player control types."""

    POWER = 0
    VOLUME = 1
    UNKNOWN = 99


@dataclass
class PlayerControl(DataClassDictMixin):
    """
    Model for a player control.

    Allows for a plugin-like
    structure to override common player commands.
    """

    type: PlayerControlType
    control_id: str
    provider: str
    name: str
    state: Any = None

    def __hash__(self):
        """Return custom hash."""
        return hash((self.type, self.provider, self.control_id))

    async def set_state(self, new_state: Any) -> None:
        """Handle command to set the state for a player control."""
        # by default we just signal an event on the eventbus
        # pickup this event (e.g. from the websocket api)
        # or override this method with your own implementation.
        # pylint: disable=no-member
        self.mass.eventbus.signal(
            f"players/controls/{self.control_id}/state", new_state
        )


@dataclass
class CalculatedPlayerState(DataClassDictMixin):
    """Model for a (calculated) player state."""

    player_id: str = None
    provider_id: str = None
    name: str = None
    powered: bool = False
    state: PlayerState = PlayerState.IDLE
    available: bool = False
    volume_level: int = 0
    muted: bool = False
    is_group_player: bool = False
    group_childs: Set[str] = field(default_factory=set)
    device_info: DeviceInfo = field(default_factory=DeviceInfo)
    group_parents: Set[str] = field(default_factory=set)
    features: Set[PlayerFeature] = field(default_factory=set)
    active_queue: str = None

    def __hash__(self):
        """Return custom hash."""
        return hash((self.provider_id, self.player_id))

    def __str__(self):
        """Return string representation, used for logging."""
        return f"{self.name} ({self.provider_id}/{self.player_id})"

    def update(self, new_obj: "PlayerState") -> Set[str]:
        """Update state from other PlayerState instance and return changed keys."""
        changed_keys = set()
        # pylint: disable=no-member
        for key in self.__dataclass_fields__.keys():
            new_val = getattr(new_obj, key)
            if getattr(self, key) != new_val:
                setattr(self, key, new_val)
                changed_keys.add(key)
        return changed_keys


class Player:
    """Model for a music player."""

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
    def state(self) -> PlayerState:
        """Return current PlayerState of player."""
        return PlayerState.IDLE

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
    def group_childs(self) -> Set[str]:
        """Return list of child player id's if player is a group player."""
        return {}

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device info for this player."""
        return DeviceInfo()

    @property
    def should_poll(self) -> bool:
        """Return True if this player should be polled for state updates."""
        return False

    @property
    def features(self) -> Set[PlayerFeature]:
        """Return list of features this player supports."""
        return {}

    @property
    def config_entries(self) -> Set[ConfigEntry]:
        """Return player specific config entries (if any)."""
        return {}

    # Public methods / player commands: should be overriden with provider specific implementation

    async def on_poll(self) -> None:
        """Call when player is periodically polled by the player manager (should_poll=True)."""
        self.update_state()

    async def on_add(self) -> None:
        """Call when player is added to the player manager."""

    async def on_remove(self) -> None:
        """Call when player is removed from the player manager."""

    async def cmd_play_uri(self, uri: str) -> None:
        """
        Play the specified uri/url on the player.

            :param uri: uri/url to send to the player.
        """
        raise NotImplementedError

    async def cmd_stop(self) -> None:
        """Send STOP command to player."""
        raise NotImplementedError

    async def cmd_play(self) -> None:
        """Send PLAY command to player."""
        raise NotImplementedError

    async def cmd_pause(self) -> None:
        """Send PAUSE command to player."""
        raise NotImplementedError

    async def cmd_next(self) -> None:
        """Send NEXT TRACK command to player."""
        raise NotImplementedError

    async def cmd_previous(self) -> None:
        """Send PREVIOUS TRACK command to player."""
        raise NotImplementedError

    async def cmd_power_on(self) -> None:
        """Send POWER ON command to player."""
        raise NotImplementedError

    async def cmd_power_off(self) -> None:
        """Send POWER OFF command to player."""
        raise NotImplementedError

    async def cmd_volume_set(self, volume_level: int) -> None:
        """
        Send volume level command to player.

            :param volume_level: volume level to set (0..100).
        """
        raise NotImplementedError

    async def cmd_volume_mute(self, is_muted: bool = False) -> None:
        """
        Send volume MUTE command to given player.

            :param is_muted: bool with new mute state.
        """
        raise NotImplementedError

    # OPTIONAL: QUEUE SERVICE CALLS/COMMANDS - OVERRIDE ONLY IF SUPPORTED BY PROVIDER

    async def cmd_queue_play_index(self, index: int) -> None:
        """
        Play item at index X on player's queue.

            :param index: (int) index of the queue item that should start playing
        """
        if PlayerFeature.QUEUE in self.features:
            raise NotImplementedError

    async def cmd_queue_load(self, queue_items: QueueItems) -> None:
        """
        Load/overwrite given items in the player's queue implementation.

            :param queue_items: a list of QueueItems
        """
        if PlayerFeature.QUEUE in self.features:
            raise NotImplementedError

    async def cmd_queue_insert(
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

    async def cmd_queue_append(self, queue_items: QueueItems) -> None:
        """
        Append new items at the end of the queue.

            :param queue_items: a list of QueueItems
        """
        if PlayerFeature.QUEUE in self.features:
            raise NotImplementedError

    async def cmd_queue_update(self, queue_items: QueueItems) -> None:
        """
        Overwrite the existing items in the queue, used for reordering.

            :param queue_items: a list of QueueItems
        """
        if PlayerFeature.QUEUE in self.features:
            raise NotImplementedError

    async def cmd_queue_clear(self) -> None:
        """Clear the player's queue."""
        if PlayerFeature.QUEUE in self.features:
            raise NotImplementedError

    # Private properties and methods
    # Do not override below this point!

    @property
    def active_queue(self) -> str:
        """Return the active parent player/queue for a player."""
        return self._calculated_state.active_queue or self.player_id

    @property
    def group_parents(self) -> Set[str]:
        """Return all groups this player belongs to."""
        return self._calculated_state.group_parents

    @property
    def config(self) -> ConfigSubItem:
        """Return this player's configuration."""
        return self.mass.config.get_player_config(self.player_id)

    @property
    def enabled(self):
        """Return True if this player is enabled."""
        return self.config[CONF_ENABLED]

    @property
    def power_control(self) -> Optional[PlayerControl]:
        """Return this player's Power Control."""
        player_control_conf = self.config.get(CONF_POWER_CONTROL)
        if player_control_conf:
            return self.mass.players.get_player_control(player_control_conf)
        return None

    @property
    def volume_control(self) -> Optional[PlayerControl]:
        """Return this player's Volume Control."""
        player_control_conf = self.config.get(CONF_VOLUME_CONTROL)
        if player_control_conf:
            return self.mass.players.get_player_control(player_control_conf)
        return None

    @property
    def calculated_state(self) -> CalculatedPlayerState:
        """Return calculated/final state for this player."""
        return self._calculated_state

    @callback
    def update_state(self) -> None:
        """Call to update current player state in the player manager."""
        if self.mass.exit:
            return
        if not self.added_to_mass:
            if self.enabled:
                # player is now enabled and can be added
                create_task(self.mass.players.add_player(self))
            return
        new_state = self.create_calculated_state()
        changed_keys = self._calculated_state.update(new_state)
        # always update the player queue
        player_queue = self.mass.players.get_player_queue(self.active_queue)
        if player_queue:
            create_task(player_queue.update_state)
        # basic throttle: do not send state changed events if player did not change
        if not changed_keys:
            return
        self._calculated_state = new_state
        self.mass.eventbus.signal(EVENT_PLAYER_CHANGED, new_state)
        # update group player childs when parent updates
        for child_player_id in self.group_childs:
            create_task(self.mass.players.trigger_player_update(child_player_id))
        # update group player when child updates
        for group_player_id in self._calculated_state.group_parents:
            create_task(self.mass.players.trigger_player_update(group_player_id))

    @callback
    def _get_powered(self) -> bool:
        """Return final/calculated player's power state."""
        if not self.available or not self.enabled:
            return False
        power_control = self.power_control
        if power_control:
            return power_control.state
        return self.powered

    @callback
    def _get_state(self, powered: bool, active_queue: str) -> PlayerState:
        """Return final/calculated player's PlayerState."""
        if powered and active_queue != self.player_id:
            # use group state
            return self.mass.players.get_player(active_queue).state
        return PlayerState.OFF if not powered else self.state

    @callback
    def _get_available(self) -> bool:
        """Return current availablity of player."""
        return False if not self.enabled else self.available

    @callback
    def _get_volume_level(self) -> int:
        """Return final/calculated player's volume_level."""
        if not self.available or not self.enabled:
            return 0
        # handle volume control
        volume_control = self.volume_control
        if volume_control:
            return volume_control.state
        # handle group volume
        if self.is_group_player:
            group_volume = 0
            active_players = 0
            for child_player_id in self.group_childs:
                child_player = self.mass.players.get_player(child_player_id)
                if child_player:
                    group_volume += child_player.calculated_state.volume_level
                    active_players += 1
            if active_players:
                group_volume = group_volume / active_players
            return int(group_volume)
        return int(self.volume_level)

    @callback
    def _get_group_parents(self) -> Set[str]:
        """Return all group players this player belongs to."""
        if self.is_group_player:
            return {}
        return {
            player.player_id
            for player in self.mass.players
            if player.is_group_player and self.player_id in player.group_childs
        }

    @callback
    def _get_active_queue(self) -> str:
        """Return the active parent player/queue for a player."""
        # if a group is playing, all of it's childs will have/use
        # the parent's player's queue.
        for group_player_id in self.group_parents:
            group_player = self.mass.players.get_player(group_player_id)
            if group_player and group_player.state in [
                PlayerState.PLAYING,
                PlayerState.PAUSED,
            ]:
                return group_player_id
        return self.player_id

    @callback
    def create_calculated_state(self) -> CalculatedPlayerState:
        """Create CalculatedPlayerState."""
        conf_name = self.config.get(CONF_NAME)
        active_queue = self._get_active_queue()
        powered = self._get_powered()
        return CalculatedPlayerState(
            player_id=self.player_id,
            provider_id=self.provider_id,
            name=conf_name if conf_name else self.name,
            powered=powered,
            state=self._get_state(powered, active_queue),
            available=self._get_available(),
            volume_level=self._get_volume_level(),
            muted=self.muted,
            is_group_player=self.is_group_player,
            group_childs=self.group_childs,
            device_info=self.device_info,
            group_parents=self._get_group_parents(),
            features=self.features,
            active_queue=active_queue,
        )

    def to_dict(self) -> dict:
        """Return playerstate for compatability with json serializer."""
        return self._calculated_state.to_dict()

    def __init__(self, *args, **kwargs) -> None:
        """Initialize a Player instance."""
        self.mass: Optional[MusicAssistant] = None
        self.added_to_mass = False
        self._calculated_state = CalculatedPlayerState()
