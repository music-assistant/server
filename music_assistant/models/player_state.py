"""
Models and helpers for the calculated state of a player.

PlayerProviders send Player objects to us with the raw/untouched player state.
Due to configuration settings and other influences this playerstate needs alteration,
that's why we store the final player state (we present to outside world)
into a PlayerState object.
"""

import logging
from datetime import datetime
from typing import List, Optional

from music_assistant.constants import (
    CONF_ENABLED,
    CONF_GROUP_DELAY,
    CONF_NAME,
    CONF_POWER_CONTROL,
    CONF_VOLUME_CONTROL,
    EVENT_PLAYER_CHANGED,
)
from music_assistant.helpers.typing import MusicAssistantType
from music_assistant.models.config_entry import ConfigEntry, ConfigEntryType
from music_assistant.models.player import (
    DeviceInfo,
    PlaybackState,
    Player,
    PlayerControlType,
    PlayerFeature,
)
from music_assistant.utils import callback

LOGGER = logging.getLogger("mass")

ATTR_PLAYER_ID = "player_id"
ATTR_PROVIDER_ID = "provider_id"
ATTR_NAME = "name"
ATTR_POWERED = "powered"
ATTR_ELAPSED_TIME = "elapsed_time"
ATTR_STATE = "state"
ATTR_AVAILABLE = "available"
ATTR_CURRENT_URI = "current_uri"
ATTR_VOLUME_LEVEL = "volume_level"
ATTR_MUTED = "muted"
ATTR_IS_GROUP_PLAYER = "is_group_player"
ATTR_GROUP_CHILDS = "group_childs"
ATTR_DEVICE_INFO = "device_info"
ATTR_SHOULD_POLL = "should_poll"
ATTR_FEATURES = "features"
ATTR_CONFIG_ENTRIES = "config_entries"
ATTR_UPDATED_AT = "updated_at"
ATTR_ACTIVE_QUEUE = "active_queue"
ATTR_GROUP_PARENTS = "group_parents"


# list of Player attributes that can/will cause a player changed event
UPDATE_ATTRIBUTES = [
    ATTR_NAME,
    ATTR_POWERED,
    ATTR_STATE,
    ATTR_AVAILABLE,
    ATTR_CURRENT_URI,
    ATTR_VOLUME_LEVEL,
    ATTR_MUTED,
    ATTR_IS_GROUP_PLAYER,
    ATTR_GROUP_CHILDS,
    ATTR_DEVICE_INFO,
    ATTR_FEATURES,
]


