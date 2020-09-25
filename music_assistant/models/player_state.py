"""
Models and helpers for the calculated state of a player.

PlayerProviders send Player objects to us with the raw/untouched player state.
Due to configuration settings and other influences this playerstate needs alteration,
that's why we store the final player state (we present to outside world)
into a PlayerState object.
"""

import logging
from datetime import datetime

from music_assistant.constants import (
    CONF_ENABLED,
    CONF_NAME,
    CONF_POWER_CONTROL,
    CONF_VOLUME_CONTROL,
    EVENT_PLAYER_CHANGED,
)
from music_assistant.helpers.typing import MusicAssistantType
from music_assistant.models.player import PlaybackState, Player
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


UPDATE_ATTRIBUTES = [
    ATTR_NAME,
    ATTR_POWERED,
    ATTR_ELAPSED_TIME,
    ATTR_STATE,
    ATTR_AVAILABLE,
    ATTR_CURRENT_URI,
    ATTR_VOLUME_LEVEL,
    ATTR_MUTED,
    ATTR_IS_GROUP_PLAYER,
    ATTR_GROUP_CHILDS,
    ATTR_DEVICE_INFO,
    ATTR_GROUP_PARENTS,
    ATTR_ACTIVE_QUEUE,
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
        self.player_id = player.player_id
        self.provider_id = player.provider_id
        self.features = player.features
        self.muted = player.muted
        self.is_group_player = player.is_group_player
        self.group_childs = player.group_childs
        self.device_info = player.device_info
        self.group_parents = player.group_parents
        self.active_queue = player.active_queue
        self.elapsed_time = player.elapsed_time
        self.current_uri = player.current_uri
        self.available = player.available
        self.name = player.name
        self.powered = player.powered
        self.state = player.state
        self.volume_level = player.volume_level
        # run update to set the transforms
        self.update(player)

    @callback
    def update(self, player: Player):
        """Update attributes from player object."""
        self.elapsed_time = player.elapsed_time
        self.updated_at = datetime.utcnow()
        player_changed = False
        for attr in UPDATE_ATTRIBUTES:
            new_value = getattr(player, attr)

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
                setattr(self, attr, new_value)
                LOGGER.debug(
                    "Player %s attribute %s changed from %s to %s",
                    self.player_id,
                    attr,
                    current_value,
                    new_value,
                )
                player_changed = True
        if player_changed:
            self.mass.signal_event(EVENT_PLAYER_CHANGED, self)

        # always update the player queue
        player_queue = self.mass.player_manager.get_player_queue(self.active_queue)
        if player_queue:
            self.mass.add_job(player_queue.async_update_state())

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
            control = self.mass.player_manager.get_player_control(
                player_config[CONF_POWER_CONTROL]
            )
            if control:
                return control.state
        return power

    def get_state(self, state: PlaybackState) -> PlaybackState:
        """Return final/calculated player's playback state."""
        if self.powered and self.active_queue != self.player_id:
            # use group state
            return self.mass.player_manager.get_player(self.active_queue).state
        if state == PlaybackState.Stopped and not self.powered:
            return PlaybackState.Off
        return state

    def get_available(self, available: bool) -> bool:
        """Return current availablity of player."""
        player_enabled = bool(
            self.mass.config.get_player_config(self.player_id)[CONF_ENABLED]
        )
        return False if not player_enabled else available

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
