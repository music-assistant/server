"""Model(s) for Player."""
from __future__ import annotations

from dataclasses import dataclass, field
import time
from mashumaro import DataClassDictMixin
from .enums import PlayerFeature, PlayerType, PlayerState


@dataclass(frozen=True)
class DeviceInfo(DataClassDictMixin):
    """Model for a player's deviceinfo."""

    model: str = "unknown"
    address: str = "unknown"
    manufacturer: str = "unknown"


DEFAULT_FEATURES = (PlayerFeature.POWER, PlayerFeature.MUTE)


@dataclass
class Player(DataClassDictMixin):
    """Representation of a Player within Music Assistant."""

    player_id: str
    provider: str
    type: PlayerType
    name: str
    available: bool
    powered: bool
    device_info: DeviceInfo
    supported_features: tuple[PlayerFeature] = DEFAULT_FEATURES

    elapsed_time: float = 0
    elapsed_time_last_updated: float = time.time()
    current_url: str = ""
    state: PlayerState = PlayerState.IDLE

    volume_level: int = 100
    volume_muted: bool = False

    # group_members: Return list of player group child id's or synced childs.
    # - If this player is a dedicated group player (e.g. cast),
    #   returns all child id's of the players in the group.
    # - If this is a syncgroup of players from the same platform (e.g. sonos),
    #   this will return the id's of players synced to this player.
    group_members: list[str] = field(default_factory=list)
    # active_queue: return player_id of the active queue for this player
    # if the player is grouped and a group is active, this will return the group's player_id
    # otherwise it will return the own player_id
    active_queue: str = ""

    @property
    def corrected_elapsed_time(self) -> float:
        """Return the corrected/realtime elapsed time."""
        return self.elapsed_time + (time.time() - self.elapsed_time_last_updated)
