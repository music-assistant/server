"""Models and helpers for a player."""
from __future__ import annotations

import asyncio
from abc import ABC
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List

from mashumaro import DataClassDictMixin

from music_assistant.helpers.util import get_changed_keys
from music_assistant.models.enums import EventType, PlayerState
from music_assistant.models.event import MassEvent
from music_assistant.models.media_items import ContentType

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

    from .player_queue import PlayerQueue


@dataclass(frozen=True)
class DeviceInfo(DataClassDictMixin):
    """Model for a player's deviceinfo."""

    model: str = "unknown"
    address: str = "unknown"
    manufacturer: str = "unknown"


class Player(ABC):
    """Model for a music player."""

    player_id: str
    _attr_group_members: List[str] = []
    _attr_name: str = ""
    _attr_powered: bool = False
    _attr_elapsed_time: float = 0
    _attr_current_url: str = ""
    _attr_state: PlayerState = PlayerState.IDLE
    _attr_available: bool = True
    _attr_volume_level: int = 100
    _attr_volume_muted: bool = False
    _attr_device_info: DeviceInfo = DeviceInfo()
    _attr_max_sample_rate: int = 96000
    _attr_stream_type: ContentType = ContentType.FLAC
    # below objects will be set by playermanager at register/update
    mass: MusicAssistant = None  # type: ignore[assignment]
    _prev_state: dict = {}

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
    def available(self) -> bool:
        """Return current availablity of player."""
        return self._attr_available

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

    # DEFAULT PLAYER SETTINGS

    @property
    def max_sample_rate(self) -> int:
        """Return the (default) max supported sample rate."""
        # if a player does not report/set its supported sample rates, we use a pretty safe default
        return self._attr_max_sample_rate

    @property
    def stream_type(self) -> ContentType:
        """Return the default/preferred content type to use for streaming."""
        return self._attr_stream_type

    # GROUP PLAYER ATTRIBUTES AND METHODS (may be overridden if needed)
    # a player can optionally be a group leader (e.g. Sonos)
    # or be a group player itself (e.g. Cast)
    # support both scenarios here

    @property
    def is_group(self) -> bool:
        """Return if this player represents a playergroup or is grouped with other players."""
        return len(self.group_members) > 1

    @property
    def group_members(self) -> List[str]:
        """
        Return list of grouped players.

        If this player is a dedicated group player (e.g. cast), returns the grouped child id's.
        If this is a player grouped with other players within the same platform (e.g. Sonos),
        this will return the players that are currently grouped together.
        The first child id should represent the group leader.
        """
        return self._attr_group_members

    @property
    def group_leader(self) -> str | None:
        """Return the leader's player_id of this playergroup."""
        if group_members := self.group_members:
            return group_members[0]
        return None

    @property
    def is_group_leader(self) -> bool:
        """Return if this player is the leader in a playergroup."""
        return self.is_group and self.group_leader == self.player_id

    @property
    def is_passive(self) -> bool:
        """
        Return if this player may not accept any playback related commands.

        Usually this means the player is part of a playergroup but not the leader.
        """
        if self.is_group and self.player_id not in self.group_members:
            return False
        return self.is_group and not self.is_group_leader

    @property
    def group_name(self) -> str:
        """Return name of this grouped player."""
        if not self.is_group:
            return self.name
        # default to name of groupleader and number of childs
        num_childs = len([x for x in self.group_members if x != self.player_id])
        return f"{self.name} +{num_childs}"

    @property
    def group_powered(self) -> bool:
        """Calculate a group power state from the grouped members."""
        if not self.is_group:
            return self.powered
        for _ in self.get_child_players(True):
            return True
        return False

    @property
    def group_volume_level(self) -> int:
        """Calculate a group volume from the grouped members."""
        if not self.is_group:
            return self.volume_level
        group_volume = 0
        active_players = 0
        for child_player in self.get_child_players(True):
            group_volume += child_player.volume_level
            active_players += 1
        if active_players:
            group_volume = group_volume / active_players
        return int(group_volume)

    async def set_group_volume(self, volume_level: int) -> None:
        """Send volume level (0..100) command to groupplayer's member(s)."""
        # handle group volume by only applying the volume to powered members
        cur_volume = self.group_volume_level
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

    async def set_group_power(self, powered: bool) -> None:
        """Send power command to groupplayer's member(s)."""
        coros = [
            player.power(powered) for player in self.get_child_players(not powered)
        ]
        await asyncio.gather(*coros)

    # SOME CONVENIENCE METHODS (may be overridden if needed)

    async def volume_mute(self, muted: bool) -> None:
        """Send volume mute command to player."""
        # for players that do not support mute, we fake mute with volume
        self._attr_volume_muted = muted
        if muted:
            setattr(self, "prev_volume", self.volume_level)
        else:
            await self.volume_set(getattr(self, "prev_volume", 0))

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

    # DO NOT OVERRIDE BELOW

    @property
    def active_queue(self) -> PlayerQueue:
        """Return the queue that is currently active on/for this player."""
        for queue in self.mass.players.player_queues:
            if queue.stream and queue.stream.url == self.current_url:
                return queue
        return self.mass.players.get_player_queue(self.player_id)

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

        # always update the playerqueue
        self.mass.players.get_player_queue(self.player_id).on_player_update()

        if len(changed_keys) == 0:
            return

        self._prev_state = cur_state
        self.mass.signal_event(
            MassEvent(EventType.PLAYER_UPDATED, object_id=self.player_id, data=self)
        )

        if skip_forward:
            return
        if self.is_group:
            # update group player members when parent updates
            for child_player_id in self.group_members:
                if child_player_id == self.player_id:
                    continue
                if player := self.mass.players.get_player(child_player_id):
                    self.mass.create_task(
                        player.on_parent_update, self.player_id, changed_keys
                    )

        # update group player(s) when child updates
        for group_player in self.get_group_parents():
            self.mass.create_task(
                group_player.on_child_update, self.player_id, changed_keys
            )

    def get_group_parents(self) -> List[Player]:
        """Get any/all group player id's this player belongs to."""
        return [
            x
            for x in self.mass.players
            if x.is_group and self.player_id in x.group_members and x != self
        ]

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
            "is_group": self.is_group,
            "group_members": self.group_members,
            "group_leader": self.group_leader,
            "is_passive": self.is_passive,
            "group_name": self.group_name,
            "group_powered": self.group_powered,
            "group_volume_level": int(self.group_volume_level),
            "device_info": self.device_info.to_dict(),
            "active_queue": self.active_queue.queue_id
            if self.active_queue
            else self.player_id,
        }

    def get_child_players(
        self,
        only_powered: bool = False,
        only_playing: bool = False,
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
                child_players.add(child_player)
        return list(child_players)
