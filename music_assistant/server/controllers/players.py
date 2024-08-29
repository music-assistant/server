"""Logic to play music from MusicProviders to supported players."""

from __future__ import annotations

import asyncio
import functools
import time
from collections.abc import Iterable
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Concatenate, ParamSpec, TypeVar, cast

import shortuuid

from music_assistant.common.helpers.util import get_changed_values
from music_assistant.common.models.config_entries import (
    CONF_ENTRY_ANNOUNCE_VOLUME,
    CONF_ENTRY_ANNOUNCE_VOLUME_MAX,
    CONF_ENTRY_ANNOUNCE_VOLUME_MIN,
    CONF_ENTRY_ANNOUNCE_VOLUME_STRATEGY,
    CONF_ENTRY_PLAYER_ICON,
    CONF_ENTRY_PLAYER_ICON_GROUP,
    PlayerConfig,
)
from music_assistant.common.models.enums import (
    EventType,
    MediaType,
    PlayerFeature,
    PlayerState,
    PlayerType,
    ProviderFeature,
    ProviderType,
)
from music_assistant.common.models.errors import (
    AlreadyRegisteredError,
    PlayerCommandFailed,
    PlayerUnavailableError,
    ProviderUnavailableError,
    UnsupportedFeaturedException,
)
from music_assistant.common.models.media_items import UniqueList
from music_assistant.common.models.player import DeviceInfo, Player, PlayerMedia
from music_assistant.constants import (
    CONF_AUTO_PLAY,
    CONF_GROUP_MEMBERS,
    CONF_HIDE_PLAYER,
    CONF_PLAYERS,
    CONF_PREVENT_SYNC_LEADER_OFF,
    CONF_SYNC_LEADER,
    CONF_SYNCGROUP_DEFAULT_ON,
    CONF_TTS_PRE_ANNOUNCE,
    SYNCGROUP_PREFIX,
)
from music_assistant.server.helpers.api import api_command
from music_assistant.server.helpers.tags import parse_tags
from music_assistant.server.helpers.throttle_retry import Throttler
from music_assistant.server.helpers.util import TaskManager
from music_assistant.server.models.core_controller import CoreController
from music_assistant.server.models.player_provider import PlayerProvider

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine, Iterator

    from music_assistant.common.models.config_entries import CoreConfig


_PlayerControllerT = TypeVar("_PlayerControllerT", bound="PlayerController")
_R = TypeVar("_R")
_P = ParamSpec("_P")


