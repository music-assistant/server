"""Models and helpers for a player."""
from __future__ import annotations

from abc import ABC
from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import TYPE_CHECKING, Any, Dict, List

from mashumaro import DataClassDictMixin
from music_assistant.constants import EventType
from music_assistant.helpers.typing import MusicAssistant
from music_assistant.helpers.util import create_task

if TYPE_CHECKING:
    from .player_queue import PlayerQueue


class PlayerState(Enum):
    """Enum for the (playback)state of a player."""

    IDLE = "idle"
    PAUSED = "paused"
    PLAYING = "playing"
    OFF = "off"


@dataclass(frozen=True)
class DeviceInfo(DataClassDictMixin):
    """Model for a player's deviceinfo."""

    model: str = "unknown"
    address: str = "unknown"
    manufacturer: str = "unknown"


class PlayerFeature(IntEnum):
    """Enum for player features."""

    QUEUE = 0
    GAPLESS = 1
    CROSSFADE = 2


class Player(ABC):
    """Model for a music player."""

    player_id: str
    _attr_is_group: bool = False
    _attr_group_childs: List[str] = []
    _attr_name: str = None
    _attr_powered: bool = False
    _attr_elapsed_time: int = 0
    _attr_current_url: str = None
    _attr_state: PlayerState = PlayerState.IDLE
    _attr_available: bool = True
    _attr_volume_level: int = 100
    _attr_device_info: DeviceInfo = DeviceInfo()
    _attr_max_sample_rate: int = 96000
    _attr_active_queue_id: str = ""
    # mass object will be set by playermanager at register
    mass: MusicAssistant = None  # type: ignore[assignment]

    @property
    def name(self) -> bool:
        """Return player name."""
        return self._attr_name or self.player_id

    @property
    def is_group(self) -> bool:
        """Return bool if this player is a grouped player (playergroup)."""
        return self._attr_is_group

    @property
    def group_childs(self) -> List[str]:
        """Return list of child player id's of PlayerGroup (if player is group)."""
        return self._attr_group_childs

    @property
    def powered(self) -> bool:
        """Return current power state of player."""
        return self._attr_powered

    @property
    def elapsed_time(self) -> int:
        """Return elapsed time of current playing media in seconds."""
        return self._attr_elapsed_time

    @property
    def current_url(self) -> str:
        """Return URL that is currently loaded in the player."""

    @property
    def state(self) -> PlayerState:
        """Return current PlayerState of player."""
        return self._attr_state

    @property
    def available(self) -> bool:
        """Return current availablity of player."""
        return self._attr_available

    @property
    def volume_level(self) -> int:
        """Return current volume level of player (scale 0..100)."""
        return self._attr_volume_level

    @property
    def device_info(self) -> DeviceInfo:
        """Return basic device/provider info for this player."""
        return self._attr_device_info

    @property
    def max_sample_rate(self) -> int:
        """Return the maximum supported sample rate this player supports."""
        return self._attr_max_sample_rate

    @property
    def active_queue(self) -> PlayerQueue:
        """
        Return the currently active queue for this player.

        If the player is a group child this will return its parent when that is playing,
        otherwise it will return the player's own queue.
        """
        return self.mass.players.get_player_queue(self._attr_active_queue_id)

    async def play_url(self, url: str) -> None:
        """Play the specified url on the player."""
        raise NotImplementedError

    async def stop(self) -> None:
        """Send STOP command to player."""
        raise NotImplementedError

    async def play(self) -> None:
        """Send PLAY/UNPAUSE command to player."""
        raise NotImplementedError

    async def pause(self) -> None:
        """Send PAUSE command to player."""
        raise NotImplementedError

    async def power(self, powered: bool) -> None:
        """Send POWER command to player."""
        raise NotImplementedError

    async def volume_set(self, volume_level: int) -> None:
        """Send volume level (0..100) command to player."""
        raise NotImplementedError

    # SOME CONVENIENCE METHODS

    async def volume_up(self, step_size: int = 5):
        """Send volume UP command to player."""
        new_level = min(self.volume_level + step_size, 100)
        return await self.volume_set(new_level)

    async def volume_down(self, step_size: int = 5):
        """Send volume DOWN command to player."""
        new_level = max(self.volume_level - step_size, 0)
        return await self.volume_set(new_level)

    async def play_pause(self) -> None:
        """Toggle play/pause on player."""
        if self.state == PlayerState.PAUSED:
            await self.play()
        else:
            await self.pause()

    async def power_toggle(self) -> None:
        """Toggle power on player."""
        await self.power(not self.powered)

    # DO NOT OVERRIDE BELOW

    def update_state(self) -> None:
        """Update current player state in the player manager."""
        # determine active queue for player
        queue_id = self.player_id
        for player_id in self.get_group_parents():
            if player := self.mass.players.get_player(player_id):
                if player.state in [PlayerState.PLAYING, PlayerState.PAUSED]:
                    queue_id = player_id
                    break
        self._attr_active_queue_id = queue_id
        # basic throttle: do not send state changed events if player did not change
        prev_state = getattr(self, "_prev_state", None)
        cur_state = self.to_dict()
        if prev_state == cur_state:
            return
        setattr(self, "_prev_state", cur_state)
        self.mass.signal_event(EventType.PLAYER_CHANGED, self)
        self.mass.players.get_player_queue(self.player_id).on_player_update()
        if self.is_group:
            # update group player childs when parent updates
            for child_player_id in self.group_childs:
                if player := self.mass.players.get_player(child_player_id):
                    create_task(player.update_state)
        else:
            # update group player when child updates
            for group_player_id in self.get_group_parents():
                if player := self.mass.players.get_player(group_player_id):
                    create_task(player.update_state)

    def get_group_parents(self) -> List[str]:
        """Get any/all group player id's this player belongs to."""
        return [
            x.player_id
            for x in self.mass.players
            if x.is_group and self.player_id in x.group_childs
        ]

    def to_dict(self) -> Dict[str, Any]:
        """Export object to dict."""
        return {
            "player_id": self.player_id,
            "name": self.name,
            "powered": self.powered,
            "elapsed_time": self.elapsed_time,
            "state": self.state.value,
            "available": self.available,
            "is_group": self.is_group,
            "group_childs": self.group_childs,
            "volume_level": int(self.volume_level),
            "device_info": self.device_info.to_dict(),
            "active_queue": self.active_queue.queue_id,
        }


