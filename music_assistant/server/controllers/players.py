"""Logic to play music from MusicProviders to supported players."""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, cast

from music_assistant.common.helpers.util import get_changed_keys
from music_assistant.common.models.enums import (
    EventType,
    PlayerFeature,
    PlayerState,
    PlayerType,
)
from music_assistant.common.models.errors import AlreadyRegisteredError
from music_assistant.common.models.player import Player
from music_assistant.constants import ROOT_LOGGER_NAME
from music_assistant.server.helpers.api import api_command
from music_assistant.server.models.player_provider import PlayerProvider

from .player_queue import PlayerQueuesController

if TYPE_CHECKING:
    from music_assistant.server import MusicAssistant

LOGGER = logging.getLogger(f"{ROOT_LOGGER_NAME}.players")


class PlayerController:
    """Controller holding all logic to control registered players."""

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize class."""
        self.mass = mass
        self._players: dict[str, Player] = {}
        self._prev_states: dict[str, dict] = {}
        self.queues = PlayerQueuesController()

    async def setup(self) -> None:
        """Async initialize of module."""
        self.mass.create_task(self._poll_players())

    async def cleanup(self) -> None:
        """Cleanup on exit."""
        for player_id in set(self._players.keys()):
            player = self._players.pop(player_id)
            player.on_remove()

    def __iter__(self):
        """Iterate over (available) players."""
        return iter(self._players.values())

    @api_command("players/all")
    def all(self) -> tuple[Player]:
        """Return all registered players."""
        return tuple(self._players.values())

    @api_command("players/get")
    def get(self, player_id: str) -> Player | None:
        """Return Player by player_id or None if not found."""
        return self._players.get(player_id)

    @api_command("players/get_by_name")
    def get_by_name(self, name: str) -> Player | None:
        """Return Player by name or None if no match is found."""
        return next((x for x in self._players.values() if x.name == name), None)

    @api_command("players/set")
    def set(self, player: Player) -> None:
        """Set/Update player details on the controller."""
        if player.player_id not in self._players:
            # new player
            self.register(player)
            return
        self._players[player.player_id] = player
        self.update(player.player_id)

    @api_command("players/register")
    def register(self, player: Player) -> None:
        """Register a new player on the controller."""
        if self.mass.closed:
            return
        player_id = player.player_id

        if player_id in self._players:
            raise AlreadyRegisteredError(f"Player {player_id} is already registered")

        # register playerqueue for this player
        self.mass.create_task(self.queues.on_player_register(player_id))

        self._players[player_id] = player

        LOGGER.info(
            "Player registered: %s/%s",
            player_id,
            player.name,
        )
        self.mass.signal_event(
            EventType.PLAYER_ADDED, object_id=player.player_id, data=player
        )

    @api_command("players/remove")
    def remove(self, player_id: str):
        """Remove a player from the registry."""
        player = self._players.pop(player_id, None)
        if player is None:
            return
        LOGGER.info("Player removed: %s", player.name)
        self.queues.on_player_remove(player_id)
        self.mass.config.remove(f"players/{player_id}")
        self._prev_states.pop(player_id, None)
        self.mass.signal_event(EventType.PLAYER_REMOVED, player_id)

    @api_command("players/update")
    def update(self, player_id: str, skip_forward: bool = False) -> None:
        """Update player state."""
        if player_id not in self._players:
            return
        player = self._players[player_id]
        # calculate active_queue
        player.active_queue = self._get_active_queue(player_id)
        # basic throttle: do not send state changed events if player did not actually change
        prev_state = self._prev_states.get(player_id, {})
        new_state = self._players[player_id].to_dict()
        changed_keys = get_changed_keys(
            prev_state, new_state, ignore_keys=["elapsed_time"]
        )

        if len(changed_keys) == 0:
            return

        # signal update to the playerqueue
        self.queues.on_player_update(player_id)

        self.mass.signal_event(
            EventType.PLAYER_UPDATED, object_id=player_id, data=player
        )

        if skip_forward:
            return
        if player.type == PlayerType.GROUP:
            # update group player members when parent updates
            for child_player_id in player.group_members:
                if child_player_id == player_id:
                    continue
                self.update(child_player_id, skip_forward=True)

        # update group player(s) when child updates
        for group_player in self._get_player_groups(player_id):
            self.update(group_player.player_id, skip_forward=True)

    def get_player_provider(self, player_id: str) -> PlayerProvider:
        """Return PlayerProvider for given player."""
        player = self._players[player_id]
        player_provider = self.mass.get_provider(player.provider)
        return cast(PlayerProvider, player_provider)

    # Player commands

    @api_command("players/cmd/stop")
    async def cmd_stop(self, player_id: str) -> None:
        """
        Send STOP command to given player.
            - player_id: player_id of the player to handle the command.
        """
        player_provider = self.get_player_provider(player_id)
        await player_provider.cmd_stop(player_id)

    @api_command("players/cmd/play")
    async def cmd_play(self, player_id: str) -> None:
        """
        Send PLAY (unpause) command to given player.
            - player_id: player_id of the player to handle the command.
        """
        player_provider = self.get_player_provider(player_id)
        await player_provider.cmd_play(player_id)

    @api_command("players/cmd/pause")
    async def cmd_pause(self, player_id: str) -> None:
        """
        Send PAUSE command to given player.
            - player_id: player_id of the player to handle the command.
        """
        player_provider = self.get_player_provider(player_id)
        await player_provider.cmd_pause(player_id)

    @api_command("players/cmd/play_pause")
    async def cmd_play_pause(self, player_id: str) -> None:
        """
        Toggle play/pause on given player.
            - player_id: player_id of the player to handle the command.
        """
        player = self._players[player_id]
        if player.state == PlayerState.PLAYING:
            await self.cmd_pause(player_id)
        else:
            await self.cmd_play(player_id)

    @api_command("players/cmd/power")
    async def cmd_power(self, player_id: str, powered: bool) -> None:
        """
        Send POWER command to given player.
            - player_id: player_id of the player to handle the command.
            - powered: bool if player should be powered on or off.
        """

        # TODO: Implement PlayerControl
        player = self._players[player_id]
        # stop player at power off
        if not powered and player.state in (PlayerState.PLAYING, PlayerState.PAUSED):
            await self.cmd_stop(player_id)
        if PlayerFeature.POWER not in player.supported_features:
            LOGGER.warning(
                "Power command called but player %s does not support setting power",
                player_id,
            )
            player.powered = powered
            self.update(player_id)
            return
        player_provider = self.get_player_provider(player_id)
        await player_provider.cmd_power(player_id, powered)

    @api_command("players/cmd/volume_set")
    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """
        Send VOLUME_SET command to given player.
            - player_id: player_id of the player to handle the command.
            - volume_level: volume level (0..100) to set on the player.
        """
        # TODO: Implement PlayerControl
        player = self._players[player_id]
        if PlayerFeature.VOLUME_SET not in player.supported_features:
            LOGGER.warning(
                "Volume set command called but player %s does not support volume",
                player_id,
            )
            player.volume_level = volume_level
            self.update(player_id)
            return
        player_provider = self.get_player_provider(player_id)
        await player_provider.cmd_volume_set(player_id, volume_level)

    @api_command("players/cmd/volume_mute")
    async def cmd_volume_mute(self, player_id: str, muted: bool) -> None:
        """
        Send VOLUME_MUTE command to given player.
            - player_id: player_id of the player to handle the command.
            - muted: bool if player should be muted.
        """
        player = self._players[player_id]
        if PlayerFeature.VOLUME_MUTE not in player.supported_features:
            LOGGER.warning(
                "Mute command called but player %s does not support muting", player_id
            )
            player.volume_muted = muted
            self.update(player_id)
            return
        # TODO: Implement PlayerControl
        player_provider = self.get_player_provider(player_id)
        await player_provider.cmd_volume_mute(player_id, muted)

    def _get_player_groups(self, player_id: str) -> tuple[Player, ...]:
        """Return all (player_ids of) any groupplayers the given player belongs to."""
        return tuple(x for x in self if player_id in x.group_members)

    def _get_active_queue(self, player_id: str) -> str:
        """Return the active_queue id for given player."""
        if group_players := self._get_player_groups(player_id):
            # prefer the first playing (or paused) group parent
            for group_player in group_players:
                if group_player.state in (PlayerState.PLAYING, PlayerState.PAUSED):
                    return group_player.player_id
            # fallback to the first powered group player
            for group_player in group_players:
                if group_player.powered:
                    return group_player.player_id
        # defaults to the player's own player id
        return player_id

    async def _poll_players(self) -> None:
        """Poll players every X interval."""
        interval = 30
        cur_tick = 0
        while True:
            for player in self.players:
                if not player.available:
                    continue
                if cur_tick == interval:
                    self.mass.loop.call_soon(player.update_state)
                if player.queue.active and player.state == PlayerState.PLAYING:
                    self.mass.loop.call_soon(player.queue.on_player_update)
            if cur_tick == interval:
                cur_tick = 0
            else:
                cur_tick += 1
            await asyncio.sleep(1)
