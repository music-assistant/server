"""Models and helpers for a player."""
from __future__ import annotations

import asyncio
from abc import ABC
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Tuple

from mashumaro import DataClassDictMixin

from music_assistant.helpers.util import get_changed_keys
from music_assistant.models.enums import EventType, PlayerState
from music_assistant.models.event import MassEvent
from music_assistant.models.media_items import ContentType

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

    from .player_queue import PlayerQueue


DEFAULT_SUPPORTED_CONTENT_TYPES = (
    # if a player does not report/set its supported content types, we use a pretty safe default
    ContentType.FLAC,
    ContentType.MP3,
    ContentType.WAVE,
    ContentType.PCM_S16LE,
    ContentType.PCM_S24LE,
)

DEFAULT_SUPPORTED_SAMPLE_RATES = (
    # if a player does not report/set its supported sample rates, we use a pretty safe default
    44100,
    48000,
    88200,
    96000,
)


@dataclass(frozen=True)
class DeviceInfo(DataClassDictMixin):
    """Model for a player's deviceinfo."""

    model: str = "unknown"
    address: str = "unknown"
    manufacturer: str = "unknown"


class Player(ABC):
    """Model for a music player."""

    player_id: str
    _attr_is_group: bool = False
    _attr_group_childs: List[str] = []
    _attr_name: str = ""
    _attr_powered: bool = False
    _attr_elapsed_time: float = 0
    _attr_current_url: str = ""
    _attr_state: PlayerState = PlayerState.IDLE
    _attr_available: bool = True
    _attr_volume_level: int = 100
    _attr_volume_muted: bool = False
    _attr_device_info: DeviceInfo = DeviceInfo()
    _attr_supported_content_types: Tuple[ContentType] = DEFAULT_SUPPORTED_CONTENT_TYPES
    _attr_supported_sample_rates: Tuple[int] = DEFAULT_SUPPORTED_SAMPLE_RATES
    _attr_active_queue_id: str = ""
    _attr_use_multi_stream: bool = False
    # below objects will be set by playermanager at register/update
    mass: MusicAssistant = None  # type: ignore[assignment]
    _attr_group_parents: List[str] = []  # will be set by player manager
    _prev_state: dict = {}

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
    def group_parents(self) -> List[str]:
        """Return all/any group player id's this player belongs to."""
        return self._attr_group_parents

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

    @property
    def supported_sample_rates(self) -> Tuple[int]:
        """Return the sample rates this player supports."""
        return self._attr_supported_sample_rates

    @property
    def supported_content_types(self) -> Tuple[ContentType]:
        """Return the content types this player supports."""
        return self._attr_supported_content_types

    @property
    def active_queue(self) -> PlayerQueue:
        """
        Return the currently active queue for this player.

        If the player is a group child this will return its parent when that is playing,
        otherwise it will return the player's own queue.
        """
        return self.mass.players.get_player_queue(self._attr_active_queue_id)

    @property
    def use_multi_stream(self) -> bool:
        """
        Return bool if this player needs multistream approach.

        This is used for groupplayers that do not distribute the audio streams over players.
        Instead this can be used as convenience service where each client receives the same audio
        at more or less the same time. The player's implementation will be responsible for
        synchronization of audio on child players (if possible), Music Assistant will only
        coordinate the start and makes sure that every child received the same audio chunk
        within the same timespan.
        """
        return self._attr_use_multi_stream

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

    def on_child_update(self, player_id: str, changed_keys: set) -> None:
        """Call when one of the child players of a playergroup updates."""
        self.update_state(skip_forward=True)

    def on_parent_update(self, player_id: str, changed_keys: set) -> None:
        """Call when (one of) the parent player(s) of a grouped player updates."""
        self.update_state(skip_forward=True)

    def on_remove(self) -> None:
        """Call when player is about to be removed (cleaned up) from player manager."""

    # DO NOT OVERRIDE BELOW

    def update_state(self, skip_forward: bool = False) -> None:
        """Update current player state in the player manager."""
        if self.mass is None or self.mass.closed:
            # guard
            return
        self._attr_group_parents = self._get_attr_group_parents()
        # determine active queue for player
        self._attr_active_queue_id = self._get_active_queue_id()
        # basic throttle: do not send state changed events if player did not change
        cur_state = self.to_dict()
        changed_keys = get_changed_keys(self._prev_state, cur_state)

        # always update the playerqueue
        self.mass.players.get_player_queue(self.player_id).on_player_update()

        if len(changed_keys) == 0:
            return

        self._prev_state = cur_state
        if changed_keys != {"elapsed_time"}:
            self.mass.signal_event(
                MassEvent(EventType.PLAYER_UPDATED, object_id=self.player_id, data=self)
            )

        if skip_forward:
            return
        if self.is_group:
            # update group player childs when parent updates
            for child_player_id in self.group_childs:
                if player := self.mass.players.get_player(child_player_id):
                    self.mass.create_task(
                        player.on_parent_update, self.player_id, changed_keys
                    )
            return
        # update group player when child updates
        for group_player_id in self._attr_group_parents:
            if player := self.mass.players.get_player(group_player_id):
                self.mass.create_task(
                    player.on_child_update, self.player_id, changed_keys
                )

    def _get_attr_group_parents(self) -> List[str]:
        """Get any/all group player id's this player belongs to."""
        return [
            x.player_id
            for x in self.mass.players
            if x.is_group and self.player_id in x.group_childs
        ]

    def _get_active_queue_id(self) -> str:
        """Return the currently active queue for this (grouped) player."""
        for player_id in self._attr_group_parents:
            player = self.mass.players.get_player(player_id)
            if not player or not player.powered:
                continue
            queue = self.mass.players.get_player_queue(player_id)
            if not queue or not queue.active:
                continue
            if queue.queue_id in player.current_url:
                # match found!
                return queue.queue_id
        return self.player_id

    def to_dict(self) -> Dict[str, Any]:
        """Export object to dict."""
        return {
            "player_id": self.player_id,
            "name": self.name,
            "powered": self.powered,
            "elapsed_time": int(self.elapsed_time),
            "state": self.state.value,
            "available": self.available,
            "is_group": self.is_group,
            "group_childs": self.group_childs,
            "group_parents": self.group_parents,
            "volume_level": int(self.volume_level),
            "device_info": self.device_info.to_dict(),
            "active_queue": self.active_queue.queue_id,
        }


