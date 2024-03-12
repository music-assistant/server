"""Logic to play music from MusicProviders to supported players."""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import TYPE_CHECKING, Any, Concatenate, ParamSpec, TypeVar, cast

import shortuuid

from music_assistant.common.helpers.util import get_changed_values
from music_assistant.common.models.enums import (
    EventType,
    PlayerFeature,
    PlayerState,
    PlayerType,
    ProviderFeature,
    ProviderType,
)
from music_assistant.common.models.errors import (
    AlreadyRegisteredError,
    PlayerUnavailableError,
    ProviderUnavailableError,
    UnsupportedFeaturedException,
)
from music_assistant.common.models.player import DeviceInfo, Player
from music_assistant.constants import (
    CONF_AUTO_PLAY,
    CONF_GROUP_MEMBERS,
    CONF_HIDE_PLAYER,
    CONF_PLAYERS,
    ROOT_LOGGER_NAME,
    SYNCGROUP_PREFIX,
)
from music_assistant.server.helpers.api import api_command
from music_assistant.server.models.core_controller import CoreController
from music_assistant.server.models.player_provider import PlayerProvider

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine, Iterable, Iterator

    from music_assistant.common.models.config_entries import CoreConfig
    from music_assistant.common.models.queue_item import QueueItem

LOGGER = logging.getLogger(f"{ROOT_LOGGER_NAME}.players")

_PlayerControllerT = TypeVar("_PlayerControllerT", bound="PlayerController")
_R = TypeVar("_R")
_P = ParamSpec("_P")


def log_player_command(
    func: Callable[Concatenate[_PlayerControllerT, _P], Awaitable[_R]],
) -> Callable[Concatenate[_PlayerControllerT, _P], Coroutine[Any, Any, _R | None]]:
    """Check and log commands to players."""

    @functools.wraps(func)
    async def wrapper(self: _PlayerControllerT, *args: _P.args, **kwargs: _P.kwargs) -> _R | None:
        """Log and log_player_command commands to players."""
        player_id = kwargs["player_id"] if "player_id" in kwargs else args[0]
        if (player := self._players.get(player_id)) is None or not player.available:
            # player not existent
            self.logger.debug(
                "Ignoring command %s for unavailable player %s",
                func.__name__,
                player_id,
            )
            return
        self.logger.debug(
            "Handling command %s for player %s",
            func.__name__,
            player.display_name,
        )
        await func(self, *args, **kwargs)

    return wrapper