class PlayerState:
    """
    Model for the calculated state of a player.

    PlayerProviders send Player objects to us with the raw/untouched player state.
    Due to configuration settings and other influences this playerstate needs alteration,
    that's why we store the final player state (we present to outside world)
    into this PlayerState object.
    """

    def __init__(self, mass: MusicAssistantType, player: Player):
        """Initialize a PlayerState from a Player object."""
        self.mass = mass
        # make sure the MusicAssistant obj is present on the player
        player.mass = mass
        self._player = player
        self._player_id = player.player_id
        self._provider_id = player.provider_id
        self._features = player.features
        self._muted = player.muted
        self._is_group_player = player.is_group_player
        self._group_childs = player.group_childs
        self._device_info = player.device_info
        self._elapsed_time = player.elapsed_time
        self._current_uri = player.current_uri
        self._available = player.available
        self._name = player.name
        self._powered = player.powered
        self._state = player.state
        self._volume_level = player.volume_level
        self._updated_at = datetime.utcnow()
        self._group_parents = self.get_group_parents()
        self._active_queue = self.get_active_queue()
        self._config_entries = self.get_player_config_entries()
        # schedule update to set the transforms
        self.mass.add_job(self.async_update(player))

    @property
    def player(self):
        """Return the underlying player object."""
        return self._player

    @property
    def player_id(self) -> str:
        """Return player id of this player."""
        return self._player_id

    @property
    def provider_id(self) -> str:
        """Return provider id of this player."""
        return self._provider_id

    @property
    def name(self) -> str:
        """Return name of the player."""
        return self._name

    @property
    def powered(self) -> bool:
        """Return current power state of player."""
        return self._powered

    @property
    def elapsed_time(self) -> int:
        """Return elapsed time of current playing media in seconds."""
        return self._elapsed_time

    @property
    def elapsed_milliseconds(self) -> Optional[int]:
        """
        Return elapsed time of current playing media in milliseconds.

        This is an optional property.
        If provided, the property must return the REALTIME value while playing.
        Used for synced playback in player groups.
        """
        return self.player.elapsed_milliseconds  # always realtime returned from player

    @property
    def state(self) -> PlaybackState:
        """Return current PlaybackState of player."""
        return self._state

    @property
    def available(self) -> bool:
        """Return current availablity of player."""
        return self._available

    @property
    def current_uri(self) -> Optional[str]:
        """Return currently loaded uri of player (if any)."""
        return self._current_uri

    @property
    def volume_level(self) -> int:
        """Return current volume level of player (scale 0..100)."""
        return self._volume_level

    @property
    def muted(self) -> bool:
        """Return current mute state of player."""
        return self._muted

    @property
    def is_group_player(self) -> bool:
        """Return True if this player is a group player."""
        return self._is_group_player

    @property
    def group_childs(self) -> List[str]:
        """Return list of child player id's if player is a group player."""
        return self._group_childs

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device info for this player."""
        return self._device_info

    @property
    def should_poll(self) -> bool:
        """Return True if this player should be polled for state updates."""
        return self._player.should_poll  # always realtime returned from player

    @property
    def features(self) -> List[PlayerFeature]:
        """Return list of features this player supports."""
        return self._features

    @property
    def config_entries(self) -> List[ConfigEntry]:
        """Return player specific config entries (if any)."""
        return self._config_entries

    async def async_update(self, player: Player):
        """Update attributes from player object."""
        # detect state changes
        changed_keys = set()
        for attr in UPDATE_ATTRIBUTES:
            new_value = getattr(self._player, attr)

            # handle transformations
            if attr == ATTR_NAME:
                new_value = self.get_name(new_value)
            elif attr == ATTR_POWERED:
                new_value = self.get_power(new_value)
            elif attr == ATTR_STATE:
                new_value = self.get_state(new_value)
            elif attr == ATTR_AVAILABLE:
                new_value = self.get_available(new_value)
            elif attr == ATTR_VOLUME_LEVEL:
                new_value = self.get_volume_level(new_value)

            current_value = getattr(self, attr)

            if current_value != new_value:
                # value changed
                setattr(self, "_" + attr, new_value)
                changed_keys.add(attr)
                LOGGER.debug("Attribute %s changed on player %s", attr, self.player_id)

        # some attributes are always updated
        self._elapsed_time = player.elapsed_time
        self._updated_at = datetime.utcnow()
        self._group_parents = self.get_group_parents()
        self._active_queue = self.get_active_queue()
        self._config_entries = self.get_player_config_entries()

        if changed_keys:
            self.mass.signal_event(EVENT_PLAYER_CHANGED, self)
            # update group player childs when parent updates
            if ATTR_GROUP_CHILDS in changed_keys:
                for child_player_id in self.group_childs:
                    self.mass.add_job(
                        self.mass.player_manager.async_trigger_player_update(
                            child_player_id
                        )
                    )

        # always update the player queue
        player_queue = self.mass.player_manager.get_player_queue(self.active_queue)
        if player_queue:
            self.mass.add_job(player_queue.async_update_state())

    @callback
    def get_name(self, name: str) -> str:
        """Return final/calculated player name."""
        conf_name = self.mass.config.get_player_config(self.player_id)[CONF_NAME]
        return conf_name if conf_name else name

    @callback
    def get_power(self, power: bool) -> bool:
        """Return final/calculated player's power state."""
        if not self.available:
            return False
        player_config = self.mass.config.player_settings[self.player_id]
        if player_config.get(CONF_POWER_CONTROL):
            control = self.mass.player_manager.get_player_control(
                player_config[CONF_POWER_CONTROL]
            )
            if control:
                return control.state
        return power

    @callback
    def get_state(self, state: PlaybackState) -> PlaybackState:
        """Return final/calculated player's playback state."""
        if self.powered and self.active_queue != self.player_id:
            # use group state
            return self.mass.player_manager.get_player(self.active_queue).state
        if state == PlaybackState.Stopped and not self.powered:
            return PlaybackState.Off
        return state

    @callback
    def get_available(self, available: bool) -> bool:
        """Return current availablity of player."""
        player_enabled = bool(
            self.mass.config.get_player_config(self.player_id)[CONF_ENABLED]
        )
        return False if not player_enabled else available

    @callback
    def get_volume_level(self, volume_level: int) -> int:
        """Return final/calculated player's volume_level."""
        if not self.available:
            return 0
        player_config = self.mass.config.player_settings[self.player_id]
        if player_config.get(CONF_VOLUME_CONTROL):
            control = self.mass.player_manager.get_player_control(
                player_config[CONF_VOLUME_CONTROL]
            )
            if control:
                return control.state
        # handle group volume
        if self.is_group_player:
            group_volume = 0
            active_players = 0
            for child_player_id in self.group_childs:
                child_player = self.mass.player_manager.get_player(child_player_id)
                if child_player and child_player.available and child_player.powered:
                    group_volume += child_player.volume_level
                    active_players += 1
            if active_players:
                group_volume = group_volume / active_players
            return group_volume
        return volume_level

    @property
    def group_parents(self) -> List[str]:
        """Return all group players this player belongs to."""
        return self._group_parents

    @callback
    def get_group_parents(self) -> List[str]:
        """Return all group players this player belongs to."""
        if self.is_group_player:
            return []
        result = []
        for player in self.mass.player_manager.players:
            if not player.is_group_player:
                continue
            if self.player_id not in player.group_childs:
                continue
            result.append(player.player_id)
        return result

    @property
    def active_queue(self) -> str:
        """Return the active parent player/queue for a player."""
        return self._active_queue

    @callback
    def get_active_queue(self) -> str:
        """Return the active parent player/queue for a player."""
        # if a group is powered on, all of it's childs will have/use
        # the parent's player's queue.
        for group_player_id in self.group_parents:
            group_player = self.mass.player_manager.get_player(group_player_id)
            if group_player and group_player.powered:
                return group_player_id
        return self.player_id

    @property
    def updated_at(self) -> datetime:
        """Return the datetime (UTC) that the player state was last updated."""
        return self._updated_at

    @callback
    def get_player_config_entries(self):
        """Get final/calculated config entries for a player."""
        entries = [item for item in self.player.config_entries]
        # append power control config entries
        power_controls = self.mass.player_manager.get_player_controls(
            PlayerControlType.POWER
        )
        if power_controls:
            controls = [
                {"text": f"{item.provider}: {item.name}", "value": item.control_id}
                for item in power_controls
            ]
            entries.append(
                ConfigEntry(
                    entry_key=CONF_POWER_CONTROL,
                    entry_type=ConfigEntryType.STRING,
                    description_key=CONF_POWER_CONTROL,
                    values=controls,
                )
            )
        # append volume control config entries
        volume_controls = self.mass.player_manager.get_player_controls(
            PlayerControlType.VOLUME
        )
        if volume_controls:
            controls = [
                {"text": f"{item.provider}: {item.name}", "value": item.control_id}
                for item in volume_controls
            ]
            entries.append(
                ConfigEntry(
                    entry_key=CONF_VOLUME_CONTROL,
                    entry_type=ConfigEntryType.STRING,
                    description_key=CONF_VOLUME_CONTROL,
                    values=controls,
                )
            )
        # append group player entries
        for parent_id in self.group_parents:
            parent_player = self.mass.player_manager.get_player(parent_id)
            if parent_player and parent_player.provider_id == "group_player":
                entries.append(
                    ConfigEntry(
                        entry_key=CONF_GROUP_DELAY,
                        entry_type=ConfigEntryType.INT,
                        default_value=0,
                        range=(0, 500),
                        description_key=CONF_GROUP_DELAY,
                    )
                )
                break
        return entries

    @callback
    def to_dict(self):
        """Instance attributes as dict so it can be serialized to json."""
        return {
            ATTR_PLAYER_ID: self.player_id,
            ATTR_PROVIDER_ID: self.provider_id,
            ATTR_NAME: self.name,
            ATTR_POWERED: self.powered,
            ATTR_ELAPSED_TIME: int(self.elapsed_time),
            ATTR_STATE: self.state.value,
            ATTR_AVAILABLE: self.available,
            ATTR_CURRENT_URI: self.current_uri,
            ATTR_VOLUME_LEVEL: self.volume_level,
            ATTR_MUTED: self.muted,
            ATTR_IS_GROUP_PLAYER: self.is_group_player,
            ATTR_GROUP_CHILDS: self.group_childs,
            ATTR_DEVICE_INFO: self.device_info.to_dict(),
            ATTR_UPDATED_AT: self.updated_at.isoformat(),
            ATTR_GROUP_PARENTS: self.group_parents,
            ATTR_FEATURES: self.features,
            ATTR_ACTIVE_QUEUE: self.active_queue,
        }

    async def async_cmd_play_uri(self, uri: str) -> None:
        """
        Play the specified uri/url on the player.

            :param uri: uri/url to send to the player.
        """
        return await self.player.async_cmd_play_uri(uri)

    async def async_cmd_stop(self) -> None:
        """Send STOP command to player."""
        return await self.player.async_cmd_stop()

    async def async_cmd_play(self) -> None:
        """Send PLAY command to player."""
        return await self.player.async_cmd_play()

    async def async_cmd_pause(self) -> None:
        """Send PAUSE command to player."""
        return await self.player.async_cmd_pause()

    async def async_cmd_next(self) -> None:
        """Send NEXT TRACK command to player."""
        return await self.player.async_cmd_next()

    async def async_cmd_previous(self) -> None:
        """Send PREVIOUS TRACK command to player."""
        return await self.player.async_cmd_previous()

    async def async_cmd_power_on(self) -> None:
        """Send POWER ON command to player."""
        return await self.player.async_cmd_power_on()

    async def async_cmd_power_off(self) -> None:
        """Send POWER OFF command to player."""
        return await self.player.async_cmd_power_off()

    async def async_cmd_volume_set(self, volume_level: int) -> None:
        """
        Send volume level command to player.

            :param volume_level: volume level to set (0..100).
        """
        return await self.player.async_cmd_volume_set(volume_level)

    async def async_cmd_volume_mute(self, is_muted: bool = False) -> None:
        """
        Send volume MUTE command to given player.

            :param is_muted: bool with new mute state.
        """
        return await self.player.async_cmd_volume_mute(is_muted)

    # OPTIONAL: QUEUE SERVICE CALLS/COMMANDS - OVERRIDE ONLY IF SUPPORTED BY PROVIDER

    async def async_cmd_queue_play_index(self, index: int) -> None:
        """
        Play item at index X on player's queue.

            :param index: (int) index of the queue item that should start playing
        """
        return await self.player.async_cmd_queue_play_index(index)

    async def async_cmd_queue_load(self, queue_items) -> None:
        """
        Load/overwrite given items in the player's queue implementation.

            :param queue_items: a list of QueueItems
        """
        return await self.player.async_cmd_queue_load(queue_items)

    async def async_cmd_queue_insert(self, queue_items, insert_at_index: int) -> None:
        """
        Insert new items at position X into existing queue.

        If insert_at_index 0 or None, will start playing newly added item(s)
            :param queue_items: a list of QueueItems
            :param insert_at_index: queue position to insert new items
        """
        return await self.player.async_cmd_queue_insert(queue_items, insert_at_index)

    async def async_cmd_queue_append(self, queue_items) -> None:
        """
        Append new items at the end of the queue.

            :param queue_items: a list of QueueItems
        """
        return await self.player.async_cmd_queue_append(queue_items)

    async def async_cmd_queue_update(self, queue_items) -> None:
        """
        Overwrite the existing items in the queue, used for reordering.

            :param queue_items: a list of QueueItems
        """
        return await self.player.async_cmd_queue_update(queue_items)

    async def async_cmd_queue_clear(self) -> None:
        """Clear the player's queue."""
        return await self.player.async_cmd_queue_clear()
