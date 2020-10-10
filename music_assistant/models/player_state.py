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
    ATTR_ACTIVE_QUEUE,
    ATTR_AVAILABLE,
    ATTR_CURRENT_URI,
    ATTR_DEVICE_INFO,
    ATTR_ELAPSED_TIME,
    ATTR_FEATURES,
    ATTR_GROUP_CHILDS,
    ATTR_GROUP_PARENTS,
    ATTR_IS_GROUP_PLAYER,
    ATTR_MUTED,
    ATTR_NAME,
    ATTR_PLAYER_ID,
    ATTR_POWERED,
    ATTR_PROVIDER_ID,
    ATTR_SHOULD_POLL,
    ATTR_STATE,
    ATTR_UPDATED_AT,
    ATTR_VOLUME_LEVEL,
    CONF_ENABLED,
    CONF_GROUP_DELAY,
    CONF_NAME,
    CONF_POWER_CONTROL,
    CONF_VOLUME_CONTROL,
    EVENT_PLAYER_CHANGED,
)
from music_assistant.helpers.typing import MusicAssistantType
from music_assistant.models.player import (
    DeviceInfo,
    PlaybackState,
    Player,
    PlayerFeature,
)

LOGGER = logging.getLogger("player_state")

# List of all player_state attributes
PLAYER_ATTRIBUTES = [
    ATTR_ACTIVE_QUEUE,
    ATTR_AVAILABLE,
    ATTR_CURRENT_URI,
    ATTR_DEVICE_INFO,
    ATTR_ELAPSED_TIME,
    ATTR_FEATURES,
    ATTR_GROUP_CHILDS,
    ATTR_GROUP_PARENTS,
    ATTR_IS_GROUP_PLAYER,
    ATTR_MUTED,
    ATTR_NAME,
    ATTR_PLAYER_ID,
    ATTR_POWERED,
    ATTR_PROVIDER_ID,
    ATTR_SHOULD_POLL,
    ATTR_STATE,
    ATTR_VOLUME_LEVEL,
]

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
    ATTR_SHOULD_POLL,
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
        self._group_delay = self.get_group_delay()
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
        # always realtime returned from player
        if self.player.elapsed_milliseconds is not None:
            return self.player.elapsed_milliseconds - self.group_delay
        return None

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
    def group_delay(self) -> int:
        """Return group delay of this player in milliseconds (if configured)."""
        return self._group_delay

    async def async_update(self, player: Player):
        """Run update player state task in executor."""
        self.mass.add_job(self.update, player)

    def update(self, player: Player):
        """Update attributes from player object."""
        new_available = self.get_available(player.available)
        if self.available == new_available and not new_available:
            return  # ignore players that are unavailable

        # detect state changes
        changed_keys = set()
        for attr in PLAYER_ATTRIBUTES:

            new_value = getattr(self._player, attr, None)

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
            elif attr == ATTR_GROUP_PARENTS:
                new_value = self.get_group_parents()
            elif attr == ATTR_ACTIVE_QUEUE:
                new_value = self.get_active_queue()

            current_value = getattr(self, attr)

            if current_value != new_value:
                # value changed
                setattr(self, "_" + attr, new_value)
                changed_keys.add(attr)

        if changed_keys:
            self._updated_at = datetime.utcnow()

            if changed_keys.intersection(set(UPDATE_ATTRIBUTES)):
                self.mass.signal_event(EVENT_PLAYER_CHANGED, self)
                # update group player childs when parent updates
                for child_player_id in self.group_childs:
                    self.mass.add_job(
                        self.mass.players.async_trigger_player_update(child_player_id)
                    )
                # update group player when child updates
                for group_player_id in self.group_parents:
                    self.mass.add_job(
                        self.mass.players.async_trigger_player_update(group_player_id)
                    )

            # always update the player queue
            player_queue = self.mass.players.get_player_queue(self.active_queue)
            if player_queue:
                self.mass.add_job(player_queue.async_update_state())
            self._group_delay = self.get_group_delay()

    def get_name(self, name: str) -> str:
        """Return final/calculated player name."""
        conf_name = self.mass.config.get_player_config(self.player_id)[CONF_NAME]
        return conf_name if conf_name else name

    def get_power(self, power: bool) -> bool:
        """Return final/calculated player's power state."""
        if not self.available:
            return False
        player_config = self.mass.config.player_settings[self.player_id]
        if player_config.get(CONF_POWER_CONTROL):
            control = self.mass.players.get_player_control(
                player_config[CONF_POWER_CONTROL]
            )
            if control:
                return control.state
        return power

    def get_state(self, state: PlaybackState) -> PlaybackState:
        """Return final/calculated player's playback state."""
        if self.powered and self.active_queue != self.player_id:
            # use group state
            return self.mass.players.get_player_state(self.active_queue).state
        if state == PlaybackState.Stopped and not self.powered:
            return PlaybackState.Off
        return state

    def get_available(self, available: bool) -> bool:
        """Return current availablity of player."""
        player_enabled = self.mass.config.get_player_config(self.player_id)[
            CONF_ENABLED
        ]
        return False if not player_enabled else available

    def get_volume_level(self, volume_level: int) -> int:
        """Return final/calculated player's volume_level."""
        if not self.available:
            return 0
        player_config = self.mass.config.player_settings[self.player_id]
        if player_config.get(CONF_VOLUME_CONTROL):
            control = self.mass.players.get_player_control(
                player_config[CONF_VOLUME_CONTROL]
            )
            if control:
                return control.state
        # handle group volume
        if self.is_group_player:
            group_volume = 0
            active_players = 0
            for child_player_id in self.group_childs:
                child_player = self.mass.players.get_player_state(child_player_id)
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

    def get_group_parents(self) -> List[str]:
        """Return all group players this player belongs to."""
        if self.is_group_player:
            return []
        result = []
        for player in self.mass.players.player_states:
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

    def get_active_queue(self) -> str:
        """Return the active parent player/queue for a player."""
        # if a group is powered on, all of it's childs will have/use
        # the parent's player's queue.
        for group_player_id in self.group_parents:
            group_player = self.mass.players.get_player_state(group_player_id)
            if group_player and group_player.powered:
                return group_player_id
        return self.player_id

    @property
    def updated_at(self) -> datetime:
        """Return the datetime (UTC) that the player state was last updated."""
        return self._updated_at

    def get_group_delay(self):
        """Get group delay for a player."""
        player_settings = self.mass.config.get_player_config(self.player_id)
        if player_settings:
            return player_settings.get(CONF_GROUP_DELAY, 0)
        return 0

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
            ATTR_DEVICE_INFO: self.device_info,
            ATTR_UPDATED_AT: self.updated_at,
            ATTR_GROUP_PARENTS: self.group_parents,
            ATTR_FEATURES: self.features,
            ATTR_ACTIVE_QUEUE: self.active_queue,
        }
