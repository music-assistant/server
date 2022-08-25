"""Models and helpers for a player."""
from __future__ import annotations

import asyncio
from abc import ABC
from dataclasses import dataclass
from logging import Logger
from typing import TYPE_CHECKING, Any, Dict, List, Tuple

from mashumaro import DataClassDictMixin

from music_assistant.helpers.util import get_changed_keys

from .enums import EventType, PlayerFeature, PlayerState, PlayerType
from .event import MassEvent
from .player_settings import PlayerSettings

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

    from .player_queue import PlayerQueue


@dataclass(frozen=True)
class DeviceInfo(DataClassDictMixin):
    """Model for a player's deviceinfo."""

    model: str = "unknown"
    address: str = "unknown"
    manufacturer: str = "unknown"


DEFAULT_FEATURES = (PlayerFeature.POWER, PlayerFeature.MUTE)


class Player(ABC):
    """Model for a standard music player."""

    player_id: str
    platform: str  # e.g. cast or sonos etc.

    _attr_type: PlayerType = PlayerType.PLAYER
    _attr_name: str = ""
    _attr_powered: bool = False
    _attr_elapsed_time: float = 0
    _attr_current_url: str = ""
    _attr_state: PlayerState = PlayerState.IDLE
    _attr_available: bool = True
    _attr_volume_level: int = 100
    _attr_volume_muted: bool = False
    _attr_device_info: DeviceInfo = DeviceInfo()
    _attr_supported_features: Tuple[PlayerFeature] = DEFAULT_FEATURES
    _attr_group_members: List[str] = []

    # below objects will be set by playermanager at register/update
    mass: MusicAssistant = None  # type: ignore[assignment]
    queue: PlayerQueue = None  # type: ignore[assignment]
    settings: PlayerSettings = None  # type: ignore[assignment]
    logger: Logger = None  # type: ignore[assignment]
    _prev_state: dict = {}

    @property
    def type(self) -> bool:
        """Return player type."""
        return self._attr_type

    @property
    def available(self) -> bool:
        """Return current availablity of player."""
        return self._attr_available

    @property
    def name(self) -> bool:
        """Return player name."""
        return self._attr_name or self.player_id

    @property
    def powered(self) -> bool:
        """Return current power state of player."""
        return self._attr_powered

    @property
    def elapsed_time(self) -> float:
        """Return elapsed time of current playing media in seconds."""
        # NOTE: Make sure to provide an accurate elapsed time otherwise the
        # queue reporting of playing tracks will be wrong.
        # this attribute will be checked every second when the queue is playing
        return self._attr_elapsed_time

    @property
    def current_url(self) -> str:
        """Return URL that is currently loaded in the player."""
        return self._attr_current_url

    @property
    def state(self) -> PlayerState:
        """Return current PlayerState of player."""
        if not self.powered:
            return PlayerState.OFF
        return self._attr_state

    @property
    def volume_level(self) -> int:
        """Return current volume level of player (scale 0..100)."""
        return self._attr_volume_level

    @property
    def volume_muted(self) -> bool:
        """Return current mute mode of player."""
        return self._attr_volume_muted

    @property
    def device_info(self) -> DeviceInfo:
        """Return basic device/provider info for this player."""
        return self._attr_device_info

    @property
    def supported_features(self) -> Tuple[PlayerFeature]:
        """Return features supported by this player."""
        return self._attr_supported_features

    # SERVICE CALLS BELOW

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
        if PlayerFeature.POWER in self.supported_features:
            raise NotImplementedError
        # fallback to mute as power control
        if PlayerFeature.MUTE in self.supported_features:
            await self.volume_mute(not powered)
        self._attr_powered = powered
        self.update_state()

    async def volume_set(self, volume_level: int) -> None:
        """Send volume level (0..100) command to player."""
        raise NotImplementedError

    @property
    def group_members(self) -> List[str]:
        """
        Return list of player group child id's or synced childs.

        - If this player is a dedicated group player (e.g. cast),
          returns all child id's of the players in the group.
        - If this is a syncgroup of players from the same platform (e.g. sonos),
          this will return the id's of players synced to this player.
        """
        return self._attr_group_members

    async def volume_mute(self, muted: bool) -> None:
        """Send volume mute command to player."""
        if PlayerFeature.MUTE in self.supported_features:
            raise NotImplementedError
        # for players that do not support mute, we fake mute with volume
        self._attr_volume_muted = muted
        if muted:
            setattr(self, "prev_volume", self.volume_level)
        else:
            await self.volume_set(getattr(self, "prev_volume", 0))

    # SOME CONVENIENCE METHODS (may be overridden if needed)

    async def volume_up(self, step_size: int = 5) -> None:
        """Send volume UP command to player."""
        new_level = min(self.volume_level + step_size, 100)
        return await self.volume_set(new_level)

    async def volume_down(self, step_size: int = 5) -> None:
        """Send volume DOWN command to player."""
        new_level = max(self.volume_level - step_size, 0)
        return await self.volume_set(new_level)

    async def play_pause(self) -> None:
        """Toggle play/pause on player."""
        if self.state == PlayerState.PLAYING:
            await self.pause()
        else:
            await self.play()

    async def power_toggle(self) -> None:
        """Toggle power on player."""
        await self.power(not self.powered)

    def on_update(self) -> None:
        """Call when player is about to be updated in the player manager."""

    def on_child_update(self, player_id: str, changed_keys: set) -> None:
        """Call when one of the child players of a playergroup updates."""
        self.update_state(skip_forward=True)

    def on_parent_update(self, player_id: str, changed_keys: set) -> None:
        """Call when (one of) the parent player(s) of a grouped player updates."""
        self.update_state(skip_forward=True)

    def on_remove(self) -> None:
        """Call when player is about to be removed (cleaned up) from player manager."""

    @property
    def active_queue(self) -> str:
        """Return the queue_id that is currently active on/for this player."""
        # if player is a (passive) sync child, return its sync parent
        if self.type == PlayerType.SYNC_CHILD:
            for player in self.mass.players:
                if self.player_id in player.group_members:
                    return player.queue.queue_id

        # look for any groups that are active (powered on)
        for group in self.groups:
            if group.powered:
                return group.queue.queue_id

        # no group parents active return own queue
        return self.queue.queue_id

    @property
    def groups(self) -> List[Player]:
        """Return all group players this player belongs to."""
        if not self.mass:
            return []
        return [
            x
            for x in self.mass.players
            if self.player_id != x.player_id and self.player_id in x.group_members
        ]

    # DO NOT OVERRIDE BELOW !

    def update_state(self, skip_forward: bool = False) -> None:
        """Update current player state in the player manager."""
        if self.mass is None or self.mass.closed:
            # guard
            return
        self.on_update()
        # basic throttle: do not send state changed events if player did not change
        cur_state = self.to_dict()
        changed_keys = get_changed_keys(
            self._prev_state, cur_state, ignore_keys=["elapsed_time"]
        )

        if len(changed_keys) == 0:
            return

        # update the playerqueue
        self.queue.on_player_update()

        self._prev_state = cur_state
        self.mass.signal_event(
            MassEvent(EventType.PLAYER_UPDATED, object_id=self.player_id, data=self)
        )

        if skip_forward:
            return
        if self.type == PlayerType.GROUP:
            # update group player members when parent updates
            for child_player_id in self.group_members:
                if child_player_id == self.player_id:
                    continue
                if player := self.mass.players.get_player(child_player_id):
                    self.mass.loop.call_soon_threadsafe(
                        player.on_parent_update, self.player_id, changed_keys
                    )

        # update group player(s) when child updates
        for group_player in self.groups:
            self.mass.loop.call_soon_threadsafe(
                group_player.on_child_update, self.player_id, changed_keys
            )

    def to_dict(self) -> Dict[str, Any]:
        """Export object to dict."""
        return {
            "player_id": self.player_id,
            "name": self.name,
            "powered": self.powered,
            "elapsed_time": int(self.elapsed_time),
            "state": self.state.value,
            "available": self.available,
            "volume_level": int(self.volume_level),
            "group_members": self.group_members,
            "device_info": self.device_info.to_dict(),
            "active_queue": self.active_queue,
            "groups": [x.player_id for x in self.groups],
        }


