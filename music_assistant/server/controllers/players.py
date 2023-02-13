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
    ProviderType,
)
from music_assistant.common.models.errors import (
    AlreadyRegisteredError,
    PlayerCommandFailed,
    PlayerUnavailableError,
    UnsupportedFeaturedException,
)
from music_assistant.common.models.player import Player
from music_assistant.constants import CONF_PLAYERS, ROOT_LOGGER_NAME
from music_assistant.server.helpers.api import api_command
from music_assistant.server.models.player_provider import PlayerProvider

from .player_queue import PlayerQueuesController

if TYPE_CHECKING:
    from music_assistant.server import MusicAssistant

LOGGER = logging.getLogger(f"{ROOT_LOGGER_NAME}.players")

# TODO: Implement volume fade effect (https://github.com/Logitech/slimserver/blob/public/8.4/Slim/Player/Player.pm#L373)


class PlayerController:
    """Controller holding all logic to control registered players."""

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize class."""
        self.mass = mass
        self._players: dict[str, Player] = {}
        self._prev_states: dict[str, dict] = {}
        self.queues = PlayerQueuesController(self)

    async def setup(self) -> None:
        """Async initialize of module."""

    async def close(self) -> None:
        """Cleanup on exit."""

    @property
    def providers(self) -> list[PlayerProvider]:
        """Return all loaded/running MusicProviders (instances)."""
        return self.mass.get_providers(ProviderType.MUSIC)

    def __iter__(self):
        """Iterate over (available) players."""
        return iter(self._players.values())

    @api_command("players/all")
    def all(self) -> tuple[Player]:
        """Return all registered players."""
        return tuple(self._players.values())

    @api_command("players/get")
    def get(
        self,
        player_id: str,
        raise_not_found: bool = False,
        raise_unavailable: bool = False,
    ) -> Player | None:
        """Return Player by player_id or None if not found."""
        player = self._players.get(player_id)
        if player is None and raise_not_found:
            raise PlayerUnavailableError(f"Player {player_id} does not exist")
        if player and not player.available and raise_unavailable:
            raise PlayerUnavailableError(f"Player {player_id} is not available")
        return player

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
        self.mass.create_task(self.queues.on_player_register(player))

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
        player.active_queue = self._get_active_queue(player)
        # calculate group volume
        player.group_volume = self._get_group_volume_level(player)
        # prefer any overridden name from config
        player.display_name = self.mass.config.get(
            f"{CONF_PLAYERS}/{player_id}/name", player.name
        )
        # set player state to off if player is not powered
        if player.powered and player.state == PlayerState.OFF:
            player.state = PlayerState.IDLE
        elif not player.powered:
            player.state = PlayerState.OFF
        # basic throttle: do not send state changed events if player did not actually change
        prev_state = self._prev_states.get(player_id, {})
        new_state = self._players[player_id].to_dict()
        changed_keys = get_changed_keys(
            prev_state,
            new_state,
            ignore_keys=["elapsed_time", "elapsed_time_last_updated"],
        )
        self._prev_states[player_id] = new_state

        if not player.enabled and "enabled" not in changed_keys:
            # ignore updates for disabled players
            return

        # always signal update to the playerqueue
        self.queues.on_player_update(player, changed_keys)

        if len(changed_keys) == 0:
            return

        self.mass.signal_event(
            EventType.PLAYER_UPDATED, object_id=player_id, data=player
        )

        if skip_forward:
            return
        if player.type == PlayerType.GROUP:
            # update group player childs when parent updates
            for child_player_id in player.group_childs:
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
        player = self.get(player_id, True, True)
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
        # TODO: Handle group power
        player = self.get(player_id, True, True)
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
        player = self.get(player_id, True, True)
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

    @api_command("players/cmd/group_volume")
    async def cmd_group_volume(self, player_id: str, volume_level: int) -> None:
        """
        Send VOLUME_SET command to given playergroup.

        Will send the new (average) volume level to group childs.
            - player_id: player_id of the playergroup to handle the command.
            - volume_level: volume level (0..100) to set on the player.
        """
        group_player = self.get(player_id, True, True)
        # handle group volume by only applying the volume to powered members
        cur_volume = group_player.volume_level
        new_volume = volume_level
        volume_dif = new_volume - cur_volume
        if cur_volume == 0:
            volume_dif_percent = 1 + (new_volume / 100)
        else:
            volume_dif_percent = volume_dif / cur_volume
        coros = []
        for child_player in self._get_child_players(group_player, True):
            cur_child_volume = child_player.volume_level
            new_child_volume = cur_child_volume + (
                cur_child_volume * volume_dif_percent
            )
            coros.append(self.cmd_volume_set(child_player.player_id, new_child_volume))
        await asyncio.gather(*coros)

    @api_command("players/cmd/volume_mute")
    async def cmd_volume_mute(self, player_id: str, muted: bool) -> None:
        """
        Send VOLUME_MUTE command to given player.
            - player_id: player_id of the player to handle the command.
            - muted: bool if player should be muted.
        """
        player = self.get(player_id, True, True)
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

    @api_command("players/cmd/sync")
    async def cmd_sync(self, player_id: str, target_player: str) -> None:
        """
        Handle SYNC command for given player.

        Join/add the given player(id) to the given (master) player/sync group.
        If the player is already synced to another player, it will be unsynced there first.
        If the target player itself is already synced to another player, this will fail.
        If the player can not be synced with the given target player, this will fail.

            - player_id: player_id of the player to handle the command.
            - target_player: player_id of the syncgroup master or group player.
        """
        child_player = self.get(player_id, True, True)
        master_player = self.get(target_player, True, True)
        if PlayerFeature.SYNC not in child_player.supported_features:
            raise UnsupportedFeaturedException(
                f"Player {child_player.name} does not support (unsync) commands"
            )
        if PlayerFeature.SYNC not in master_player.supported_features:
            raise UnsupportedFeaturedException(
                f"Player {master_player.name} does not support (unsync) commands"
            )
        if master_player.synced_to is not None:
            raise PlayerCommandFailed(
                f"Player {target_player} is already synced to another player."
            )
        if player_id not in master_player.can_sync_with:
            raise PlayerCommandFailed(
                f"Player {player_id} can not be synced to {target_player}."
            )
        # all checks passed, forward command to the player provider
        player_provider = self.get_player_provider(player_id)
        await player_provider.cmd_sync(player_id, target_player)

    @api_command("players/cmd/unsync")
    async def cmd_unsync(self, player_id: str) -> None:
        """
        Handle UNSYNC command for given player.

        Remove the given player from any syncgroups it currently is synced to.
        If the player is not currently synced to any other player,
        this will silently be ignored.

            - player_id: player_id of the player to handle the command.
        """
        player = self.get(player_id, True, True)
        if PlayerFeature.SYNC not in player.supported_features:
            raise UnsupportedFeaturedException(
                f"Player {player.name} does not support syncing"
            )
        if not player.synced_to:
            LOGGER.info(
                "Ignoring command to unsync player %s "
                "because it is currently not part of a (sync)group."
            )
            return

        # all checks passed, forward command to the player provider
        player_provider = self.get_player_provider(player_id)
        await player_provider.cmd_unsync(player_id)

    @api_command("players/cmd/set_members")
    async def cmd_set_members(self, player_id: str, members: list[str]) -> None:
        """
        Handle SET_MEMBERS command for given playergroup.

        Update the memberlist of the given PlayerGroup.

            - player_id: player_id of the groupplayer to handle the command.
            - members: list of player ids to set as members.
        """
        player = self.get(player_id, True, True)
        if player.type != PlayerType.GROUP:
            raise UnsupportedFeaturedException(f"{player.name} is not a PlayerGroup")
        if PlayerFeature.SET_MEMBERS not in player.supported_features:
            raise UnsupportedFeaturedException(
                f"PlayerGroup {player.name} does not support updating the members"
            )
        for member_id in members:
            member_player = self.get(member_id, True, True)
            if (
                member_player.player_id not in player.can_sync_with
                and "*" not in player.can_sync_with
            ):
                raise PlayerCommandFailed(
                    f"Player {member_id} can not be a member of {player_id}."
                )

        # all checks passed, forward command to the player provider
        player_provider = self.get_player_provider(player_id)
        await player_provider.cmd_unsync(player_id)

    def _get_player_groups(self, player_id: str) -> tuple[Player, ...]:
        """Return all (player_ids of) any groupplayers the given player belongs to."""
        return tuple(x for x in self if player_id in x.group_childs)

    def _get_active_queue(self, player: Player) -> str:
        """Return the active_queue id for given player."""
        # if player is synced, return master/group leader
        if player.synced_to:
            return player.synced_to
        # iterate player groups to find out if one is playing
        if group_players := self._get_player_groups(player.player_id):
            # prefer the first playing (or paused) group parent
            for group_player in group_players:
                if group_player.state in (PlayerState.PLAYING, PlayerState.PAUSED):
                    return group_player.player_id
            # fallback to the first powered group player
            for group_player in group_players:
                if group_player.powered:
                    return group_player.player_id
        # defaults to the player's own player id
        return player.player_id

    def _get_group_volume_level(self, player: Player) -> int:
        """Calculate a group volume from the grouped members."""
        if not player.group_childs:
            # player is not a group
            return player.volume_level
        # calculate group volume from all (turned on) players
        group_volume = 0
        active_players = 0
        for child_player in self._get_child_players(player, True):
            group_volume += child_player.volume_level
            active_players += 1
        if active_players:
            group_volume = group_volume / active_players
        return int(group_volume)

    def _get_child_players(
        self,
        player: Player,
        only_powered: bool = False,
        only_playing: bool = False,
    ) -> list[Player]:
        """Get (child) players attached to a grouped player."""
        child_players = []
        if not player.group_childs:
            # player is not a group
            return child_players
        if player.type != PlayerType.GROUP:
            # if the player is not a dedicated player group,
            # it is the master in a sync group and thus always present as child player
            child_players.append(player)
        for child_id in player.group_childs:
            if child_player := self.get(child_id):
                if not (not only_powered or child_player.powered):
                    continue
                if not (
                    not only_playing
                    or child_player.state in (PlayerState.PLAYING, PlayerState.PAUSED)
                ):
                    continue
                child_players.append(child_player)
        return child_players
