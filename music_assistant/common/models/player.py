"""Model(s) for Player."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from mashumaro import DataClassDictMixin

from .enums import PlayerFeature, PlayerState, PlayerType


@dataclass(frozen=True)
class DeviceInfo(DataClassDictMixin):
    """Model for a player's deviceinfo."""

    model: str = "Unknown model"
    address: str = ""
    manufacturer: str = "Unknown Manufacturer"


@dataclass
class Player(DataClassDictMixin):
    """Representation of a Player within Music Assistant."""

    player_id: str
    provider: str  # instance_id of the player provider
    type: PlayerType
    name: str
    available: bool
    powered: bool
    device_info: DeviceInfo
    supported_features: tuple[PlayerFeature, ...] = field(default=())

    elapsed_time: float = 0
    elapsed_time_last_updated: float = time.time()
    state: PlayerState = PlayerState.IDLE

    volume_level: int = 100
    volume_muted: bool = False

    # group_childs: Return list of player group child id's or synced child`s.
    # - If this player is a dedicated group player,
    #   returns all child id's of the players in the group.
    # - If this is a syncgroup of players from the same platform (e.g. sonos),
    #   this will return the id's of players synced to this player,
    #   and this may include the player's own id.
    group_childs: set[str] = field(default_factory=set)

    # active_source: return player_id of the active queue for this player
    # if the player is grouped and a group is active, this will be set to the group's player_id
    # otherwise it will be set to the own player_id
    # can also be an actual different source if the player supports that
    active_source: str | None = None

    # current_item_id: return item_id/uri of the current active/loaded item on the player
    # this may be a MA queue_item_id, url, uri or some provider specific string
    current_item_id: str | None = None

    # can_sync_with: return tuple of player_ids that can be synced to/with this player
    # usually this is just a list of all player_ids within the playerprovider
    can_sync_with: tuple[str, ...] = field(default=())

    # synced_to: player_id of the player this player is currently synced to
    # also referred to as "sync master"
    synced_to: str | None = None

    # max_sample_rate: maximum supported sample rate the player supports
    max_sample_rate: int = 48000

    # supports_24bit: bool if player supports 24bits (hi res) audio
    supports_24bit: bool = True

    # enabled_by_default: if the player is enabled by default
    # can be used by a player provider to exclude some sort of players
    enabled_by_default: bool = True

    #
    # THE BELOW ATTRIBUTES ARE MANAGED BY CONFIG AND THE PLAYER MANAGER
    #

    # enabled: if the player is enabled
    # will be set by the player manager based on config
    # a disabled player is hidden in the UI and updates will not be processed
    # nor will it be added to the HA integration
    enabled: bool = True

    # hidden: if the player is hidden in the UI
    # will be set by the player manager based on config
    # a hidden player is hidden in the UI only but can still be controlled
    hidden: bool = False

    # group_volume: if the player is a player group or syncgroup master,
    # this will return the average volume of all child players
    # if not a group player, this is just the player's volume
    group_volume: int = 100

    # display_name: return final/corrected name of the player
    # always prefers any overridden name from settings
    display_name: str = ""

    # extra_data: any additional data to store on the player object
    # and pass along freely
    extra_data: dict[str, Any] = field(default_factory=dict)

    @property
    def corrected_elapsed_time(self) -> float:
        """Return the corrected/realtime elapsed time."""
        if self.state == PlayerState.PLAYING:
            return self.elapsed_time + (time.time() - self.elapsed_time_last_updated)
        return self.elapsed_time