class PlayerGroup(Player):
    """Model for a group of players."""

    _attr_type: PlayerType = PlayerType.GROUP
    _attr_supported_features: Tuple[PlayerFeature] = tuple()

    @property
    def volume_level(self) -> int:
        """Calculate a group volume from the grouped members."""
        group_volume = 0
        active_players = 0
        for child_player in self.get_child_players(True):
            group_volume += child_player.volume_level
            active_players += 1
        if active_players:
            group_volume = group_volume / active_players
        return int(group_volume)

    async def volume_set(self, volume_level: int) -> None:
        """Send volume level (0..100) command to groupplayer's member(s)."""
        # handle group volume by only applying the volume to powered members
        cur_volume = self.volume_level
        new_volume = volume_level
        volume_dif = new_volume - cur_volume
        if cur_volume == 0:
            volume_dif_percent = 1 + (new_volume / 100)
        else:
            volume_dif_percent = volume_dif / cur_volume
        coros = []
        for child_player in self.get_child_players(True):
            cur_child_volume = child_player.volume_level
            new_child_volume = cur_child_volume + (
                cur_child_volume * volume_dif_percent
            )
            coros.append(child_player.volume_set(new_child_volume))
        await asyncio.gather(*coros)

    async def power(self, powered: bool) -> None:
        """Send POWER command to (group) player."""
        await super().power(powered)
        # turn on/off group childs
        coros = [
            player.power(powered) for player in self.get_child_players(not powered)
        ]
        await asyncio.gather(*coros)

    def get_child_players(
        self,
        only_powered: bool = False,
        only_playing: bool = False,
        ignore_sync_childs: bool = False,
    ) -> List[Player]:
        """Get players attached to a grouped player."""
        if not self.mass:
            return []
        child_players = set()
        for child_id in self.group_members:
            if child_player := self.mass.players.get_player(child_id):
                if not (not only_powered or child_player.powered):
                    continue
                if not (not only_playing or child_player.state == PlayerState.PLAYING):
                    continue
                if ignore_sync_childs and child_player.type == PlayerType.SYNC_CHILD:
                    continue
                child_players.add(child_player)
        return list(child_players)


class UniversalPlayerGroup(PlayerGroup):
    """Universal multi-platform player group (group and sync maintained by MA)."""

    async def play_url(self, url: str) -> None:
        """Play the specified url on the player."""
        # redirect command to all (non passive) clients
        coros = [
            player.play_url(url)
            for player in self.get_child_players(ignore_sync_childs=True)
        ]
        await asyncio.gather(*coros)

    async def stop(self) -> None:
        """Send STOP command to player."""
        # redirect command to all (non passive) clients
        coros = [
            player.stop() for player in self.get_child_players(ignore_sync_childs=True)
        ]
        await asyncio.gather(*coros)

    async def play(self) -> None:
        """Send PLAY/UNPAUSE command to player."""
        # redirect command to all (non passive) clients
        coros = [
            player.play() for player in self.get_child_players(ignore_sync_childs=True)
        ]
        await asyncio.gather(*coros)

    async def pause(self) -> None:
        """Send PAUSE command to player."""
        # redirect command to all (non passive) clients
        coros = [
            player.pause() for player in self.get_child_players(ignore_sync_childs=True)
        ]
        await asyncio.gather(*coros)
