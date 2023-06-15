"""Logic to play music from MusicProviders to supported players."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from typing import TYPE_CHECKING, cast

from music_assistant.common.helpers.util import get_changed_values
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
from music_assistant.constants import CONF_HIDE_GROUP_CHILDS, CONF_PLAYERS, ROOT_LOGGER_NAME
from music_assistant.server.helpers.api import api_command
from music_assistant.server.models.player_provider import PlayerProvider

from .player_queues import PlayerQueuesController

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
        self.queues = PlayerQueuesController(self)

    async def setup(self) -> None:
        """Async initialize of module."""
        self.mass.create_task(self._poll_players())

    async def close(self) -> None:
        """Cleanup on exit."""
        await self.queues.close()

    @property
    def providers(self) -> list[PlayerProvider]:
        """Return all loaded/running MusicProviders."""
        return self.mass.get_providers(ProviderType.MUSIC)  # type: ignore=return-value

    def __iter__(self) -> Iterator[Player]:
        """Iterate over (available) players."""
        return iter(self._players.values())

    @api_command("players/all")
    def all(
        self,
        return_unavailable: bool = True,
        return_hidden: bool = True,
        return_disabled: bool = False,
    ) -> tuple[Player, ...]:
        """Return all registered players."""
        return tuple(
            player
            for player in self._players.values()
            if (player.available or return_unavailable)
            and (not player.hidden_by or return_hidden)
            and (player.enabled or return_disabled)
        )

    @api_command("players/get")
    def get(
        self,
        player_id: str,
        raise_unavailable: bool = False,
    ) -> Player | None:
        """Return Player by player_id."""
        if player := self._players.get(player_id):
            if (not player.available or not player.enabled) and raise_unavailable:
                raise PlayerUnavailableError(f"Player {player_id} is not available")
            return player
        if raise_unavailable:
            raise PlayerUnavailableError(f"Player {player_id} is not available")
        return None

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
        if self.mass.closing:
            return
        player_id = player.player_id

        if player_id in self._players:
            raise AlreadyRegisteredError(f"Player {player_id} is already registered")

        # make sure a default config exists
        self.mass.config.create_default_player_config(player_id, player.provider, player.name)

        player.enabled = self.mass.config.get(f"{CONF_PLAYERS}/{player_id}/enabled", True)

        # register playerqueue for this player
        self.mass.create_task(self.queues.on_player_register(player))

        self._players[player_id] = player

        # ignore disabled players
        if not player.enabled:
            return

        LOGGER.info(
            "Player registered: %s/%s",
            player_id,
            player.name,
        )
        self.mass.signal_event(EventType.PLAYER_ADDED, object_id=player.player_id, data=player)
        # always call update to fix special attributes like display name, group volume etc.
        self.update(player.player_id)

    @api_command("players/register_or_update")
    def register_or_update(self, player: Player) -> None:
        """Register a new player on the controller or update existing one."""
        if self.mass.closing:
            return

        if player.player_id in self._players:
            self.update(player.player_id)
            return

        self.register(player)

    @api_command("players/remove")
    def remove(self, player_id: str) -> None:
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
    def update(
        self, player_id: str, skip_forward: bool = False, force_update: bool = False
    ) -> None:
        """Update player state."""
        if self.mass.closing:
            return
        if player_id not in self._players:
            return
        player = self._players[player_id]
        # calculate active_source
        player.active_source = self._get_active_source(player)
        # calculate group volume
        player.group_volume = self._get_group_volume_level(player)
        # prefer any overridden name from config
        player.display_name = (
            self.mass.config.get(f"{CONF_PLAYERS}/{player_id}/name")
            or player.name
            or player.player_id
        )
        # set player state to off if player is not powered
        if player.powered and player.state == PlayerState.OFF:
            player.state = PlayerState.IDLE
        elif not player.powered:
            player.state = PlayerState.OFF
        # basic throttle: do not send state changed events if player did not actually change
        prev_state = self._prev_states.get(player_id, {})
        new_state = self._players[player_id].to_dict()
        changed_values = get_changed_values(
            prev_state,
            new_state,
            ignore_keys=["elapsed_time", "elapsed_time_last_updated", "seq_no"],
        )
        self._prev_states[player_id] = new_state

        if not player.enabled and not force_update:
            # ignore updates for disabled players
            return

        # always signal update to the playerqueue
        self.queues.on_player_update(player, changed_values)

        if len(changed_values) == 0 and not force_update:
            return

        self.mass.signal_event(EventType.PLAYER_UPDATED, object_id=player_id, data=player)

        if skip_forward:
            return
        if player.type == PlayerType.GROUP:
            # update group player child's when parent updates
            hide_group_childs = self.mass.config.get_raw_player_config_value(
                player.player_id, CONF_HIDE_GROUP_CHILDS, "active"
            )
            for child_player in self._get_child_players(player):
                if child_player.player_id == player.player_id:
                    continue
                # handle 'hide group childs' feature here
                if hide_group_childs == "always":  # noqa: SIM114
                    child_player.hidden_by.add(player.player_id)
                elif player.powered and hide_group_childs == "active":
                    child_player.hidden_by.add(player.player_id)
                elif not player.powered and player.player_id in child_player.hidden_by:
                    child_player.hidden_by.remove(player.player_id)
                self.update(child_player.player_id, skip_forward=True)

        # update group player(s) when child updates
        for group_player in self._get_player_groups(player_id):
            if not group_player.available:
                continue
            player_prov = self.get_player_provider(group_player.player_id)
            if not player_prov:
                continue
            player_prov.on_child_state(group_player.player_id, player, changed_values)

    def get_player_provider(self, player_id: str) -> PlayerProvider:
        """Return PlayerProvider for given player."""
        player = self._players[player_id]
        player_provider = self.mass.get_provider(player.provider)
        return cast(PlayerProvider, player_provider)

    # Player commands

    @api_command("players/cmd/stop")
    async def cmd_stop(self, player_id: str) -> None:
        """Send STOP command to given player.

        - player_id: player_id of the player to handle the command.
        """
        player_id = self._check_redirect(player_id)
        player_provider = self.get_player_provider(player_id)
        await player_provider.cmd_stop(player_id)

    @api_command("players/cmd/play")
    async def cmd_play(self, player_id: str) -> None:
        """Send PLAY (unpause) command to given player.

        - player_id: player_id of the player to handle the command.
        """
        player_id = self._check_redirect(player_id)
        player_provider = self.get_player_provider(player_id)
        await player_provider.cmd_play(player_id)

    @api_command("players/cmd/pause")
    async def cmd_pause(self, player_id: str) -> None:
        """Send PAUSE command to given player.

        - player_id: player_id of the player to handle the command.
        """
        player_id = self._check_redirect(player_id)
        player_provider = self.get_player_provider(player_id)
        await player_provider.cmd_pause(player_id)

        async def _watch_pause(_player_id: str) -> None:
            player = self.get(_player_id, True)
            count = 0
            # wait for pause
            while count < 5 and player.state == PlayerState.PLAYING:
                count += 1
                await asyncio.sleep(1)
            # wait for unpause
            if player.state != PlayerState.PAUSED:
                return
            count = 0
            while count < 30 and player.state == PlayerState.PAUSED:
                count += 1
                await asyncio.sleep(1)
            # if player is still paused when the limit is reached, send stop
            if player.state == PlayerState.PAUSED:
                await self.cmd_stop(_player_id)

        # we auto stop a player from paused when its paused for 30 seconds
        self.mass.create_task(_watch_pause(player_id))

    @api_command("players/cmd/play_pause")
    async def cmd_play_pause(self, player_id: str) -> None:
        """Toggle play/pause on given player.

        - player_id: player_id of the player to handle the command.
        """
        player = self.get(player_id, True)
        if player.state == PlayerState.PLAYING:
            await self.cmd_pause(player_id)
        else:
            await self.cmd_play(player_id)

    @api_command("players/cmd/power")
    async def cmd_power(self, player_id: str, powered: bool) -> None:
        """Send POWER command to given player.

        - player_id: player_id of the player to handle the command.
        - powered: bool if player should be powered on or off.
        """
        # TODO: Implement PlayerControl
        # TODO: Handle group power
        player = self.get(player_id, True)
        if player.powered == powered:
            return
        # stop player at power off
        if (
            not powered
            and player.state in (PlayerState.PLAYING, PlayerState.PAUSED)
            and not player.synced_to
        ):
            await self.cmd_stop(player_id)
        # unsync player at power off
        if not powered:
            if player.synced_to is not None:
                await self.cmd_unsync(player_id)
            for child in self._get_child_players(player):
                if not child.synced_to:
                    continue
                await self.cmd_unsync(child.player_id)
        if PlayerFeature.POWER not in player.supported_features:
            player.powered = powered
            self.update(player_id)
            return
        player_provider = self.get_player_provider(player_id)
        await player_provider.cmd_power(player_id, powered)

    @api_command("players/cmd/volume_set")
    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """Send VOLUME_SET command to given player.

        - player_id: player_id of the player to handle the command.
        - volume_level: volume level (0..100) to set on the player.
        """
        # TODO: Implement PlayerControl
        player = self.get(player_id, True)
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

    @api_command("players/cmd/volume_up")
    async def cmd_volume_up(self, player_id: str) -> None:
        """Send VOLUME_UP command to given player.

        - player_id: player_id of the player to handle the command.
        """
        new_volume = min(100, self._players[player_id].volume_level + 5)
        await self.cmd_volume_set(player_id, new_volume)

    @api_command("players/cmd/volume_down")
    async def cmd_volume_down(self, player_id: str) -> None:
        """Send VOLUME_DOWN command to given player.

        - player_id: player_id of the player to handle the command.
        """
        new_volume = max(0, self._players[player_id].volume_level - 5)
        await self.cmd_volume_set(player_id, new_volume)

    @api_command("players/cmd/group_volume")
    async def cmd_group_volume(self, player_id: str, volume_level: int) -> None:
        """Send VOLUME_SET command to given playergroup.

        Will send the new (average) volume level to group child's.
            - player_id: player_id of the playergroup to handle the command.
            - volume_level: volume level (0..100) to set on the player.
        """
        group_player = self.get(player_id, True)
        assert group_player
        # handle group volume by only applying the volume to powered members
        cur_volume = group_player.group_volume
        new_volume = volume_level
        volume_dif = new_volume - cur_volume
        volume_dif_percent = 1 + new_volume / 100 if cur_volume == 0 else volume_dif / cur_volume
        coros = []
        for child_player in self._get_child_players(group_player, True):
            cur_child_volume = child_player.volume_level
            new_child_volume = int(cur_child_volume + (cur_child_volume * volume_dif_percent))
            coros.append(self.cmd_volume_set(child_player.player_id, new_child_volume))
        await asyncio.gather(*coros)

    @api_command("players/cmd/volume_mute")
    async def cmd_volume_mute(self, player_id: str, muted: bool) -> None:
        """Send VOLUME_MUTE command to given player.

        - player_id: player_id of the player to handle the command.
        - muted: bool if player should be muted.
        """
        player = self.get(player_id, True)
        assert player
        if PlayerFeature.VOLUME_MUTE not in player.supported_features:
            LOGGER.warning("Mute command called but player %s does not support muting", player_id)
            player.volume_muted = muted
            self.update(player_id)
            return
        # TODO: Implement PlayerControl
        player_provider = self.get_player_provider(player_id)
        await player_provider.cmd_volume_mute(player_id, muted)

    @api_command("players/cmd/sync")
    async def cmd_sync(self, player_id: str, target_player: str) -> None:
        """Handle SYNC command for given player.

        Join/add the given player(id) to the given (master) player/sync group.
        If the player is already synced to another player, it will be unsynced there first.
        If the target player itself is already synced to another player, this will fail.
        If the player can not be synced with the given target player, this will fail.

            - player_id: player_id of the player to handle the command.
            - target_player: player_id of the syncgroup master or group player.
        """
        child_player = self.get(player_id, True)
        parent_player = self.get(target_player, True)
        assert child_player
        assert parent_player
        if PlayerFeature.SYNC not in child_player.supported_features:
            raise UnsupportedFeaturedException(
                f"Player {child_player.name} does not support (un)sync commands"
            )
        if PlayerFeature.SYNC not in parent_player.supported_features:
            raise UnsupportedFeaturedException(
                f"Player {parent_player.name} does not support (un)sync commands"
            )
        if parent_player.synced_to is not None:
            raise PlayerCommandFailed(
                f"Player {target_player} is already synced to another player."
            )
        if player_id not in parent_player.can_sync_with:
            raise PlayerCommandFailed(f"Player {player_id} can not be synced to {target_player}.")
        if child_player.synced_to:
            if child_player.synced_to == parent_player.player_id:
                # nothing to do: already synced to this parent
                return
            # player already synced, unsync first
            await self.cmd_unsync(child_player.player_id)
        # stop child player if it is currently playing
        if child_player.state == PlayerState.PLAYING:
            await self.cmd_stop(player_id)
        # all checks passed, forward command to the player provider
        child_player.hidden_by.add(target_player)
        player_provider = self.get_player_provider(player_id)
        await player_provider.cmd_sync(player_id, target_player)

    @api_command("players/cmd/unsync")
    async def cmd_unsync(self, player_id: str) -> None:
        """Handle UNSYNC command for given player.

        Remove the given player from any syncgroups it currently is synced to.
        If the player is not currently synced to any other player,
        this will silently be ignored.

            - player_id: player_id of the player to handle the command.
        """
        player = self.get(player_id, True)
        if PlayerFeature.SYNC not in player.supported_features:
            raise UnsupportedFeaturedException(f"Player {player.name} does not support syncing")
        if not player.synced_to:
            LOGGER.info(
                "Ignoring command to unsync player %s "
                "because it is currently not part of a (sync)group."
            )
            return

        # all checks passed, forward command to the player provider
        if player.synced_to in player.hidden_by:
            player.hidden_by.remove(player.synced_to)
        player_provider = self.get_player_provider(player_id)
        await player_provider.cmd_unsync(player_id)

    def _check_redirect(self, player_id: str) -> str:
        """Check if playback related command should be redirected."""
        player = self.get(player_id, True)
        if player.synced_to:
            sync_master = self.get(player.synced_to, True)
            LOGGER.warning(
                "Player %s is synced to %s and can not accept "
                "playback related commands itself, "
                "redirected the command to the sync leader.",
                player.name,
                sync_master.name,
            )
            return player.synced_to
        return player_id

    def _get_player_groups(self, player_id: str) -> tuple[Player, ...]:
        """Return all (player_ids of) any groupplayers the given player belongs to."""
        return tuple(x for x in self if x.type == PlayerType.GROUP and player_id in x.group_childs)

    def _get_active_source(self, player: Player) -> str:
        """Return the active_source id for given player."""
        # if player is synced, return master/group leader
        if player.synced_to and player.synced_to in self._players:
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
        # guess source from player's current url
        if player.current_url and player.state in (PlayerState.PLAYING, PlayerState.PAUSED):
            if self.mass.webserver.base_url in player.current_url:
                return player.player_id
            if ":" in player.current_url:
                # extract source from uri/url
                return player.current_url.split(":")[0]
            return player.current_item_id or player.current_url
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
        child_players: list[Player] = []
        if not player.group_childs:
            # player is not a group
            return child_players
        if player.type != PlayerType.GROUP:
            # if the player is not a dedicated player group,
            # it is the master in a sync group and thus always present as child player
            child_players.append(player)
        for child_id in player.group_childs:
            if child_player := self.get(child_id, False):
                if not (not only_powered or child_player.powered):
                    continue
                if not (
                    not only_playing
                    or child_player.state in (PlayerState.PLAYING, PlayerState.PAUSED)
                ):
                    continue
                child_players.append(child_player)
        return child_players

    async def _poll_players(self) -> None:
        """Background task that polls players for updates."""
        count = 0
        while True:
            count += 1
            for player in list(self._players.values()):
                player_id = player.player_id
                # if the player is playing, update elapsed time every tick
                # to ensure the queue has accurate details
                player_playing = (
                    player.active_source == player.player_id and player.state == PlayerState.PLAYING
                )
                if player_playing:
                    self.mass.loop.call_soon(self.update, player_id)
                # Poll player;
                # - every 360 seconds if the player if not powered
                # - every 30 seconds if the player is powered
                # - every 10 seconds if the player is playing
                if (
                    (player.available and player.powered and count % 30 == 0)
                    or (player.available and player_playing and count % 10 == 0)
                    or count == 360
                ) and (player_prov := self.get_player_provider(player_id)):
                    try:
                        await player_prov.poll_player(player_id)
                    except PlayerUnavailableError:
                        player.available = False
                        player.state = PlayerState.IDLE
                        player.powered = False
                        self.mass.loop.call_soon(self.update, player_id)
                    except Exception as err:  # pylint: disable=broad-except
                        LOGGER.warning(
                            "Error while requesting latest state from player %s: %s",
                            player.display_name,
                            str(err),
                            exc_info=err,
                        )
                    if count >= 360:
                        count = 0
            await asyncio.sleep(1)