class PlayerController(CoreController):
    """Controller holding all logic to control registered players."""

    domain: str = "players"

    def __init__(self, *args, **kwargs) -> None:
        """Initialize core controller."""
        super().__init__(*args, **kwargs)
        self._players: dict[str, Player] = {}
        self._prev_states: dict[str, dict] = {}
        self.manifest.name = "Players controller"
        self.manifest.description = (
            "Music Assistant's core controller which manages all players from all providers."
        )
        self.manifest.icon = "speaker-multiple"
        self._poll_task: asyncio.Task | None = None

    async def setup(self, config: CoreConfig) -> None:
        """Async initialize of module."""
        self._poll_task = self.mass.create_task(self._poll_players())

    async def close(self) -> None:
        """Cleanup on exit."""
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()

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
        return_disabled: bool = False,
    ) -> tuple[Player, ...]:
        """Return all registered players."""
        return tuple(
            player
            for player in self._players.values()
            if (player.available or return_unavailable) and (player.enabled or return_disabled)
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
                msg = f"Player {player_id} is not available"
                raise PlayerUnavailableError(msg)
            return player
        if raise_unavailable:
            msg = f"Player {player_id} is not available"
            raise PlayerUnavailableError(msg)
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
            msg = f"Player {player_id} is already registered"
            raise AlreadyRegisteredError(msg)

        # make sure that the player's provider is set to the instance id
        if prov := self.mass.get_provider(player.provider):
            player.provider = prov.instance_id
        else:
            raise RuntimeError("Invalid provider ID given: %s", player.provider)

        # make sure a default config exists
        self.mass.config.create_default_player_config(
            player_id, player.provider, player.name, player.enabled_by_default
        )

        player.enabled = self.mass.config.get(f"{CONF_PLAYERS}/{player_id}/enabled", True)

        # register playerqueue for this player
        self.mass.create_task(self.mass.player_queues.on_player_register(player))

        self._players[player_id] = player

        # ignore disabled players
        if not player.enabled:
            return

        # initialize sync groups as soon as a player is registered
        self.mass.loop.create_task(self._register_syncgroups())

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
            self._players[player.player_id] = player
            self.update(player.player_id)
            return

        self.register(player)

    @api_command("players/remove")
    def remove(self, player_id: str, cleanup_config: bool = True) -> None:
        """Remove a player from the registry."""
        player = self._players.pop(player_id, None)
        if player is None:
            return
        LOGGER.info("Player removed: %s", player.name)
        self.mass.player_queues.on_player_remove(player_id)
        if cleanup_config:
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
        # calculate active_source (if needed)
        player.active_source = self._get_active_source(player)
        # calculate group volume
        player.group_volume = self._get_group_volume_level(player)
        if player.type in (PlayerType.GROUP, PlayerType.SYNC_GROUP):
            player.volume_level = player.group_volume
        # prefer any overridden name from config
        player.display_name = (
            self.mass.config.get_raw_player_config_value(player.player_id, "name")
            or player.name
            or player.player_id
        )
        if (
            not player.powered
            and player.state == PlayerState.PLAYING
            and PlayerFeature.POWER not in player.supported_features
            and player.active_source == player_id
        ):
            # mark player as powered if its playing
            # could happen for players that do not officially support power commands
            player.powered = True
        player.hidden = self.mass.config.get_raw_player_config_value(
            player.player_id, CONF_HIDE_PLAYER, False
        )
        # handle syncgroup - get attributes from first player that has this group as source
        if player.player_id.startswith(SYNCGROUP_PREFIX):
            if player.powered and (sync_leader := self.get_sync_leader(player)):
                player.state = sync_leader.state
                player.current_item_id = sync_leader.current_item_id
                player.elapsed_time = sync_leader.elapsed_time
                player.elapsed_time_last_updated = sync_leader.elapsed_time_last_updated
            else:
                player.state = PlayerState.IDLE

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
        self.mass.player_queues.on_player_update(player, changed_values)

        if len(changed_values) == 0 and not force_update:
            return

        self.mass.signal_event(EventType.PLAYER_UPDATED, object_id=player_id, data=player)

        if skip_forward:
            return
        # update/signal group player(s) child's when group updates
        if player.type in (PlayerType.GROUP, PlayerType.SYNC_GROUP):
            for child_player in self.iter_group_members(player):
                if child_player.player_id == player.player_id:
                    continue
                self.update(child_player.player_id, skip_forward=True)
        # update/signal group player(s) when child updates
        for group_player in self._get_player_groups(player, powered_only=False):
            player_prov = self.get_player_provider(group_player.player_id)
            if not player_prov:
                continue
            if group_player.player_id.startswith(SYNCGROUP_PREFIX):
                self.update(group_player.player_id, skip_forward=True)
            else:
                self.mass.create_task(player_prov.poll_player(group_player.player_id))

    def get_player_provider(self, player_id: str) -> PlayerProvider:
        """Return PlayerProvider for given player."""
        player = self._players[player_id]
        player_provider = self.mass.get_provider(player.provider)
        return cast(PlayerProvider, player_provider)

    # Player commands

    @api_command("players/cmd/stop")
    @log_player_command
    async def cmd_stop(self, player_id: str) -> None:
        """Send STOP command to given player.

        - player_id: player_id of the player to handle the command.
        """
        player_id = self._check_redirect(player_id)
        if player_provider := self.get_player_provider(player_id):
            await player_provider.cmd_stop(player_id)

    @api_command("players/cmd/play")
    @log_player_command
    async def cmd_play(self, player_id: str) -> None:
        """Send PLAY (unpause) command to given player.

        - player_id: player_id of the player to handle the command.
        """
        player_id = self._check_redirect(player_id)
        player_provider = self.get_player_provider(player_id)
        await player_provider.cmd_play(player_id)

    @api_command("players/cmd/pause")
    @log_player_command
    async def cmd_pause(self, player_id: str) -> None:
        """Send PAUSE command to given player.

        - player_id: player_id of the player to handle the command.
        """
        player_id = self._check_redirect(player_id)
        player = self.get(player_id, True)
        if PlayerFeature.PAUSE not in player.supported_features:
            # if player does not support pause, we need to send stop
            await self.cmd_stop(player_id)
            return
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
    @log_player_command
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
    @log_player_command
    async def cmd_power(self, player_id: str, powered: bool) -> None:
        """Send POWER command to given player.

        - player_id: player_id of the player to handle the command.
        - powered: bool if player should be powered on or off.
        """
        # forward to syncgroup if needed
        if player_id.startswith(SYNCGROUP_PREFIX):
            await self.cmd_group_power(player_id, powered)
            return

        player = self.get(player_id, True)

        if player.powered == powered:
            return  # nothing to do

        # always stop player at power off
        if (
            not powered
            and player.state in (PlayerState.PLAYING, PlayerState.PAUSED)
            and not player.synced_to
            and player.powered
        ):
            await self.cmd_stop(player_id)

        # unsync player at power off
        if not powered:
            if player.synced_to is not None:
                await self.cmd_unsync(player_id)
            for child in self.iter_group_members(player):
                if not child.synced_to:
                    continue
                await self.cmd_unsync(child.player_id)
        if PlayerFeature.POWER in player.supported_features:
            # forward to player provider
            player_provider = self.get_player_provider(player_id)
            await player_provider.cmd_power(player_id, powered)
        else:
            # allow the stop command to process and prevent race conditions
            await asyncio.sleep(0.2)
        # always optimistically set the power state to update the UI
        # as fast as possible and prevent race conditions
        player.powered = powered
        self.update(player_id)
        # handle actions when a syncgroup child turns on
        if active_group_player := self._get_active_player_group(player):
            if active_group_player.player_id.startswith(SYNCGROUP_PREFIX):
                self._on_syncgroup_child_power(
                    active_group_player.player_id, player.player_id, powered
                )
            elif player_prov := self.get_player_provider(active_group_player.player_id):
                player_prov.on_child_power(active_group_player.player_id, player.player_id, powered)
        # handle 'auto play on power on'  feature
        elif (
            powered
            and self.mass.config.get_raw_player_config_value(player_id, CONF_AUTO_PLAY, False)
            and player.active_source in (None, player_id)
        ):
            await self.mass.player_queues.resume(player_id)

    @api_command("players/cmd/volume_set")
    @log_player_command
    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """Send VOLUME_SET command to given player.

        - player_id: player_id of the player to handle the command.
        - volume_level: volume level (0..100) to set on the player.
        """
        # TODO: Implement PlayerControl
        player = self.get(player_id, True)
        if player.type in (PlayerType.GROUP, PlayerType.SYNC_GROUP):
            # redirect to group volume control
            await self.cmd_group_volume(player_id, volume_level)
            return
        if PlayerFeature.VOLUME_SET not in player.supported_features:
            msg = f"Player {player.display_name} does not support volume_set"
            raise UnsupportedFeaturedException(msg)
        player_provider = self.get_player_provider(player_id)
        await player_provider.cmd_volume_set(player_id, volume_level)

    @api_command("players/cmd/volume_up")
    @log_player_command
    async def cmd_volume_up(self, player_id: str) -> None:
        """Send VOLUME_UP command to given player.

        - player_id: player_id of the player to handle the command.
        """
        new_volume = min(100, self._players[player_id].volume_level + 5)
        await self.cmd_volume_set(player_id, new_volume)

    @api_command("players/cmd/volume_down")
    @log_player_command
    async def cmd_volume_down(self, player_id: str) -> None:
        """Send VOLUME_DOWN command to given player.

        - player_id: player_id of the player to handle the command.
        """
        new_volume = max(0, self._players[player_id].volume_level - 5)
        await self.cmd_volume_set(player_id, new_volume)

    @api_command("players/cmd/group_volume")
    @log_player_command
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
        coros = []
        for child_player in self.iter_group_members(group_player, True):
            cur_child_volume = child_player.volume_level
            new_child_volume = int(cur_child_volume + volume_dif)
            new_child_volume = max(0, new_child_volume)
            new_child_volume = min(100, new_child_volume)
            coros.append(self.cmd_volume_set(child_player.player_id, new_child_volume))
        await asyncio.gather(*coros)

    @api_command("players/cmd/group_power")
    async def cmd_group_power(self, player_id: str, power: bool) -> None:
        """Handle power command for a PlayerGroup."""
        group_player = self.get(player_id, True)

        if group_player.powered == power:
            return  # nothing to do

        # always stop (group/master)player at power off
        if not power and group_player.state in (PlayerState.PLAYING, PlayerState.PAUSED):
            await self.cmd_stop(player_id)

        async with asyncio.TaskGroup() as tg:
            members_powered = False
            for member in self.iter_group_members(group_player, only_powered=True):
                members_powered = True
                if power:
                    # set active source to group player if the group (is going to be) powered
                    member.active_source = group_player.player_id
                elif member.active_source == group_player.player_id:
                    # turn off child player when group turns off
                    tg.create_task(self.cmd_power(member.player_id, False))
                    member.active_source = None
            # edge case: group turned on but no members are powered, power them all!
            if not members_powered and power:
                for member in self.iter_group_members(group_player, only_powered=False):
                    tg.create_task(self.cmd_power(member.player_id, True))
                    member.active_source = group_player.player_id

        if power and group_player.player_id.startswith(SYNCGROUP_PREFIX):
            await self._sync_syncgroup(group_player.player_id)
        self.update(player_id)

    @api_command("players/cmd/volume_mute")
    @log_player_command
    async def cmd_volume_mute(self, player_id: str, muted: bool) -> None:
        """Send VOLUME_MUTE command to given player.

        - player_id: player_id of the player to handle the command.
        - muted: bool if player should be muted.
        """
        player = self.get(player_id, True)
        assert player
        if PlayerFeature.VOLUME_MUTE not in player.supported_features:
            msg = f"Player {player.display_name} does not support muting"
            raise UnsupportedFeaturedException(msg)
        player_provider = self.get_player_provider(player_id)
        await player_provider.cmd_volume_mute(player_id, muted)

    @api_command("players/cmd/seek")
    async def cmd_seek(self, player_id: str, position: int) -> None:
        """Handle SEEK command for given queue.

        - player_id: player_id of the player to handle the command.
        - position: position in seconds to seek to in the current playing item.
        """
        player_id = self._check_redirect(player_id)

        player = self.get(player_id, True)
        if PlayerFeature.SEEK not in player.supported_features:
            msg = f"Player {player.display_name} does not support seeking"
            raise UnsupportedFeaturedException(msg)
        player_prov = self.mass.players.get_player_provider(player_id)
        await player_prov.cmd_seek(player_id, position)

    async def play_media(
        self,
        player_id: str,
        queue_item: QueueItem,
        seek_position: int,
        fade_in: bool,
    ) -> None:
        """Handle PLAY MEDIA on given player.

        This is called by the Queue controller to start playing a queue item on the given player.
        The provider's own implementation should work out how to handle this request.

            - player_id: player_id of the player to handle the command.
            - queue_item: The QueueItem that needs to be played on the player.
            - seek_position: Optional seek to this position.
            - fade_in: Optionally fade in the item at playback start.
        """
        if player_id.startswith(SYNCGROUP_PREFIX):
            # redirect to syncgroup-leader if needed
            await self.cmd_group_power(player_id, True)
            group_player = self.get(player_id, True)
            if sync_leader := self.get_sync_leader(group_player):
                await self.play_media(
                    sync_leader.player_id,
                    queue_item=queue_item,
                    seek_position=seek_position,
                    fade_in=fade_in,
                )
                group_player.state = PlayerState.PLAYING
            return
        player_prov = self.mass.players.get_player_provider(player_id)
        await player_prov.play_media(
            player_id=player_id,
            queue_item=queue_item,
            seek_position=int(seek_position),
            fade_in=fade_in,
        )

    async def enqueue_next_queue_item(self, player_id: str, queue_item: QueueItem) -> None:
        """
        Handle enqueuing of the next queue item on the player.

        Only called if the player supports PlayerFeature.ENQUE_NEXT.
        Called about 1 second after a new track started playing.
        Called about 15 seconds before the end of the current track.

        A PlayerProvider implementation is in itself responsible for handling this
        so that the queue items keep playing until its empty or the player stopped.

        This will NOT be called if the end of the queue is reached (and repeat disabled).
        This will NOT be called if the player is using flow mode to playback the queue.
        """
        if player_id.startswith(SYNCGROUP_PREFIX):
            # redirect to syncgroup-leader if needed
            group_player = self.get(player_id, True)
            if sync_leader := self.get_sync_leader(group_player):
                await self.enqueue_next_queue_item(
                    sync_leader.player_id,
                    queue_item=queue_item,
                )
            return
        player_prov = self.mass.players.get_player_provider(player_id)
        await player_prov.enqueue_next_queue_item(player_id=player_id, queue_item=queue_item)

    @api_command("players/cmd/sync")
    @log_player_command
    async def cmd_sync(self, player_id: str, target_player: str) -> None:
        """Handle SYNC command for given player.

        Join/add the given player(id) to the given (leader) player/sync group.
        If the player is already synced to another player, it will be unsynced there first.
        If the target player itself is already synced to another player, this may fail.
        If the player can not be synced with the given target player, this may fail.

            - player_id: player_id of the player to handle the command.
            - target_player: player_id of the syncgroup leader or group player.
        """
        child_player = self.get(player_id, True)
        parent_player = self.get(target_player, True)
        assert child_player
        assert parent_player
        if PlayerFeature.SYNC not in child_player.supported_features:
            msg = f"Player {child_player.name} does not support (un)sync commands"
            raise UnsupportedFeaturedException(msg)
        if PlayerFeature.SYNC not in parent_player.supported_features:
            msg = f"Player {parent_player.name} does not support (un)sync commands"
            raise UnsupportedFeaturedException(msg)
        if child_player.synced_to:
            if child_player.synced_to == parent_player.player_id:
                # nothing to do: already synced to this parent
                return
            # player already synced, unsync first
            await self.cmd_unsync(child_player.player_id)
        elif child_player.state == PlayerState.PLAYING:
            # stop child player if it is currently playing
            await self.cmd_stop(player_id)
        if player_id not in parent_player.can_sync_with:
            raise RuntimeError(
                f"Player {child_player.display_name} can not "
                f"be synced with {parent_player.display_name}",
            )
        # all checks passed, forward command to the player provider
        player_provider = self.get_player_provider(player_id)
        await player_provider.cmd_sync(player_id, target_player)

    @api_command("players/cmd/unsync")
    @log_player_command
    async def cmd_unsync(self, player_id: str) -> None:
        """Handle UNSYNC command for given player.

        Remove the given player from any syncgroups it currently is synced to.
        If the player is not currently synced to any other player,
        this will silently be ignored.

            - player_id: player_id of the player to handle the command.
        """
        player = self.get(player_id, True)
        if PlayerFeature.SYNC not in player.supported_features:
            msg = f"Player {player.name} does not support syncing"
            raise UnsupportedFeaturedException(msg)
        if not player.synced_to:
            LOGGER.info(
                "Ignoring command to unsync player %s "
                "because it is currently not synced to another player.",
                player.display_name,
            )
            return

        # all checks passed, forward command to the player provider
        player_provider = self.get_player_provider(player_id)
        await player_provider.cmd_unsync(player_id)
        # reset active_source just in case
        player.active_source = None

    @api_command("players/create_group")
    async def create_group(self, provider: str, name: str, members: list[str]) -> Player:
        """Create new Player/Sync Group on given PlayerProvider with name and members.

        - provider: provider domain or instance id to create the new group on.
        - name: Name for the new group to create.
        - members: A list of player_id's that should be part of this group.

        Returns the newly created player on success.
        NOTE: Fails if the given provider does not support creating new groups
        or members are given that can not be handled by the provider.
        """
        # perform basic checks
        if (player_prov := self.mass.get_provider(provider)) is None:
            msg = f"Provider {provider} is not available!"
            raise ProviderUnavailableError(msg)
        if ProviderFeature.PLAYER_GROUP_CREATE in player_prov.supported_features:
            # provider supports group create feature: forward request to provider
            # the provider is itself responsible for
            # checking if the members can be used for grouping
            return await player_prov.create_group(name, members=members)
        if ProviderFeature.SYNC_PLAYERS in player_prov.supported_features:
            # default syncgroup implementation
            return await self._create_syncgroup(player_prov.instance_id, name, members)
        msg = f"Provider {player_prov.name} does not support creating groups"
        raise UnsupportedFeaturedException(msg)

    def _check_redirect(self, player_id: str) -> str:
        """Check if playback related command should be redirected."""
        player = self.get(player_id, True)
        if player_id.startswith(SYNCGROUP_PREFIX) and (sync_leader := self.get_sync_leader(player)):
            return sync_leader.player_id
        if player.synced_to:
            sync_leader = self.get(player.synced_to, True)
            LOGGER.warning(
                "Player %s is synced to %s and can not accept "
                "playback related commands itself, "
                "redirected the command to the sync leader.",
                player.name,
                sync_leader.name,
            )
            return player.synced_to
        return player_id

    def _get_player_groups(
        self, player: Player, available_only: bool = True, powered_only: bool = False
    ) -> Iterator[Player]:
        """Return all groupplayers the given player belongs to."""
        for _player in self:
            if _player.player_id == player.player_id:
                continue
            if _player.type not in (PlayerType.GROUP, PlayerType.SYNC_GROUP):
                continue
            if available_only and not _player.available:
                continue
            if powered_only and not _player.powered:
                continue
            if (
                player.player_id in _player.group_childs
                or player.active_source == _player.player_id
            ):
                yield _player

    def _get_active_player_group(self, player: Player) -> Player | None:
        """Return the currently active groupplayer for the given player (if any)."""
        # prefer active source group
        for group_player in self._get_player_groups(player, available_only=True, powered_only=True):
            if player.active_source in (group_player.player_id, group_player.active_source):
                return group_player
        # fallback to just the first powered group
        for group_player in self._get_player_groups(player, available_only=True, powered_only=True):
            return group_player
        return None

    def _get_active_source(self, player: Player) -> str:
        """Return the active_source id for given player."""
        # if player is synced, return group leader's active source
        if player.synced_to and (parent_player := self.get(player.synced_to)):
            return parent_player.active_source
        # fallback to the first active group player
        if player.powered:
            for group_player in self._get_player_groups(
                player, available_only=True, powered_only=True
            ):
                if group_player.state in (PlayerState.PLAYING, PlayerState.PAUSED):
                    return group_player.active_source
        # defaults to the player's own player id if not active source set
        return player.active_source or player.player_id

    def _get_group_volume_level(self, player: Player) -> int:
        """Calculate a group volume from the grouped members."""
        if len(player.group_childs) == 0:
            # player is not a group or syncgroup
            return player.volume_level
        # calculate group volume from all (turned on) players
        group_volume = 0
        active_players = 0
        for child_player in self.iter_group_members(player, True):
            group_volume += child_player.volume_level
            active_players += 1
        if active_players:
            group_volume = group_volume / active_players
        return int(group_volume)

    def iter_group_members(
        self,
        group_player: Player,
        only_powered: bool = False,
        only_playing: bool = False,
    ) -> Iterator[Player]:
        """Get (child) players attached to a grouped player."""
        for child_id in list(group_player.group_childs):
            if child_player := self.get(child_id, False):
                if not child_player.available:
                    continue
                if not (not only_powered or child_player.powered):
                    continue
                if not (
                    not only_playing
                    or child_player.state in (PlayerState.PLAYING, PlayerState.PAUSED)
                ):
                    continue
                yield child_player

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
                # - every 120 seconds if the player if not powered
                # - every 30 seconds if the player is powered
                # - every 10 seconds if the player is playing
                if (
                    (player.powered and count % 30 == 0)
                    or (player_playing and count % 10 == 0)
                    or count % 120 == 0
                ) and (player_prov := self.get_player_provider(player_id)):
                    try:
                        await player_prov.poll_player(player_id)
                    except PlayerUnavailableError:
                        player.available = False
                        player.state = PlayerState.IDLE
                        player.powered = False
                    except Exception as err:  # pylint: disable=broad-except
                        LOGGER.warning(
                            "Error while requesting latest state from player %s: %s",
                            player.display_name,
                            str(err),
                            exc_info=err if self.logger.isEnabledFor(10) else None,
                        )
                    finally:
                        # always update player state
                        self.mass.loop.call_soon(self.update, player_id)
                    if count >= 120:
                        count = 0
            await asyncio.sleep(1)

    # Syncgroup specific functions/helpers

    async def _create_syncgroup(self, provider: str, name: str, members: list[str]) -> Player:
        """Create new (providers-specific) SyncGroup with given name and members."""
        new_group_id = f"{SYNCGROUP_PREFIX}{shortuuid.random(8).lower()}"
        # cleanup list, filter groups (should be handled by frontend, but just in case)
        members = [
            x.player_id
            for x in self
            if x.player_id in members
            if not x.player_id.startswith(SYNCGROUP_PREFIX)
            if x.provider == provider and PlayerFeature.SYNC in x.supported_features
        ]
        # create default config with the user chosen name
        self.mass.config.create_default_player_config(
            new_group_id,
            provider,
            name=name,
            enabled=True,
            values={CONF_GROUP_MEMBERS: members},
        )
        return self._register_syncgroup(
            group_player_id=new_group_id, provider=provider, name=name, members=members
        )

    def get_sync_leader(self, group_player: Player) -> Player | None:
        """Get the active sync leader player for a syncgroup or synced player."""
        if group_player.synced_to:
            # should not happen but just in case...
            return group_player.synced_to
        for child_player in self.iter_group_members(
            group_player, only_powered=True, only_playing=False
        ):
            if child_player.synced_to and child_player.synced_to in group_player.group_childs:
                return self.get(child_player.synced_to)
            elif child_player.synced_to:
                # player is already synced to a member outside this group ?!
                continue
            elif child_player.group_childs:
                return child_player
        # select new sync leader: return the first playing player
        for child_player in self.iter_group_members(
            group_player, only_powered=True, only_playing=True
        ):
            return child_player
        # fallback select new sync leader: return the first powered player
        for child_player in self.iter_group_members(
            group_player, only_powered=True, only_playing=False
        ):
            return child_player
        return None

    async def _sync_syncgroup(self, player_id: str) -> None:
        """Sync all (possible) players of a syncgroup."""
        group_player = self.get(player_id, True)
        sync_leader = self.get_sync_leader(group_player)
        for member in self.iter_group_members(group_player, only_powered=True):
            if not member.can_sync_with:
                continue
            if not sync_leader:
                # elect the first member as the sync leader if we do not have one
                sync_leader = member
                continue
            if sync_leader.player_id == member.player_id:
                continue
            await self.cmd_sync(member.player_id, sync_leader.player_id)

    async def _register_syncgroups(self) -> None:
        """Register all (virtual/fake) syncgroup players."""
        player_configs = await self.mass.config.get_player_configs(include_values=True)
        for player_config in player_configs:
            if not player_config.player_id.startswith(SYNCGROUP_PREFIX):
                continue
            members = player_config.get_value(CONF_GROUP_MEMBERS)
            self._register_syncgroup(
                group_player_id=player_config.player_id,
                provider=player_config.provider,
                name=player_config.name or player_config.default_name,
                members=members,
            )

    def _register_syncgroup(
        self, group_player_id: str, provider: str, name: str, members: Iterable[str]
    ) -> Player:
        """Register a (virtual/fake) syncgroup player."""
        # extract player features from first/random player
        for member in members:
            if first_player := self.get(member):
                break
        else:
            # edge case: no child player is (yet) available; postpone register
            return None
        player = Player(
            player_id=group_player_id,
            provider=provider,
            type=PlayerType.SYNC_GROUP,
            name=name,
            available=True,
            powered=False,
            device_info=DeviceInfo(model="SyncGroup", manufacturer=provider.title()),
            supported_features=first_player.supported_features,
            group_childs=set(members),
            active_source=group_player_id,
        )
        self.mass.players.register_or_update(player)
        return player

    def _on_syncgroup_child_power(
        self, player_id: str, child_player_id: str, new_power: bool
    ) -> None:
        """
        Call when a power command was executed on one of the child player of a Player/Sync group.

        This is used to handle special actions such as (re)syncing.
        """
        group_player = self.mass.players.get(player_id)
        child_player = self.mass.players.get(child_player_id)

        if not group_player.powered:
            # guard, this should be caught in the player controller but just in case...
            return

        powered_childs = list(self.iter_group_members(group_player, True))
        if not new_power and child_player in powered_childs:
            powered_childs.remove(child_player)
        if new_power and child_player not in powered_childs:
            powered_childs.append(child_player)

        # if the last player of a group turned off, turn off the group
        if len(powered_childs) == 0:
            self.logger.debug(
                "Group %s has no more powered members, turning off group player",
                group_player.display_name,
            )
            self.mass.create_task(self.cmd_power(player_id, False))
            return

        group_playing = group_player.state == PlayerState.PLAYING
        is_sync_leader = (
            len(child_player.group_childs) > 0
            and child_player.active_source == group_player.player_id
        )
        if group_playing and not new_power and is_sync_leader:
            # the current sync leader player turned OFF while the group player
            # should still be playing - we need to select a new sync leader and resume
            self.logger.warning(
                "Syncleader %s turned off while syncgroup is playing, "
                "a forced resume for syngroup %s will be attempted in 5 seconds...",
                child_player.display_name,
                group_player.display_name,
            )

            async def forced_resync() -> None:
                # we need to wait a bit here to not run into massive race conditions
                await asyncio.sleep(5)
                await self._sync_syncgroup(group_player.player_id)
                await self.mass.player_queues.resume(group_player.player_id)

            self.mass.create_task(forced_resync())
            return
        if new_power:
            # if a child player turned ON while the group player is on, we need to resync/resume
            self.mass.create_task(self._sync_syncgroup(group_player.player_id))