class PlayerGroup(Player):
    """Convenience Model for a player group with some additional helper methods."""

    _attr_is_group: bool = True
    _attr_group_childs: List[str] = []

    @property
    def volume_level(self) -> int:
        """Return current volume level of player (scale 0..100)."""
        if not self.available:
            return 0
        # calculate group volume from powered players for convenience
        # may be overridden if implementation provides this natively
        group_volume = 0
        active_players = 0
        for child_player in self._get_child_players(True):
            group_volume += child_player.volume_level
            active_players += 1
        if active_players:
            group_volume = group_volume / active_players
        return int(group_volume)

    @property
    def elapsed_time(self) -> float:
        """Return the corrected/precise elsapsed time of the grouped player."""
        if not self.use_multi_stream:
            return super().elapsed_time
        # calculate from group childs
        for child_player in self._get_child_players(True):
            if not child_player.current_url:
                continue
            if self.player_id not in child_player.current_url:
                continue
            if child_player.state not in [PlayerState.PLAYING, PlayerState.PAUSED]:
                continue
            return child_player.elapsed_time
        return 0

    @property
    def state(self) -> PlayerState:
        """Return the state of the grouped player."""
        if not self.use_multi_stream:
            return super().state
        # calculate from group childs
        for child_player in self._get_child_players(True):
            if not child_player.current_url:
                continue
            if self.player_id not in child_player.current_url:
                continue
            if child_player.state not in [PlayerState.PLAYING, PlayerState.PAUSED]:
                continue
            return child_player.state
        return super().state

    @property
    def current_url(self) -> str:
        """Return the current_url of the grouped player."""
        if not self.use_multi_stream:
            return super().current_url
        # calculate from group childs
        for child_player in self._get_child_players(True):
            if not child_player.current_url:
                continue
            if self.player_id not in child_player.current_url:
                continue
            return child_player.current_url
        return super().current_url

    async def stop(self) -> None:
        """Send STOP command to player."""
        if not self.use_multi_stream:
            return await super().stop()
        # redirect command to all child players
        await asyncio.gather(*[x.stop() for x in self._get_child_players(True)])

    async def play(self) -> None:
        """Send PLAY/UNPAUSE command to player."""
        if not self.use_multi_stream:
            return await super().play()
        # redirect command to all child players
        await asyncio.gather(*[x.play() for x in self._get_child_players(True)])

    async def pause(self) -> None:
        """Send PAUSE command to player."""
        if not self.use_multi_stream:
            return await super().pause()
        # redirect command to all child players
        await asyncio.gather(*[x.pause() for x in self._get_child_players(True)])

    async def power(self, powered: bool) -> None:
        """Send POWER command to player."""
        if self.use_multi_stream:
            # redirect command to all child players
            await asyncio.gather(
                *[x.power(powered) for x in self._get_child_players(True)]
            )
        else:
            return await super().power(powered)

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
        for child_player in self._get_child_players(True):
            cur_child_volume = child_player.volume_level
            new_child_volume = cur_child_volume + (
                cur_child_volume * volume_dif_percent
            )
            await child_player.volume_set(new_child_volume)

    def _get_child_players(
        self, only_powered: bool = False, only_playing: bool = False
    ) -> List[Player]:
        """Get players attached to this group."""
        if not self.mass:
            return []
        child_players = []
        for child_id in self.group_childs:
            if child_player := self.mass.players.get_player(child_id):
                if not (not only_powered or child_player.powered):
                    continue
                if not (not only_playing or child_player.state == PlayerState.PLAYING):
                    continue
                child_players.append(child_player)
        return child_players

    def on_child_update(self, player_id: str, changed_keys: set) -> None:
        """Call when one of the child players of a playergroup updates."""
        self.update_state(True)

        # convenience helper:
        # power off group player if last child player turns off
        if "powered" not in changed_keys or not self.active_queue.active:
            return
        powered_childs = set()
        for child_id in self._attr_group_childs:
            if player := self.mass.players.get_player(child_id):
                if player.powered:
                    powered_childs.add(child_id)
        if self.powered and len(powered_childs) == 0:

            async def auto_turn_off_group():
                await self.active_queue.stop()
                await self.power(False)

            self.mass.create_task(auto_turn_off_group())