def handle_player_command(
    func: Callable[Concatenate[_PlayerControllerT, _P], Awaitable[_R]],
) -> Callable[Concatenate[_PlayerControllerT, _P], Coroutine[Any, Any, _R | None]]:
    """Check and log commands to players."""

    @functools.wraps(func)
    async def wrapper(self: _PlayerControllerT, *args: _P.args, **kwargs: _P.kwargs) -> _R | None:
        """Log and handle_player_command commands to players."""
        player_id = kwargs["player_id"] if "player_id" in kwargs else args[0]
        if (player := self._players.get(player_id)) is None or not player.available:
            # player not existent
            self.logger.warning(
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
        try:
            await func(self, *args, **kwargs)
        except Exception as err:
            raise PlayerCommandFailed(str(err)) from err

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
        self._player_throttlers: dict[str, Throttler] = {}

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

    # Player commands

    @api_command("players/cmd/stop")
    @handle_player_command
    async def cmd_stop(self, player_id: str, skip_forward: bool = False) -> None:
        """Send STOP command to given player.

        - player_id: player_id of the player to handle the command.
        """
        player_id = self._check_redirect(player_id)
        player = self.get(player_id, True)
        # Redirect to queue controller if active (as it also handles some other logic)
        # Note that skip_forward will be set by the queue controller
        # to prevent an endless loop.
        if not skip_forward and player.active_source == player_id:
            await self.mass.player_queues.stop(player_id)
            return
        # handle syncgroup: redirect to syncgroup-leader if needed
        if player_id.startswith(SYNCGROUP_PREFIX):
            if sync_leader := self.get_sync_leader(player):
                await self.cmd_stop(sync_leader.player_id)
            return
        if player_provider := self.get_player_provider(player_id):
            await player_provider.cmd_stop(player_id)

    @api_command("players/cmd/play")
    @handle_player_command
    async def cmd_play(self, player_id: str, skip_forward: bool = False) -> None:
        """Send PLAY (unpause) command to given player.

        - player_id: player_id of the player to handle the command.
        """
        player_id = self._check_redirect(player_id)
        player = self.get(player_id, True)
        if player.announcement_in_progress:
            self.logger.warning("Ignore queue command: An announcement is in progress")
            return
        # Redirect to queue controller if active (as it also handles some other logic)
        # Note that skip_forward will be set by the queue controller
        # to prevent an endless loop.
        if not skip_forward and player.active_source == player_id:
            await self.mass.player_queues.play(player_id)
            return
        # handle syncgroup: redirect to syncgroup-leader if needed
        if player_id.startswith(SYNCGROUP_PREFIX):
            if sync_leader := self.get_sync_leader(player):
                await self.cmd_play(sync_leader.player_id)
            return
        player_provider = self.get_player_provider(player_id)
        async with self._player_throttlers[player_id]:
            await player_provider.cmd_play(player_id)

    @api_command("players/cmd/pause")
    @handle_player_command
    async def cmd_pause(self, player_id: str) -> None:
        """Send PAUSE command to given player.

        - player_id: player_id of the player to handle the command.
        """
        player_id = self._check_redirect(player_id)
        player = self.get(player_id, True)
        if player.announcement_in_progress:
            self.logger.warning("Ignore command: An announcement is in progress")
            return
        if PlayerFeature.PAUSE not in player.supported_features:
            # if player does not support pause, we need to send stop
            await self.cmd_stop(player_id)
            return
        # handle syncgroup: redirect to syncgroup-leader if needed
        if player_id.startswith(SYNCGROUP_PREFIX):
            if sync_leader := self.get_sync_leader(player):
                await self.cmd_pause(sync_leader.player_id)
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
    @handle_player_command
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

        # grab info about any groups this player is active in
        # to handle actions on the group when a (sync)group child turns on/off
        if active_group_player_id := self._get_active_player_group(player):
            active_group_player = self.get(active_group_player_id)
            group_player_state = active_group_player.state
            if not powered and active_group_player.type == PlayerType.SYNC_GROUP:
                # handle 'prevent sync leader off' feature
                powered_members = list(self.iter_group_members(active_group_player, True))
                sync_leader = self.get_sync_leader(active_group_player)
                if (
                    len(powered_members) > 1
                    and (sync_leader == player)
                    and self.mass.config.get_raw_player_config_value(
                        active_group_player_id, CONF_PREVENT_SYNC_LEADER_OFF, False
                    )
                ):
                    raise PlayerCommandFailed(
                        f"{player.display_name} is the sync "
                        "leader of a syncgroup and cannot be turned off"
                    )
        else:
            active_group_player = None

        # always stop player at power off
        if (
            not powered
            and player.powered
            and player.state in (PlayerState.PLAYING, PlayerState.PAUSED)
            and not player.synced_to
        ):
            await self.cmd_stop(player_id)

        # unsync player at power off
        if not powered:
            if player.synced_to or player.group_childs:
                await self.cmd_unsync(player_id)

        if PlayerFeature.POWER in player.supported_features:
            # player supports power command: forward to player provider
            player_provider = self.get_player_provider(player_id)
            async with self._player_throttlers[player_id]:
                await player_provider.cmd_power(player_id, powered)
        else:
            # allow the stop command to process and prevent race conditions
            await asyncio.sleep(0.2)

        # always optimistically set the power state to update the UI
        # as fast as possible and prevent race conditions
        player.powered = powered
        # reset active source
        player.active_source = None
        self.update(player_id)

        # handle 'auto play on power on'  feature
        if (
            not active_group_player
            and powered
            and self.mass.config.get_raw_player_config_value(player_id, CONF_AUTO_PLAY, False)
            and player.active_source in (None, player_id)
        ):
            await self.mass.player_queues.resume(player_id)

        # handle group player actions
        if not (active_group_player and active_group_player.powered):
            return

        # run actions suitable for every type of group player
        powered_childs = list(self.mass.players.iter_group_members(active_group_player, True))
        if not powered and player in powered_childs:
            powered_childs.remove(player.player_id)
        elif powered and player.player_id not in powered_childs:
            powered_childs.append(player.player_id)
        # if the last player of a group turned off, turn off the group
        if len(powered_childs) == 0:
            self.logger.debug(
                "Group %s has no more powered members, turning off group player",
                active_group_player.display_name,
            )
            self.mass.create_task(self.mass.players.cmd_power(active_group_player.player_id, False))
            return
        # forward to either syncgroup logic or group player logic
        if active_group_player.type == PlayerType.SYNC_GROUP:
            self._on_syncgroup_child_power(active_group_player, player, powered, group_player_state)
        elif active_group_player.type == PlayerType.GROUP:
            player_prov = self.mass.get_provider(active_group_player.provider)
            player_prov.on_group_child_power(
                active_group_player, player, powered, group_player_state
            )

    @api_command("players/cmd/volume_set")
    @handle_player_command
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
        async with self._player_throttlers[player_id]:
            await player_provider.cmd_volume_set(player_id, volume_level)

    @api_command("players/cmd/volume_up")
    @handle_player_command
    async def cmd_volume_up(self, player_id: str) -> None:
        """Send VOLUME_UP command to given player.

        - player_id: player_id of the player to handle the command.
        """
        new_volume = min(100, self._players[player_id].volume_level + 5)
        await self.cmd_volume_set(player_id, new_volume)

    @api_command("players/cmd/volume_down")
    @handle_player_command
    async def cmd_volume_down(self, player_id: str) -> None:
        """Send VOLUME_DOWN command to given player.

        - player_id: player_id of the player to handle the command.
        """
        new_volume = max(0, self._players[player_id].volume_level - 5)
        await self.cmd_volume_set(player_id, new_volume)

    @api_command("players/cmd/group_volume")
    @handle_player_command
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
            if PlayerFeature.VOLUME_SET not in child_player.supported_features:
                continue
            cur_child_volume = child_player.volume_level
            new_child_volume = int(cur_child_volume + volume_dif)
            new_child_volume = max(0, new_child_volume)
            new_child_volume = min(100, new_child_volume)
            coros.append(self.cmd_volume_set(child_player.player_id, new_child_volume))
        await asyncio.gather(*coros)

    @api_command("players/cmd/group_power")
    async def cmd_group_power(self, player_id: str, power: bool) -> None:
        """Handle power command for a (Sync/Player)Group."""
        group_player = self.get(player_id, True)

        if group_player.powered == power:
            return  # nothing to do

        if group_player.type == PlayerType.GROUP:
            # this is a native group player, redirect
            await self.cmd_power(player_id, power)
            return

        if not (group_player.type == PlayerType.SYNC_GROUP or group_player.group_childs):
            # this is not a (temporary) sync group - nothing to do
            raise UnsupportedFeaturedException("Player is not a sync group")

        # make sure to update the group power state
        group_player.powered = power

        # always stop (group/master)player at power off
        if not power and group_player.state in (PlayerState.PLAYING, PlayerState.PAUSED):
            await self.cmd_stop(player_id)

        default_on_pref = self.mass.config.get_raw_player_config_value(
            group_player.player_id, CONF_SYNCGROUP_DEFAULT_ON, "powered_only"
        )

        # handle syncgroup - this will also work for temporary syncgroups
        # where players are manually synced against a group leader
        any_member_powered = False
        async with TaskManager(self.mass) as tg:
            for member in self.iter_group_members(
                group_player, only_powered=(default_on_pref != "always_all")
            ):
                any_member_powered = True
                if power:
                    if member.state in (PlayerState.PLAYING, PlayerState.PAUSED):
                        # stop playing existing content on member if we start the group player
                        tg.create_task(self.cmd_stop(member.player_id))
                    # set active source to group player if the group (is going to be) powered
                    member.active_group = group_player.active_group
                    member.active_source = group_player.active_source
                    self.update(member.player_id, skip_forward=True)
                else:
                    # turn off child player when group turns off
                    tg.create_task(self.cmd_power(member.player_id, False))
                    # reset active source on player
                    member.active_source = None
                    member.active_group = None
                    self.update(member.player_id, skip_forward=True)
            # handle default power ON
            if power:
                sync_leader = self.get_sync_leader(group_player)
                for member in self.iter_group_members(group_player, only_powered=False):
                    if default_on_pref == "always_all" or (
                        sync_leader
                        and default_on_pref == "always_leader"
                        and member.player_id == sync_leader.player_id
                    ):
                        tg.create_task(self.cmd_power(member.player_id, True))
                        member.active_group = group_player.player_id
                        member.active_source = group_player.active_source
                        any_member_powered = True
                if not any_member_powered:
                    return

        if power and group_player.player_id.startswith(SYNCGROUP_PREFIX):
            await self.sync_syncgroup(group_player.player_id)
        self.update(player_id)

    @api_command("players/cmd/volume_mute")
    @handle_player_command
    async def cmd_volume_mute(self, player_id: str, muted: bool) -> None:
        """Send VOLUME_MUTE command to given player.

        - player_id: player_id of the player to handle the command.
        - muted: bool if player should be muted.
        """
        player = self.get(player_id, True)
        assert player
        if PlayerFeature.VOLUME_MUTE not in player.supported_features:
            self.logger.info(
                "Player %s does not support muting, using volume instead", player.display_name
            )
            if muted:
                player._prev_volume_level = player.volume_level
                player.volume_muted = True
                await self.cmd_volume_set(player_id, 0)
            else:
                player.volume_muted = False
                await self.cmd_volume_set(player_id, player._prev_volume_level)
            return
        player_provider = self.get_player_provider(player_id)
        async with self._player_throttlers[player_id]:
            await player_provider.cmd_volume_mute(player_id, muted)

    @api_command("players/cmd/seek")
    async def cmd_seek(self, player_id: str, position: int) -> None:
        """Handle SEEK command for given player (directly).

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

    @api_command("players/cmd/play_announcement")
    async def play_announcement(
        self,
        player_id: str,
        url: str,
        use_pre_announce: bool | None = None,
        volume_level: int | None = None,
    ) -> None:
        """Handle playback of an announcement (url) on given player."""
        player = self.get(player_id, True)
        while player.announcement_in_progress:
            await asyncio.sleep(0.5)
        if not url.startswith("http"):
            raise PlayerCommandFailed("Only URLs are supported for announcements")
        try:
            # mark announcement_in_progress on player
            player.announcement_in_progress = True
            # determine if the player(group) has native announcements support
            native_announce_support = PlayerFeature.PLAY_ANNOUNCEMENT in player.supported_features
            if not native_announce_support and player.synced_to:
                # redirect to sync master if player is group child
                self.logger.warning(
                    "Detected announcement request to a player that is currently synced, "
                    "this will be redirected to the entire syncgroup."
                )
                await self.play_announcement(player.synced_to, url, use_pre_announce, volume_level)
                return
            if not native_announce_support and player.active_group:
                # redirect to group player if playergroup is active
                self.logger.warning(
                    "Detected announcement request to a player which has a group active, "
                    "this will be redirected to the group."
                )
                await self.play_announcement(
                    player.active_group, url, use_pre_announce, volume_level
                )
                return
            if player.type in (PlayerType.SYNC_GROUP, PlayerType.GROUP) and not player.powered:
                # announcement request sent to inactive group, check if any child's are playing
                if len(list(self.iter_group_members(player, True, True))) > 0:
                    # just for the sake of simplicity we handle this request per-player
                    # so we can restore the individual players again.
                    self.logger.warning(
                        "Detected announcement request to an inactive playergroup, "
                        "while one or more individual players are playing. "
                        "This announcement will be redirected to the individual players."
                    )
                    async with TaskManager(self.mass) as tg:
                        for group_member in player.group_childs:
                            tg.create_task(
                                self.play_announcement(
                                    group_member,
                                    url=url,
                                    use_pre_announce=use_pre_announce,
                                    volume_level=volume_level,
                                )
                            )
                    return
            # determine pre-announce from (group)player config
            if use_pre_announce is None and "tts" in url:
                use_pre_announce = await self.mass.config.get_player_config_value(
                    player_id,
                    CONF_TTS_PRE_ANNOUNCE,
                )
            self.logger.info(
                "Playback announcement to player %s (with pre-announce: %s): %s",
                player.display_name,
                use_pre_announce,
                url,
            )
            # create a PlayerMedia object for the announcement so
            # we can send a regular play-media call downstream
            announcement = PlayerMedia(
                uri=self.mass.streams.get_announcement_url(player_id, url, use_pre_announce),
                media_type=MediaType.ANNOUNCEMENT,
                title="Announcement",
                custom_data={"url": url, "use_pre_announce": use_pre_announce},
            )
            # handle native announce support
            if native_announce_support:
                if prov := self.mass.get_provider(player.provider):
                    await prov.play_announcement(player_id, announcement, volume_level)
                    return
            # use fallback/default implementation
            await self._play_announcement(player, announcement, volume_level)
        finally:
            player.announcement_in_progress = False

    async def play_media(self, player_id: str, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA on given player.

        - player_id: player_id of the player to handle the command.
        - media: The Media that needs to be played on the player.
        """
        # handle syncgroup: redirect to syncgroup-leader if needed
        if player_id.startswith(SYNCGROUP_PREFIX):
            await self.cmd_group_power(player_id, True)
            group_player = self.get(player_id, True)
            if sync_leader := self.get_sync_leader(group_player):
                await self.play_media(sync_leader.player_id, media=media)
                group_player.state = PlayerState.PLAYING
            return
        # power on the player if needed
        player = self.get(player_id, True)
        if not player.powered:
            await self.cmd_power(player_id, True)
        player_prov = self.mass.players.get_player_provider(player_id)
        await player_prov.play_media(
            player_id=player_id,
            media=media,
        )

    async def enqueue_next_media(self, player_id: str, media: PlayerMedia) -> None:
        """Handle enqueuing of a next media item on the player."""
        if player_id.startswith(SYNCGROUP_PREFIX):
            # redirect to syncgroup-leader if needed
            group_player = self.get(player_id, True)
            if sync_leader := self.get_sync_leader(group_player):
                await self.enqueue_next_media(
                    sync_leader.player_id,
                    media=media,
                )
            return
        player_prov = self.mass.players.get_player_provider(player_id)
        async with self._player_throttlers[player_id]:
            await player_prov.enqueue_next_media(player_id=player_id, media=media)

    @api_command("players/cmd/sync")
    @handle_player_command
    async def cmd_sync(self, player_id: str, target_player: str) -> None:
        """Handle SYNC command for given player.

        Join/add the given player(id) to the given (leader) player/sync group.
        If the player is already synced to another player, it will be unsynced there first.
        If the target player itself is already synced to another player, this may fail.
        If the player can not be synced with the given target player, this may fail.

            - player_id: player_id of the player to handle the command.
            - target_player: player_id of the syncgroup leader or group player.
        """
        await self.cmd_sync_many(target_player, [player_id])

    @api_command("players/cmd/unsync")
    @handle_player_command
    async def cmd_unsync(self, player_id: str) -> None:
        """Handle UNSYNC command for given player.

        Remove the given player from any syncgroups it currently is synced to.
        If the player is not currently synced to any other player,
        this will silently be ignored.

            - player_id: player_id of the player to handle the command.
        """
        if (player := self.get(player_id)) and player.group_childs:
            # this player is a syncgroup leader, unsync all children
            await self.cmd_unsync_many(player.group_childs)
            return
        await self.cmd_unsync_many([player_id])

    @api_command("players/cmd/sync_many")
    async def cmd_sync_many(self, target_player: str, child_player_ids: list[str]) -> None:
        """Create temporary sync group by joining given players to target player."""
        parent_player: Player = self.get(target_player, True)
        if PlayerFeature.SYNC not in parent_player.supported_features:
            msg = f"Player {parent_player.name} does not support (un)sync commands"
            raise UnsupportedFeaturedException(msg)
        # filter all player ids on compatibility and availability
        final_player_ids: UniqueList[str] = UniqueList()
        for child_player_id in child_player_ids:
            if child_player_id == target_player:
                continue
            if not (child_player := self.get(child_player_id)):
                self.logger.warning("Player %s is not available", child_player_id)
                continue
            if PlayerFeature.SYNC not in child_player.supported_features:
                self.logger.warning(
                    "Player %s does not support (un)sync commands", child_player.name
                )
                continue
            if child_player.synced_to and child_player.synced_to == target_player:
                continue  # already synced to this target
            elif child_player.synced_to:
                # player already synced to another player, unsync first
                self.logger.warning(
                    "Player %s is already synced, unsyncing first", child_player.name
                )
                await self.cmd_unsync(child_player.player_id)

            if child_player_id not in parent_player.can_sync_with:
                self.logger.warning(
                    "Player %s can not be synced with %s",
                    child_player.display_name,
                    parent_player.display_name,
                )
                continue
            # if we reach here, all checks passed
            final_player_ids.append(child_player_id)
            # set active source if player is synced
            child_player.active_source = parent_player.active_source

        # forward command to the player provider after all (base) sanity checks
        player_provider = self.get_player_provider(target_player)
        async with self._player_throttlers[target_player]:
            await player_provider.cmd_sync_many(target_player, child_player_ids)

    @api_command("players/cmd/unsync_many")
    async def cmd_unsync_many(self, player_ids: list[str]) -> None:
        """Handle UNSYNC command for all the given players."""
        # filter all player ids on compatibility and availability
        for player_id in list(player_ids):
            if not (child_player := self.get(player_id)):
                self.logger.warning("Player %s is not available", player_id)
                continue
            if PlayerFeature.SYNC not in child_player.supported_features:
                self.logger.warning(
                    "Player %s does not support (un)sync commands", child_player.name
                )
                continue
            if not child_player.synced_to:
                continue
            # reset active source player if it is unsynced
            child_player.active_source = None
            # forward command to the player provider
            if player_provider := self.get_player_provider(player_id):
                await player_provider.cmd_unsync(player_id)

    def set(self, player: Player) -> None:
        """Set/Update player details on the controller."""
        if player.player_id not in self._players:
            # new player
            self.register(player)
            return
        self._players[player.player_id] = player
        self.update(player.player_id)

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

        # register throttler for this player
        self._player_throttlers[player_id] = Throttler(1, 0.2)

        self._players[player_id] = player

        # ignore disabled players
        if not player.enabled:
            return

        # initialize sync groups as soon as a player is registered
        self.mass.loop.create_task(self._register_syncgroups())

        self.logger.info(
            "Player registered: %s/%s",
            player_id,
            player.name,
        )
        self.mass.signal_event(EventType.PLAYER_ADDED, object_id=player.player_id, data=player)
        # always call update to fix special attributes like display name, group volume etc.
        self.update(player.player_id)

    def register_or_update(self, player: Player) -> None:
        """Register a new player on the controller or update existing one."""
        if self.mass.closing:
            return

        if player.player_id in self._players:
            self._players[player.player_id] = player
            self.update(player.player_id)
            return

        self.register(player)

    def remove(self, player_id: str, cleanup_config: bool = True) -> None:
        """Remove a player from the registry."""
        player = self._players.pop(player_id, None)
        if player is None:
            return
        self.logger.info("Player removed: %s", player.name)
        self.mass.player_queues.on_player_remove(player_id)
        if cleanup_config:
            self.mass.config.remove(f"players/{player_id}")
        self._prev_states.pop(player_id, None)
        self.mass.signal_event(EventType.PLAYER_REMOVED, player_id)

    def update(
        self, player_id: str, skip_forward: bool = False, force_update: bool = False
    ) -> None:
        """Update player state."""
        if self.mass.closing:
            return
        if player_id not in self._players:
            return
        player = self._players[player_id]
        # calculate active group and active source
        player.active_group = self._get_active_player_group(player)
        if player.active_source is None:
            player.active_source = self._get_active_source(player)
        player.volume_level = player.volume_level or 0  # guard for None volume
        # correct group_members if needed
        if player.group_childs == {player.player_id}:
            player.group_childs = set()
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
        player.hidden = self.mass.config.get_raw_player_config_value(
            player.player_id, CONF_HIDE_PLAYER, False
        )
        player.icon = self.mass.config.get_raw_player_config_value(
            player.player_id,
            CONF_ENTRY_PLAYER_ICON.key,
            CONF_ENTRY_PLAYER_ICON_GROUP.default_value
            if player.type in (PlayerType.GROUP, PlayerType.SYNC_GROUP)
            else CONF_ENTRY_PLAYER_ICON.default_value,
        )
        # handle syncgroup - get attributes from sync leader
        if player.player_id.startswith(SYNCGROUP_PREFIX):
            sync_leader = self.get_sync_leader(player)
            if sync_leader and sync_leader.active_source == player.active_source:
                player.state = sync_leader.state
                player.active_source = sync_leader.active_source
                player.current_media = sync_leader.current_media
                player.elapsed_time = sync_leader.elapsed_time
                player.elapsed_time_last_updated = sync_leader.elapsed_time_last_updated
            else:
                player.state = PlayerState.IDLE
                player.active_source = player.player_id

        # basic throttle: do not send state changed events if player did not actually change
        prev_state = self._prev_states.get(player_id, {})
        new_state = self._players[player_id].to_dict()
        changed_values = get_changed_values(
            prev_state,
            new_state,
            ignore_keys=["elapsed_time", "elapsed_time_last_updated", "seq_no", "last_poll"],
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

    def get_announcement_volume(self, player_id: str, volume_override: int | None) -> int | None:
        """Get the (player specific) volume for a announcement."""
        volume_strategy = self.mass.config.get_raw_player_config_value(
            player_id,
            CONF_ENTRY_ANNOUNCE_VOLUME_STRATEGY.key,
            CONF_ENTRY_ANNOUNCE_VOLUME_STRATEGY.default_value,
        )
        volume_strategy_volume = self.mass.config.get_raw_player_config_value(
            player_id,
            CONF_ENTRY_ANNOUNCE_VOLUME.key,
            CONF_ENTRY_ANNOUNCE_VOLUME.default_value,
        )
        volume_level = volume_override
        if volume_level is None and volume_strategy == "absolute":
            volume_level = volume_strategy_volume
        elif volume_level is None and volume_strategy == "relative":
            player = self.get(player_id)
            volume_level = player.volume_level + volume_strategy_volume
        elif volume_level is None and volume_strategy == "percentual":
            player = self.get(player_id)
            percentual = (player.volume_level / 100) * volume_strategy_volume
            volume_level = player.volume_level + percentual
        if volume_level is not None:
            announce_volume_min = self.mass.config.get_raw_player_config_value(
                player_id,
                CONF_ENTRY_ANNOUNCE_VOLUME_MIN.key,
                CONF_ENTRY_ANNOUNCE_VOLUME_MIN.default_value,
            )
            volume_level = max(announce_volume_min, volume_level)
            announce_volume_max = self.mass.config.get_raw_player_config_value(
                player_id,
                CONF_ENTRY_ANNOUNCE_VOLUME_MAX.key,
                CONF_ENTRY_ANNOUNCE_VOLUME_MAX.default_value,
            )
            volume_level = min(announce_volume_max, volume_level)
        # ensure the result is an integer
        return None if volume_level is None else int(volume_level)

    def _check_redirect(self, player_id: str) -> str:
        """Check if playback related command should be redirected."""
        player = self.get(player_id, True)
        if player.synced_to:
            sync_leader = self.get(player.synced_to, True)
            self.logger.warning(
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

    def _get_active_player_group(self, player: Player) -> str | None:
        """Return the currently active groupplayer for the given player (if any)."""
        # prefer active source group
        for group_player in self._get_player_groups(player, available_only=True, powered_only=True):
            if player.active_source in (group_player.player_id, group_player.active_source):
                return group_player.player_id
        # fallback to just the first powered group
        for group_player in self._get_player_groups(player, available_only=True, powered_only=True):
            return group_player.player_id
        return None

    def _get_active_source(self, player: Player) -> str:
        """Return the active_source id for given player."""
        # if player is synced, return group leader's active source
        if player.synced_to and (parent_player := self.get(player.synced_to)):
            return parent_player.active_source
        # fallback to the first active group player
        if player.active_group:
            group_player = self.get(player.active_group)
            return self._get_active_source(group_player)
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
            if PlayerFeature.VOLUME_SET not in child_player.supported_features:
                continue
            group_volume += child_player.volume_level or 0
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
        while True:
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
                if not player.needs_poll:
                    continue
                if (self.mass.loop.time() - player.last_poll) < player.poll_interval:
                    continue
                player.last_poll = self.mass.loop.time()
                if player_prov := self.get_player_provider(player_id):
                    try:
                        await player_prov.poll_player(player_id)
                    except PlayerUnavailableError:
                        player.available = False
                        player.state = PlayerState.IDLE
                        player.powered = False
                    except Exception as err:  # pylint: disable=broad-except
                        self.logger.warning(
                            "Error while requesting latest state from player %s: %s",
                            player.display_name,
                            str(err),
                            exc_info=err if self.logger.isEnabledFor(10) else None,
                        )
                    finally:
                        # always update player state
                        self.mass.loop.call_soon(self.update, player_id)
            await asyncio.sleep(1)

    def on_player_config_changed(self, config: PlayerConfig, changed_keys: set[str]) -> None:
        """Call (by config manager) when the configuration of a player changes."""
        player = self.mass.players.get(config.player_id)
        if config.enabled:
            player_prov = self.mass.players.get_player_provider(config.player_id)
            self.mass.create_task(player_prov.poll_player(config.player_id))
        player.enabled = config.enabled
        self.mass.players.update(config.player_id, force_update=True)
        if config.player_id.startswith(SYNCGROUP_PREFIX):
            # handle syncgroup
            if f"values/{CONF_GROUP_MEMBERS}" in changed_keys:
                player = self.mass.players.get(config.player_id)
                player.group_childs = config.get_value(CONF_GROUP_MEMBERS)
                self.mass.players.update(config.player_id)
        else:
            # signal player provider that the config changed
            with suppress(PlayerUnavailableError):
                if provider := self.mass.get_provider(config.provider):
                    provider.on_player_config_changed(config, changed_keys)
        # if the player was playing, restart playback
        if player and player.state == PlayerState.PLAYING:
            self.mass.create_task(self.mass.player_queues.resume(player.active_source))

    def on_player_config_removed(self, player_id: str) -> None:
        """Call (by config manager) when the configuration of a player is removed."""
        if (player := self.mass.players.get(player_id)) and player.available:
            player.enabled = False
            self.mass.players.update(player_id, force_update=True)
        if player and (provider := self.mass.get_provider(player.provider)):
            assert isinstance(provider, PlayerProvider)
            provider.on_player_config_removed(player_id)
        if not player:
            self.mass.signal_event(EventType.PLAYER_REMOVED, player_id)

    # Syncgroup specific functions/helpers

    @api_command("players/create_syncgroup")
    async def create_syncgroup(self, name: str, members: list[str]) -> Player:
        """Create a new Sync Group with name and members.

        - name: Name for the new group to create.
        - members: A list of player_id's that should be part of this group.

        Returns the newly created player on success.
        """
        base_player = self.get(members[0], True)
        # perform basic checks
        if (player_prov := self.mass.get_provider(base_player.provider)) is None:
            msg = f"Provider {base_player.provider} is not available!"
            raise ProviderUnavailableError(msg)
        if ProviderFeature.SYNC_PLAYERS not in player_prov.supported_features:
            msg = f"Provider {player_prov.name} does not support creating groups"
            raise UnsupportedFeaturedException(msg)
        new_group_id = f"{SYNCGROUP_PREFIX}{shortuuid.random(8).lower()}"
        # cleanup list, just in case the frontend sends some garbage
        members = [
            x
            for x in members
            if (x in base_player.can_sync_with or x == base_player.player_id)
            and not x.startswith(SYNCGROUP_PREFIX)
        ]
        # create default config with the user chosen name
        self.mass.config.create_default_player_config(
            new_group_id,
            player_prov.instance_id,
            name=name,
            enabled=True,
            values={CONF_GROUP_MEMBERS: members},
        )
        return self.register_syncgroup(group_player_id=new_group_id, name=name, members=members)

    def register_syncgroup(self, group_player_id: str, name: str, members: Iterable[str]) -> Player:
        """Register a (virtual/fake) syncgroup player."""
        # extract player features from first/random player
        for member in members:
            if first_player := self.mass.players.get(member):
                break
        else:
            # edge case: no child player is (yet) available; postpone register
            return None
        player_prov = self.mass.get_provider(first_player.provider)
        if TYPE_CHECKING:
            assert player_prov
        player = Player(
            player_id=group_player_id,
            provider=player_prov.instance_id,
            type=PlayerType.SYNC_GROUP,
            name=name,
            available=True,
            powered=False,
            device_info=DeviceInfo(model="SyncGroup", manufacturer=player_prov.name),
            supported_features=first_player.supported_features,
            group_childs=set(members),
            active_source=group_player_id,
        )
        self.mass.players.register_or_update(player)
        return player

    def get_sync_leader(self, group_player: Player) -> Player | None:
        """Get the active sync leader player for a syncgroup or synced player."""
        if group_player.synced_to:
            # should not happen but just in case...
            return group_player.synced_to
        # current sync leader: return the (first/only) player that has group childs
        for child_player in self.iter_group_members(
            group_player, only_powered=False, only_playing=False
        ):
            if child_player.group_childs:
                return child_player
        pref_sync_leader = self.mass.config.get_raw_player_config_value(
            group_player.player_id, CONF_SYNC_LEADER, "auto"
        )
        if pref_sync_leader != "auto" and (player := self.get(pref_sync_leader)):
            return player
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
        # fallback select new sync leader: simply return the first player
        for child_player in self.iter_group_members(
            group_player, only_powered=False, only_playing=False
        ):
            return child_player
        return None

    async def sync_syncgroup(self, player_id: str) -> None:
        """Sync all (possible) players of a syncgroup."""
        group_player = self.get(player_id, True)
        if not (sync_leader := self.get_sync_leader(group_player)):
            raise RuntimeError("No sync leader found for syncgroup")
        for member in self.iter_group_members(group_player, only_powered=True):
            if not member.can_sync_with:
                continue
            if sync_leader.player_id == member.player_id:
                continue
            await self.cmd_sync(member.player_id, sync_leader.player_id)

    def _on_syncgroup_child_power(
        self, group_player: Player, child_player: Player, new_power: bool, group_state: PlayerState
    ) -> None:
        """
        Call when a power command was executed on one of the child players of a SyncGroup.

        This is used to handle special actions such as (re)syncing.
        The group state is sent with the state BEFORE the power command was executed.
        """
        group_playing = group_state == PlayerState.PLAYING
        sync_leader = self.mass.players.get_sync_leader(group_player)
        is_sync_leader = child_player.player_id == sync_leader.player_id
        if group_playing and not new_power and is_sync_leader:
            # the current sync leader player turned OFF while the group player
            # should still be playing - we need to select a new sync leader and resume
            self.logger.warning(
                "Syncleader %s turned off while syncgroup is playing, "
                "a forced resync for syngroup %s will be attempted...",
                child_player.display_name,
                group_player.display_name,
            )

            async def full_resync() -> None:
                await self.mass.players.sync_syncgroup(group_player.player_id)
                await self.mass.player_queues.resume(group_player.player_id)

            self.mass.call_later(2, full_resync, task_id=f"forced_resync_{group_player.player_id}")
            return
        elif new_power:
            # if a child player turned ON while the group is already active, we need to resync
            if sync_leader.player_id != child_player.player_id:
                self.mass.create_task(
                    self.cmd_sync(child_player.player_id, sync_leader.player_id),
                )

    async def _register_syncgroups(self) -> None:
        """Register all (virtual/fake) syncgroup players."""
        player_configs = await self.mass.config.get_player_configs()
        for player_config in player_configs:
            if not player_config.player_id.startswith(SYNCGROUP_PREFIX):
                continue
            members = self.mass.config.get_raw_player_config_value(
                player_config.player_id, CONF_GROUP_MEMBERS
            )
            self.register_syncgroup(
                group_player_id=player_config.player_id,
                name=player_config.name or player_config.default_name,
                members=members,
            )

    async def _play_announcement(
        self,
        player: Player,
        announcement: PlayerMedia,
        volume_level: int | None = None,
    ) -> None:
        """Handle (default/fallback) implementation of the play announcement feature.

        This default implementation will;
        - stop playback of the current media (if needed)
        - power on the player (if needed)
        - raise the volume a bit
        - play the announcement (from given url)
        - wait for the player to finish playing
        - restore the previous power and volume
        - restore playback (if needed and if possible)

        This default implementation will only be used if the player's
        provider has no native support for the PLAY_ANNOUNCEMENT feature.
        """
        prev_power = player.powered
        prev_state = player.state
        queue = self.mass.player_queues.get_active_queue(player.player_id)
        prev_queue_active = queue.active
        prev_item_id = player.current_item_id
        # stop player if its currently playing
        if prev_state in (PlayerState.PLAYING, PlayerState.PAUSED):
            self.logger.debug(
                "Announcement to player %s - stop existing content (%s)...",
                player.display_name,
                prev_item_id,
            )
            await self.cmd_stop(player.player_id)
            # wait for the player to stop
            await self.wait_for_state(player, PlayerState.IDLE, 10, 0.4)
        # adjust volume if needed
        # in case of a (sync) group, we need to do this for all child players
        prev_volumes: dict[str, int] = {}
        async with TaskManager(self.mass) as tg:
            for volume_player_id in player.group_childs or (player.player_id,):
                if not (volume_player := self.get(volume_player_id)):
                    continue
                # filter out players that have a different source active
                if volume_player.active_source not in (
                    player.active_source,
                    volume_player.player_id,
                    None,
                ):
                    continue
                prev_volume = volume_player.volume_level
                announcement_volume = self.get_announcement_volume(volume_player_id, volume_level)
                temp_volume = announcement_volume or player.volume_level
                if temp_volume != prev_volume:
                    prev_volumes[volume_player_id] = prev_volume
                    self.logger.debug(
                        "Announcement to player %s - setting temporary volume (%s)...",
                        volume_player.display_name,
                        announcement_volume,
                    )
                    tg.create_task(
                        self.cmd_volume_set(volume_player.player_id, announcement_volume)
                    )
        # play the announcement
        self.logger.debug(
            "Announcement to player %s - playing the announcement on the player...",
            player.display_name,
        )
        await self.play_media(player_id=player.player_id, media=announcement)
        # wait for the player(s) to play
        await self.wait_for_state(player, PlayerState.PLAYING, 10, minimal_time=0.1)
        # wait for the player to stop playing
        if not announcement.duration:
            media_info = await parse_tags(announcement.custom_data["url"])
            announcement.duration = media_info.duration or 60
        media_info.duration += 2
        await self.wait_for_state(
            player,
            PlayerState.IDLE,
            max(announcement.duration * 2, 60),
            announcement.duration + 2,
        )
        self.logger.debug(
            "Announcement to player %s - restore previous state...", player.display_name
        )
        # restore volume
        async with TaskManager(self.mass) as tg:
            for volume_player_id, prev_volume in prev_volumes.items():
                tg.create_task(self.cmd_volume_set(volume_player_id, prev_volume))

        await asyncio.sleep(0.2)
        player.current_item_id = prev_item_id
        # either power off the player or resume playing
        if not prev_power:
            await self.cmd_power(player.player_id, False)
            return
        elif prev_queue_active and prev_state == PlayerState.PLAYING:
            await self.mass.player_queues.resume(queue.queue_id, True)
        elif prev_state == PlayerState.PLAYING:
            # player was playing something else - try to resume that here
            self.logger.warning("Can not resume %s on %s", prev_item_id, player.display_name)
            # TODO !!

    async def wait_for_state(
        self,
        player: Player,
        wanted_state: PlayerState,
        timeout: float = 60.0,
        minimal_time: float = 0,
    ) -> None:
        """Wait for the given player to reach the given state."""
        start_timestamp = time.time()
        self.logger.debug(
            "Waiting for player %s to reach state %s", player.display_name, wanted_state
        )
        try:
            async with asyncio.timeout(timeout):
                while player.state != wanted_state:
                    await asyncio.sleep(0.1)

        except TimeoutError:
            self.logger.debug(
                "Player %s did not reach state %s within the timeout of %s seconds",
                player.display_name,
                wanted_state,
                timeout,
            )
        elapsed_time = round(time.time() - start_timestamp, 2)
        if elapsed_time < minimal_time:
            self.logger.debug(
                "Player %s reached state %s too soon (%s vs %s seconds) - add fallback sleep...",
                player.display_name,
                wanted_state,
                elapsed_time,
                minimal_time,
            )
            await asyncio.sleep(minimal_time - elapsed_time)
        else:
            self.logger.debug(
                "Player %s reached state %s within %s seconds",
                player.display_name,
                wanted_state,
                elapsed_time,
            )