class PlayerGroup(Player):
    """Convenience Model for a player group with some additional helper methods."""

    is_group: bool = True
    _attr_group_childs: List[str] = []
    _attr_support_join_control: bool = True

    @property
    def volume_level(self) -> int:
        """Return current volume level of player (scale 0..100)."""
        if not self.available:
            return 0
        # calculate group volume from powered players for convenience
        # may be overridden if implementation provides this natively
        group_volume = 0
        active_players = 0
        for child_player in self._get_players(True):
            group_volume += child_player.volume_level
            active_players += 1
        if active_players:
            group_volume = group_volume / active_players
        return int(group_volume)

    async def volume_set(self, volume_level: int) -> None:
        """Send volume level (0..100) command to player."""
        # handle group volume by only applying the valume to powered childs
        # may be overridden if implementation provides this natively
        cur_volume = self.volume_level
        new_volume = volume_level
        volume_dif = new_volume - cur_volume
        if cur_volume == 0:
            volume_dif_percent = 1 + (new_volume / 100)
        else:
            volume_dif_percent = volume_dif / cur_volume
        for child_player in self._get_players(True):
            cur_child_volume = child_player.volume_level
            new_child_volume = cur_child_volume + (
                cur_child_volume * volume_dif_percent
            )
            await child_player.volume_set(new_child_volume)

    def _get_players(self, only_powered: bool = False) -> List[Player]:
        """Get players attached to this group."""
        return [
            x
            for x in self.mass.players
            if x.player_id in self.group_childs and x.powered or not only_powered
        ]
