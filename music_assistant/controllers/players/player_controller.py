"""
MusicAssistant PlayerController.

Handles all logic to control supported players,
which are provided by Player Providers.

Note that the PlayerController has a concept of a 'player' and a 'playerstate'.
The Player is the actual object that is provided by the provider,
which incorporates the actual state of the player (e.g. volume, state, etc)
and functions for controlling the player (e.g. play, pause, etc).

The playerstate is the (final) state of the player, including any user customizations
and transformations that are applied to the player.
The playerstate is the object that is exposed to the outside world (via the API).
"""

from __future__ import annotations

import asyncio
import functools
import time
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Concatenate, TypedDict, cast, overload

from music_assistant_models.auth import UserRole
from music_assistant_models.constants import (
    PLAYER_CONTROL_FAKE,
    PLAYER_CONTROL_NATIVE,
    PLAYER_CONTROL_NONE,
)
from music_assistant_models.enums import (
    EventType,
    MediaType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
    ProviderFeature,
    ProviderType,
)
from music_assistant_models.errors import (
    AlreadyRegisteredError,
    InsufficientPermissions,
    MusicAssistantError,
    PlayerCommandFailed,
    PlayerUnavailableError,
    ProviderUnavailableError,
    UnsupportedFeaturedException,
)
from music_assistant_models.player_control import PlayerControl  # noqa: TC002

from music_assistant.constants import (
    ANNOUNCE_ALERT_FILE,
    ATTR_ANNOUNCEMENT_IN_PROGRESS,
    ATTR_AVAILABLE,
    ATTR_ELAPSED_TIME,
    ATTR_ENABLED,
    ATTR_FAKE_MUTE,
    ATTR_FAKE_POWER,
    ATTR_FAKE_VOLUME,
    ATTR_GROUP_MEMBERS,
    ATTR_LAST_POLL,
    ATTR_PREVIOUS_VOLUME,
    CONF_AUTO_PLAY,
    CONF_ENTRY_ANNOUNCE_VOLUME,
    CONF_ENTRY_ANNOUNCE_VOLUME_MAX,
    CONF_ENTRY_ANNOUNCE_VOLUME_MIN,
    CONF_ENTRY_ANNOUNCE_VOLUME_STRATEGY,
    CONF_ENTRY_TTS_PRE_ANNOUNCE,
    CONF_PLAYER_DSP,
    CONF_PLAYERS,
    CONF_PRE_ANNOUNCE_CHIME_URL,
    SYNCGROUP_PREFIX,
)
from music_assistant.controllers.webserver.helpers.auth_middleware import get_current_user
from music_assistant.helpers.api import api_command
from music_assistant.helpers.tags import async_parse_tags
from music_assistant.helpers.throttle_retry import Throttler
from music_assistant.helpers.util import TaskManager, validate_announcement_chime_url
from music_assistant.models.core_controller import CoreController
from music_assistant.models.player import Player, PlayerMedia, PlayerState
from music_assistant.models.player_provider import PlayerProvider
from music_assistant.models.plugin import PluginProvider, PluginSource

from .sync_groups import SyncGroupController, SyncGroupPlayer

if TYPE_CHECKING:
    from collections.abc import Iterator

    from music_assistant_models.config_entries import CoreConfig, PlayerConfig
    from music_assistant_models.player_queue import PlayerQueue

    from music_assistant import MusicAssistant

CACHE_CATEGORY_PLAYER_POWER = 1


class AnnounceData(TypedDict):
    """Announcement data."""

    announcement_url: str
    pre_announce: bool
    pre_announce_url: str


@overload
def handle_player_command[PlayerControllerT: "PlayerController", **P, R](
    func: Callable[Concatenate[PlayerControllerT, P], Awaitable[R]],
) -> Callable[Concatenate[PlayerControllerT, P], Coroutine[Any, Any, R | None]]: ...


@overload
def handle_player_command[PlayerControllerT: "PlayerController", **P, R](
    func: None = None,
    *,
    lock: bool = False,
) -> Callable[
    [Callable[Concatenate[PlayerControllerT, P], Awaitable[R]]],
    Callable[Concatenate[PlayerControllerT, P], Coroutine[Any, Any, R | None]],
]: ...


def handle_player_command[PlayerControllerT: "PlayerController", **P, R](
    func: Callable[Concatenate[PlayerControllerT, P], Awaitable[R]] | None = None,
    *,
    lock: bool = False,
) -> (
    Callable[Concatenate[PlayerControllerT, P], Coroutine[Any, Any, R | None]]
    | Callable[
        [Callable[Concatenate[PlayerControllerT, P], Awaitable[R]]],
        Callable[Concatenate[PlayerControllerT, P], Coroutine[Any, Any, R | None]],
    ]
):
    """Check and log commands to players.

    :param func: The function to wrap (when used without parentheses).
    :param lock: If True, acquire a lock per player_id and function name before executing.
    """

    def decorator(
        fn: Callable[Concatenate[PlayerControllerT, P], Awaitable[R]],
    ) -> Callable[Concatenate[PlayerControllerT, P], Coroutine[Any, Any, R | None]]:
        @functools.wraps(fn)
        async def wrapper(self: PlayerControllerT, *args: P.args, **kwargs: P.kwargs) -> None:
            """Log and handle_player_command commands to players."""
            player_id = kwargs.get("player_id") or args[0]
            assert isinstance(player_id, str)  # for type checking
            if (player := self._players.get(player_id)) is None or not player.available:
                # player not existent
                self.logger.warning(
                    "Ignoring command %s for unavailable player %s",
                    fn.__name__,
                    player_id,
                )
                return

            current_user = get_current_user()
            if (
                current_user
                and current_user.player_filter
                and player.player_id not in current_user.player_filter
            ):
                msg = (
                    f"{current_user.username} does not have access to player {player.display_name}"
                )
                raise InsufficientPermissions(msg)

            self.logger.debug(
                "Handling command %s for player %s (%s)",
                fn.__name__,
                player.display_name,
                f"by user {current_user.username}" if current_user else "unauthenticated",
            )

            async def execute() -> None:
                try:
                    await fn(self, *args, **kwargs)
                except Exception as err:
                    raise PlayerCommandFailed(str(err)) from err

            if lock:
                # Acquire a lock specific to player_id and function name
                lock_key = f"{fn.__name__}_{player_id}"
                if lock_key not in self._player_command_locks:
                    self._player_command_locks[lock_key] = asyncio.Lock()
                async with self._player_command_locks[lock_key]:
                    await execute()
            else:
                await execute()

        return wrapper

    # Support both @handle_player_command and @handle_player_command(lock=True)
    if func is not None:
        return decorator(func)
    return decorator


class PlayerController(CoreController):
    """Controller holding all logic to control registered players."""

    domain: str = "players"

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize core controller."""
        super().__init__(mass)
        self._players: dict[str, Player] = {}
        self._controls: dict[str, PlayerControl] = {}
        self.manifest.name = "Player Controller"
        self.manifest.description = (
            "Music Assistant's core controller which manages all players from all providers."
        )
        self.manifest.icon = "speaker-multiple"
        self._poll_task: asyncio.Task[None] | None = None
        self._player_throttlers: dict[str, Throttler] = {}
        self._player_command_locks: dict[str, asyncio.Lock] = {}
        self._sync_groups: SyncGroupController = SyncGroupController(self)

    async def setup(self, config: CoreConfig) -> None:
        """Async initialize of module."""
        self._poll_task = self.mass.create_task(self._poll_players())

    async def close(self) -> None:
        """Cleanup on exit."""
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()

    async def on_provider_loaded(self, provider: PlayerProvider) -> None:
        """Handle logic when a provider is loaded."""
        if ProviderFeature.SYNC_PLAYERS in provider.supported_features:
            await self._sync_groups.on_provider_loaded(provider)

    async def on_provider_unload(self, provider: PlayerProvider) -> None:
        """Handle logic when a provider is (about to get) unloaded."""
        if ProviderFeature.SYNC_PLAYERS in provider.supported_features:
            await self._sync_groups.on_provider_unload(provider)

    @property
    def providers(self) -> list[PlayerProvider]:
        """Return all loaded/running MusicProviders."""
        return cast("list[PlayerProvider]", self.mass.get_providers(ProviderType.PLAYER))

    def all(
        self,
        return_unavailable: bool = True,
        return_disabled: bool = False,
        provider_filter: str | None = None,
        return_sync_groups: bool = True,
    ) -> list[Player]:
        """
        Return all registered players.

        Note that this applies user filters for players (for non admin users).

        :param return_unavailable [bool]: Include unavailable players.
        :param return_disabled [bool]: Include disabled players.
        :param provider_filter [str]: Optional filter by provider lookup key.

        :return: List of Player objects.
        """
        current_user = get_current_user()
        user_filter = (
            current_user.player_filter
            if current_user and current_user.role != UserRole.ADMIN
            else None
        )
        return [
            player
            for player in self._players.values()
            if (player.available or return_unavailable)
            and (player.enabled or return_disabled)
            and (provider_filter is None or player.provider.instance_id == provider_filter)
            and (not user_filter or player.player_id in user_filter)
            and (return_sync_groups or not isinstance(player, SyncGroupPlayer))
        ]

    @api_command("players/all")
    def all_states(
        self,
        return_unavailable: bool = True,
        return_disabled: bool = False,
        provider_filter: str | None = None,
    ) -> list[PlayerState]:
        """
        Return PlayerState for all registered players.

        :param return_unavailable [bool]: Include unavailable players.
        :param return_disabled [bool]: Include disabled players.
        :param provider_filter [str]: Optional filter by provider lookup key.

        :return: List of PlayerState objects.
        """
        return [
            player.state
            for player in self.all(
                return_unavailable=return_unavailable,
                return_disabled=return_disabled,
                provider_filter=provider_filter,
            )
        ]

    def get(
        self,
        player_id: str,
        raise_unavailable: bool = False,
    ) -> Player | None:
        """
        Return Player by player_id.

        :param player_id [str]: ID of the player.
        :param raise_unavailable [bool]: Raise if player is unavailable.

        :raises PlayerUnavailableError: If player is unavailable and raise_unavailable is True.
        :return: Player object or None.
        """
        if player := self._players.get(player_id):
            if (not player.available or not player.enabled) and raise_unavailable:
                msg = f"Player {player_id} is not available"
                raise PlayerUnavailableError(msg)
            return player
        if raise_unavailable:
            msg = f"Player {player_id} is not available"
            raise PlayerUnavailableError(msg)
        return None

    @api_command("players/get")
    def get_state(
        self,
        player_id: str,
        raise_unavailable: bool = False,
    ) -> PlayerState | None:
        """
        Return PlayerState by player_id.

        :param player_id [str]: ID of the player.
        :param raise_unavailable [bool]: Raise if player is unavailable.

        :raises PlayerUnavailableError: If player is unavailable and raise_unavailable is True.
        :return: Player object or None.
        """
        current_user = get_current_user()
        user_filter = (
            current_user.player_filter
            if current_user and current_user.role != UserRole.ADMIN
            else None
        )
        if current_user and user_filter and player_id not in user_filter:
            msg = f"{current_user.username} does not have access to player {player_id}"
            raise InsufficientPermissions(msg)
        if player := self.get(player_id, raise_unavailable):
            return player.state
        return None

    def get_player_by_name(self, name: str) -> Player | None:
        """
        Return Player by name.

        :param name: Name of the player.
        :return: Player object or None.
        """
        return next((x for x in self._players.values() if x.name == name), None)

    @api_command("players/get_by_name")
    def get_player_state_by_name(self, name: str) -> PlayerState | None:
        """
        Return PlayerState by name.

        :param name: Name of the player.
        :return: PlayerState object or None.
        """
        current_user = get_current_user()
        user_filter = (
            current_user.player_filter
            if current_user and current_user.role != UserRole.ADMIN
            else None
        )
        if player := self.get_player_by_name(name):
            if current_user and user_filter and player.player_id not in user_filter:
                msg = f"{current_user.username} does not have access to player {player.player_id}"
                raise InsufficientPermissions(msg)
            return player.state
        return None

    @api_command("players/player_controls")
    def player_controls(
        self,
    ) -> list[PlayerControl]:
        """Return all registered playercontrols."""
        return list(self._controls.values())

    @api_command("players/player_control")
    def get_player_control(
        self,
        control_id: str,
    ) -> PlayerControl | None:
        """
        Return PlayerControl by control_id.

        :param control_id: ID of the player control.
        :return: PlayerControl object or None.
        """
        if control := self._controls.get(control_id):
            return control
        return None

    @api_command("players/plugin_sources")
    def get_plugin_sources(self) -> list[PluginSource]:
        """Return all available plugin sources."""
        return [
            plugin_prov.get_source()
            for plugin_prov in self.mass.get_providers(ProviderType.PLUGIN)
            if isinstance(plugin_prov, PluginProvider)
            and ProviderFeature.AUDIO_SOURCE in plugin_prov.supported_features
        ]

    @api_command("players/plugin_source")
    def get_plugin_source(
        self,
        source_id: str,
    ) -> PluginSource | None:
        """
        Return PluginSource by source_id.

        :param source_id: ID of the plugin source.
        :return: PluginSource object or None.
        """
        for plugin_prov in self.mass.get_providers(ProviderType.PLUGIN):
            assert isinstance(plugin_prov, PluginProvider)  # for type checking
            if ProviderFeature.AUDIO_SOURCE not in plugin_prov.supported_features:
                continue
            if (source := plugin_prov.get_source()) and source.id == source_id:
                return source
        return None

    # Player commands

    @api_command("players/cmd/stop")
    @handle_player_command
    async def cmd_stop(self, player_id: str) -> None:
        """Send STOP command to given player.

        - player_id: player_id of the player to handle the command.
        """
        player = self._get_player_with_redirect(player_id)
        player.mark_stop_called()
        # Redirect to queue controller if it is active
        if active_queue := self.get_active_queue(player):
            await self.mass.player_queues.stop(active_queue.queue_id)
        else:
            # handle command on player directly
            async with self._player_throttlers[player.player_id]:
                await player.stop()

    @api_command("players/cmd/play")
    @handle_player_command(lock=True)
    async def cmd_play(self, player_id: str) -> None:
        """Send PLAY (unpause) command to given player.

        - player_id: player_id of the player to handle the command.
        """
        player = self._get_player_with_redirect(player_id)
        if player.playback_state == PlaybackState.PLAYING:
            self.logger.info(
                "Ignore PLAY request to player %s: player is already playing", player.display_name
            )
            return

        # Check if a plugin source is active with a play callback
        if plugin_source := self._get_active_plugin_source(player):
            if plugin_source.can_play_pause and plugin_source.on_play:
                await plugin_source.on_play()
                return

        if player.playback_state == PlaybackState.PAUSED:
            # handle command on player/source directly
            active_source = next(
                (x for x in player.source_list if x.id == player.active_source), None
            )
            if active_source and not active_source.can_play_pause:
                raise PlayerCommandFailed(
                    "The active source (%s) on player %s does not support play/pause",
                    active_source.name,
                    player.display_name,
                )
            async with self._player_throttlers[player.player_id]:
                await player.play()
        else:
            # try to resume the player
            await self._handle_cmd_resume(player.player_id)

    @api_command("players/cmd/pause")
    @handle_player_command(lock=True)
    async def cmd_pause(self, player_id: str) -> None:
        """Send PAUSE command to given player.

        - player_id: player_id of the player to handle the command.
        """
        player = self._get_player_with_redirect(player_id)

        # Check if a plugin source is active with a pause callback
        if plugin_source := self._get_active_plugin_source(player):
            if plugin_source.can_play_pause and plugin_source.on_pause:
                await plugin_source.on_pause()
                return

        # Redirect to queue controller if it is active
        if active_queue := self.get_active_queue(player):
            await self.mass.player_queues.pause(active_queue.queue_id)
            return

        # handle command on player/source directly
        active_source = next((x for x in player.source_list if x.id == player.active_source), None)
        if active_source and not active_source.can_play_pause:
            raise PlayerCommandFailed(
                "The active source (%s) on player %s does not support play/pause",
                active_source.name,
                player.display_name,
            )
        if PlayerFeature.PAUSE not in player.supported_features:
            # if player does not support pause, we need to send stop
            self.logger.debug(
                "Player %s does not support pause, using STOP instead",
                player.display_name,
            )
            await self.cmd_stop(player.player_id)
            return
        # handle command on player directly
        await player.pause()

    @api_command("players/cmd/play_pause")
    async def cmd_play_pause(self, player_id: str) -> None:
        """Toggle play/pause on given player.

        - player_id: player_id of the player to handle the command.
        """
        player = self._get_player_with_redirect(player_id)
        if player.playback_state == PlaybackState.PLAYING:
            await self.cmd_pause(player.player_id)
        else:
            await self.cmd_play(player.player_id)

    @api_command("players/cmd/resume")
    @handle_player_command
    async def cmd_resume(
        self, player_id: str, source: str | None = None, media: PlayerMedia | None = None
    ) -> None:
        """Send RESUME command to given player.

        Resume (or restart) playback on the player.

        :param player_id: player_id of the player to handle the command.
        :param source: Optional source to resume.
        :param media: Optional media to resume.
        """
        await self._handle_cmd_resume(player_id, source, media)

    @api_command("players/cmd/seek")
    async def cmd_seek(self, player_id: str, position: int) -> None:
        """Handle SEEK command for given player.

        - player_id: player_id of the player to handle the command.
        - position: position in seconds to seek to in the current playing item.
        """
        player = self._get_player_with_redirect(player_id)

        # Check if a plugin source is active with a seek callback
        if plugin_source := self._get_active_plugin_source(player):
            if plugin_source.can_seek and plugin_source.on_seek:
                await plugin_source.on_seek(position)
                return

        # Redirect to queue controller if it is active
        if active_queue := self.get_active_queue(player):
            await self.mass.player_queues.seek(active_queue.queue_id, position)
            return

        # handle command on player/source directly
        active_source = next((x for x in player.source_list if x.id == player.active_source), None)
        if active_source and not active_source.can_seek:
            raise PlayerCommandFailed(
                "The active source (%s) on player %s does not support seeking",
                active_source.name,
                player.display_name,
            )
        if PlayerFeature.SEEK not in player.supported_features:
            msg = f"Player {player.display_name} does not support seeking"
            raise UnsupportedFeaturedException(msg)
        # handle command on player directly
        await player.seek(position)

    @api_command("players/cmd/next")
    async def cmd_next_track(self, player_id: str) -> None:
        """Handle NEXT TRACK command for given player."""
        player = self._get_player_with_redirect(player_id)
        active_source_id = player.active_source or player.player_id

        # Check if a plugin source is active with a next callback
        if plugin_source := self._get_active_plugin_source(player):
            if plugin_source.can_next_previous and plugin_source.on_next:
                await plugin_source.on_next()
                return

        # Redirect to queue controller if it is active
        if active_queue := self.get_active_queue(player):
            await self.mass.player_queues.next(active_queue.queue_id)
            return

        if PlayerFeature.NEXT_PREVIOUS in player.supported_features:
            # player has some other source active and native next/previous support
            active_source = next((x for x in player.source_list if x.id == active_source_id), None)
            if active_source and active_source.can_next_previous:
                await player.next_track()
                return
            msg = "This action is (currently) unavailable for this source."
            raise PlayerCommandFailed(msg)

        msg = f"Player {player.display_name} does not support skipping to the next track."
        raise UnsupportedFeaturedException(msg)

    @api_command("players/cmd/previous")
    async def cmd_previous_track(self, player_id: str) -> None:
        """Handle PREVIOUS TRACK command for given player."""
        player = self._get_player_with_redirect(player_id)
        active_source_id = player.active_source or player.player_id

        # Check if a plugin source is active with a previous callback
        if plugin_source := self._get_active_plugin_source(player):
            if plugin_source.can_next_previous and plugin_source.on_previous:
                await plugin_source.on_previous()
                return

        # Redirect to queue controller if it is active
        if active_queue := self.get_active_queue(player):
            await self.mass.player_queues.previous(active_queue.queue_id)
            return

        if PlayerFeature.NEXT_PREVIOUS in player.supported_features:
            # player has some other source active and native next/previous support
            active_source = next((x for x in player.source_list if x.id == active_source_id), None)
            if active_source and active_source.can_next_previous:
                await player.previous_track()
                return
            msg = "This action is (currently) unavailable for this source."
            raise PlayerCommandFailed(msg)

        msg = f"Player {player.display_name} does not support skipping to the previous track."
        raise UnsupportedFeaturedException(msg)

    @api_command("players/cmd/power")
    @handle_player_command(lock=True)
    async def cmd_power(self, player_id: str, powered: bool) -> None:
        """Send POWER command to given player.

        :param player_id: player_id of the player to handle the command.
        :param powered: bool if player should be powered on or off.
        """
        await self._handle_cmd_power(player_id, powered)

    @api_command("players/cmd/volume_set")
    @handle_player_command(lock=True)
    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """Send VOLUME_SET command to given player.

        :param player_id: player_id of the player to handle the command.
        :param volume_level: volume level (0..100) to set on the player.
        """
        await self._handle_cmd_volume_set(player_id, volume_level)

    @api_command("players/cmd/volume_up")
    @handle_player_command
    async def cmd_volume_up(self, player_id: str) -> None:
        """Send VOLUME_UP command to given player.

        - player_id: player_id of the player to handle the command.
        """
        if not (player := self.get(player_id)):
            return
        current_volume = player.volume_level or 0
        if current_volume < 5 or current_volume > 95:
            step_size = 1
        elif current_volume < 20 or current_volume > 80:
            step_size = 2
        else:
            step_size = 5
        new_volume = min(100, current_volume + step_size)
        await self.cmd_volume_set(player_id, new_volume)

    @api_command("players/cmd/volume_down")
    @handle_player_command
    async def cmd_volume_down(self, player_id: str) -> None:
        """Send VOLUME_DOWN command to given player.

        - player_id: player_id of the player to handle the command.
        """
        if not (player := self.get(player_id)):
            return
        current_volume = player.volume_level or 0
        if current_volume < 5 or current_volume > 95:
            step_size = 1
        elif current_volume < 20 or current_volume > 80:
            step_size = 2
        else:
            step_size = 5
        new_volume = max(0, current_volume - step_size)
        await self.cmd_volume_set(player_id, new_volume)

    @api_command("players/cmd/group_volume")
    @handle_player_command(lock=True)
    async def cmd_group_volume(
        self,
        player_id: str,
        volume_level: int,
    ) -> None:
        """
        Handle adjusting the overall/group volume to a playergroup (or synced players).

        Will set a new (overall) volume level to a group player or syncgroup.

        :param group_player: dedicated group player or syncleader to handle the command.
        :param volume_level: volume level (0..100) to set to the group.
        """
        player = self.get(player_id, True)
        assert player is not None  # for type checker
        if player.type == PlayerType.GROUP or player.group_members:
            # dedicated group player or sync leader
            await self.set_group_volume(player, volume_level)
            return
        if player.synced_to and (sync_leader := self.get(player.synced_to)):
            # redirect to sync leader
            await self.set_group_volume(sync_leader, volume_level)
            return
        # treat as normal player volume change
        await self.cmd_volume_set(player_id, volume_level)

    @api_command("players/cmd/group_volume_up")
    @handle_player_command
    async def cmd_group_volume_up(self, player_id: str) -> None:
        """Send VOLUME_UP command to given playergroup.

        - player_id: player_id of the player to handle the command.
        """
        group_player = self.get(player_id, True)
        assert group_player
        cur_volume = group_player.group_volume
        if cur_volume < 5 or cur_volume > 95:
            step_size = 1
        elif cur_volume < 20 or cur_volume > 80:
            step_size = 2
        else:
            step_size = 5
        new_volume = min(100, cur_volume + step_size)
        await self.cmd_group_volume(player_id, new_volume)

    @api_command("players/cmd/group_volume_down")
    @handle_player_command
    async def cmd_group_volume_down(self, player_id: str) -> None:
        """Send VOLUME_DOWN command to given playergroup.

        - player_id: player_id of the player to handle the command.
        """
        group_player = self.get(player_id, True)
        assert group_player
        cur_volume = group_player.group_volume
        if cur_volume < 5 or cur_volume > 95:
            step_size = 1
        elif cur_volume < 20 or cur_volume > 80:
            step_size = 2
        else:
            step_size = 5
        new_volume = max(0, cur_volume - step_size)
        await self.cmd_group_volume(player_id, new_volume)

    @api_command("players/cmd/volume_mute")
    @handle_player_command
    async def cmd_volume_mute(self, player_id: str, muted: bool) -> None:
        """Send VOLUME_MUTE command to given player.

        - player_id: player_id of the player to handle the command.
        - muted: bool if player should be muted.
        """
        player = self.get(player_id, True)
        assert player
        if player.mute_control == PLAYER_CONTROL_NONE:
            raise UnsupportedFeaturedException(
                f"Player {player.display_name} does not support muting"
            )
        if player.mute_control == PLAYER_CONTROL_NATIVE:
            # player supports mute command natively: forward to player
            async with self._player_throttlers[player_id]:
                await player.volume_mute(muted)
        elif player.mute_control == PLAYER_CONTROL_FAKE:
            # user wants to use fake mute control - so we use volume instead
            self.logger.debug(
                "Using volume for muting for player %s",
                player.display_name,
            )
            if muted:
                player.extra_data[ATTR_PREVIOUS_VOLUME] = player.volume_level
                player.extra_data[ATTR_FAKE_MUTE] = True
                await self._handle_cmd_volume_set(player_id, 0)
                player.update_state()
            else:
                prev_volume = player.extra_data.get(ATTR_PREVIOUS_VOLUME, 1)
                player.extra_data[ATTR_FAKE_MUTE] = False
                player.update_state()
                await self._handle_cmd_volume_set(player_id, prev_volume)
        else:
            # handle external player control
            player_control = self._controls.get(player.mute_control)
            control_name = player_control.name if player_control else player.mute_control
            self.logger.debug("Redirecting mute command to PlayerControl %s", control_name)
            if not player_control or not player_control.supports_mute:
                raise UnsupportedFeaturedException(
                    f"Player control {control_name} is not available"
                )
            async with self._player_throttlers[player_id]:
                assert player_control.mute_set is not None
                await player_control.mute_set(muted)

    @api_command("players/cmd/play_announcement")
    @handle_player_command(lock=True)
    async def play_announcement(
        self,
        player_id: str,
        url: str,
        pre_announce: bool | str | None = None,
        volume_level: int | None = None,
        pre_announce_url: str | None = None,
    ) -> None:
        """
        Handle playback of an announcement (url) on given player.

        - player_id: player_id of the player to handle the command.
        - url: URL of the announcement to play.
        - pre_announce: optional bool if pre-announce should be used.
        - volume_level: optional volume level to set for the announcement.
        - pre_announce_url: optional custom URL to use for the pre-announce chime.
        """
        player = self.get(player_id, True)
        assert player is not None  # for type checking
        if not url.startswith("http"):
            raise PlayerCommandFailed("Only URLs are supported for announcements")
        if (
            pre_announce
            and pre_announce_url
            and not validate_announcement_chime_url(pre_announce_url)
        ):
            raise PlayerCommandFailed("Invalid pre-announce chime URL specified.")
        try:
            # mark announcement_in_progress on player
            player.extra_data[ATTR_ANNOUNCEMENT_IN_PROGRESS] = True
            # determine if the player has native announcements support
            native_announce_support = PlayerFeature.PLAY_ANNOUNCEMENT in player.supported_features
            # determine pre-announce from (group)player config
            if pre_announce is None and "tts" in url:
                conf_pre_announce = self.mass.config.get_raw_player_config_value(
                    player_id,
                    CONF_ENTRY_TTS_PRE_ANNOUNCE.key,
                    CONF_ENTRY_TTS_PRE_ANNOUNCE.default_value,
                )
                pre_announce = cast("bool", conf_pre_announce)
            if pre_announce_url is None:
                if conf_pre_announce_url := self.mass.config.get_raw_player_config_value(
                    player_id,
                    CONF_PRE_ANNOUNCE_CHIME_URL,
                ):
                    # player default custom chime url
                    pre_announce_url = cast("str", conf_pre_announce_url)
                else:
                    # use global default chime url
                    pre_announce_url = ANNOUNCE_ALERT_FILE
            # if player type is group with all members supporting announcements,
            # we forward the request to each individual player
            if player.type == PlayerType.GROUP and (
                all(
                    PlayerFeature.PLAY_ANNOUNCEMENT in x.supported_features
                    for x in self.iter_group_members(player)
                )
            ):
                # forward the request to each individual player
                async with TaskManager(self.mass) as tg:
                    for group_member in player.group_members:
                        tg.create_task(
                            self.play_announcement(
                                group_member,
                                url=url,
                                pre_announce=pre_announce,
                                volume_level=volume_level,
                                pre_announce_url=pre_announce_url,
                            )
                        )
                return
            self.logger.info(
                "Playback announcement to player %s (with pre-announce: %s): %s",
                player.display_name,
                pre_announce,
                url,
            )
            # create a PlayerMedia object for the announcement so
            # we can send a regular play-media call downstream
            announce_data = AnnounceData(
                announcement_url=url,
                pre_announce=bool(pre_announce or False),
                pre_announce_url=pre_announce_url,
            )
            announcement = PlayerMedia(
                uri=self.mass.streams.get_announcement_url(player_id, announce_data=announce_data),
                media_type=MediaType.ANNOUNCEMENT,
                title="Announcement",
                custom_data=dict(announce_data),
            )
            # handle native announce support
            if native_announce_support:
                announcement_volume = self.get_announcement_volume(player_id, volume_level)
                await player.play_announcement(announcement, announcement_volume)
                return
            # use fallback/default implementation
            await self._play_announcement(player, announcement, volume_level)
        finally:
            player.extra_data[ATTR_ANNOUNCEMENT_IN_PROGRESS] = False

    @handle_player_command(lock=True)
    async def play_media(self, player_id: str, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA on given player.

        - player_id: player_id of the player to handle the command.
        - media: The Media that needs to be played on the player.
        """
        player = self._get_player_with_redirect(player_id)
        # power on the player if needed
        if player.powered is False and player.power_control != PLAYER_CONTROL_NONE:
            await self._handle_cmd_power(player.player_id, True)
        if media.source_id:
            player.set_active_mass_source(media.source_id)
        await player.play_media(media)

    @api_command("players/cmd/select_source")
    @handle_player_command(lock=True)
    async def select_source(self, player_id: str, source: str | None) -> None:
        """
        Handle SELECT SOURCE command on given player.

        - player_id: player_id of the player to handle the command.
        - source: The ID of the source that needs to be activated/selected.
        """
        if source is None:
            source = player_id  # default to MA queue source
        player = self.get(player_id, True)
        assert player is not None  # for type checking
        if player.synced_to or player.active_group:
            raise PlayerCommandFailed(f"Player {player.display_name} is currently grouped")
        # check if player is already playing and source is different
        # in that case we need to stop the player first
        prev_source = player.active_source
        if prev_source and source != prev_source:
            with suppress(PlayerCommandFailed, RuntimeError):
                # just try to stop (regardless of state)
                await self.cmd_stop(player_id)
                await asyncio.sleep(2)  # small delay to allow stop to process
        # check if source is a pluginsource
        # in that case the source id is the instance_id of the plugin provider
        if plugin_prov := self.mass.get_provider(source):
            player.set_active_mass_source(source)
            await self._handle_select_plugin_source(player, cast("PluginProvider", plugin_prov))
            return
        # check if source is a mass queue
        # this can be used to restore the queue after a source switch
        if self.mass.player_queues.get(source):
            player.set_active_mass_source(source)
            return
        # basic check if player supports source selection
        if PlayerFeature.SELECT_SOURCE not in player.supported_features:
            raise UnsupportedFeaturedException(
                f"Player {player.display_name} does not support source selection"
            )
        # basic check if source is valid for player
        if not any(x for x in player.source_list if x.id == source):
            raise PlayerCommandFailed(
                f"{source} is an invalid source for player {player.display_name}"
            )
        # forward to player
        await player.select_source(source)

    @handle_player_command(lock=True)
    async def enqueue_next_media(self, player_id: str, media: PlayerMedia) -> None:
        """
        Handle enqueuing of a next media item on the player.

        :param player_id: player_id of the player to handle the command.
        :param media: The Media that needs to be enqueued on the player.
        :raises UnsupportedFeaturedException: if the player does not support enqueueing.
        :raises PlayerUnavailableError: if the player is not available.
        """
        player = self.get(player_id, raise_unavailable=True)
        assert player is not None  # for type checking
        if PlayerFeature.ENQUEUE not in player.supported_features:
            raise UnsupportedFeaturedException(
                f"Player {player.display_name} does not support enqueueing"
            )
        async with self._player_throttlers[player_id]:
            await player.enqueue_next_media(media)

    @api_command("players/cmd/set_members")
    async def cmd_set_members(
        self,
        target_player: str,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """
        Join/unjoin given player(s) to/from target player.

        Will add the given player(s) to the target player (sync leader or group player).

        :param target_player: player_id of the syncgroup leader or group player.
        :param player_ids_to_add: List of player_id's to add to the target player.
        :param player_ids_to_remove: List of player_id's to remove from the target player.

        :raises UnsupportedFeaturedException: if the target player does not support grouping.
        :raises PlayerUnavailableError: if the target player is not available.
        """
        parent_player: Player | None = self.get(target_player, True)
        assert parent_player is not None  # for type checking
        if PlayerFeature.SET_MEMBERS not in parent_player.supported_features:
            msg = f"Player {parent_player.name} does not support group commands"
            raise UnsupportedFeaturedException(msg)

        if parent_player.synced_to:
            # guard edge case: player already synced to another player
            raise PlayerCommandFailed(
                f"Player {parent_player.name} is already synced to another player on its own, "
                "you need to ungroup it first before you can join other players to it.",
            )

        # filter all player ids on compatibility and availability
        final_player_ids_to_add: list[str] = []
        for child_player_id in player_ids_to_add or []:
            if child_player_id == target_player:
                continue
            if child_player_id in final_player_ids_to_add:
                continue
            if not (child_player := self.get(child_player_id)) or not child_player.available:
                self.logger.warning("Player %s is not available", child_player_id)
                continue

            # check if player can be synced/grouped with the target player
            if not (
                child_player_id in parent_player.can_group_with
                or child_player.provider.instance_id in parent_player.can_group_with
                or "*" in parent_player.can_group_with
            ):
                raise UnsupportedFeaturedException(
                    f"Player {child_player.name} can not be grouped with {parent_player.name}"
                )

            if (
                child_player.synced_to
                and child_player.synced_to == target_player
                and child_player_id in parent_player.group_members
            ):
                continue  # already synced to this target

            # Check if player is already part of another group and try to automatically ungroup it
            # first. If that fails, power off the group
            if child_player.active_group and child_player.active_group != target_player:
                if (
                    other_group := self.get(child_player.active_group)
                ) and PlayerFeature.SET_MEMBERS in other_group.supported_features:
                    self.logger.warning(
                        "Player %s is already part of another group (%s), "
                        "removing from that group first",
                        child_player.name,
                        child_player.active_group,
                    )
                    if child_player.player_id in other_group.static_group_members:
                        self.logger.warning(
                            "Player %s is a static member of group %s: removing is not possible, "
                            "powering the group off instead",
                            child_player.name,
                            child_player.active_group,
                        )
                        await self._handle_cmd_power(child_player.active_group, False)
                    else:
                        await other_group.set_members(player_ids_to_remove=[child_player.player_id])
                else:
                    self.logger.warning(
                        "Player %s is already part of another group (%s), powering it off first",
                        child_player.name,
                        child_player.active_group,
                    )
                    await self._handle_cmd_power(child_player.active_group, False)
            elif child_player.synced_to and child_player.synced_to != target_player:
                self.logger.warning(
                    "Player %s is already synced to another player, ungrouping first",
                    child_player.name,
                )
                await self.cmd_ungroup(child_player.player_id)

            # power on the player if needed
            if not child_player.powered and child_player.power_control != PLAYER_CONTROL_NONE:
                await self._handle_cmd_power(child_player.player_id, True)
            # if we reach here, all checks passed
            final_player_ids_to_add.append(child_player_id)

        final_player_ids_to_remove: list[str] = []
        if player_ids_to_remove:
            static_members = set(parent_player.static_group_members)
            for child_player_id in player_ids_to_remove:
                if child_player_id == target_player:
                    raise UnsupportedFeaturedException(
                        f"Cannot remove {parent_player.name} from itself as a member!"
                    )
                if child_player_id not in parent_player.group_members:
                    continue
                if child_player_id in static_members:
                    raise UnsupportedFeaturedException(
                        f"Cannot remove {child_player_id} from {parent_player.name} "
                        "as it is a static member of this group"
                    )
                final_player_ids_to_remove.append(child_player_id)

        # forward command to the player after all (base) sanity checks
        async with self._player_throttlers[target_player]:
            await parent_player.set_members(
                player_ids_to_add=final_player_ids_to_add or None,
                player_ids_to_remove=final_player_ids_to_remove or None,
            )

    @api_command("players/cmd/group")
    @handle_player_command
    async def cmd_group(self, player_id: str, target_player: str) -> None:
        """Handle GROUP command for given player.

        Join/add the given player(id) to the given (leader) player/sync group.
        If the target player itself is already synced to another player, this may fail.
        If the player can not be synced with the given target player, this may fail.

        :param player_id: player_id of the player to handle the command.
        :param target_player: player_id of the syncgroup leader or group player.

        :raises UnsupportedFeaturedException: if the target player does not support grouping.
        :raises PlayerCommandFailed: if the target player is already synced to another player.
        :raises PlayerUnavailableError: if the target player is not available.
        :raises PlayerCommandFailed: if the player is already grouped to another player.
        """
        await self.cmd_set_members(target_player, player_ids_to_add=[player_id])

    @api_command("players/cmd/group_many")
    async def cmd_group_many(self, target_player: str, child_player_ids: list[str]) -> None:
        """
        Join given player(s) to target player.

        Will add the given player(s) to the target player (sync leader or group player).
        NOTE: This is a (deprecated) alias for cmd_set_members.
        """
        await self.cmd_set_members(target_player, player_ids_to_add=child_player_ids)

    @api_command("players/cmd/ungroup")
    @handle_player_command
    async def cmd_ungroup(self, player_id: str) -> None:
        """Handle UNGROUP command for given player.

        Remove the given player from any (sync)groups it currently is synced to.
        If the player is not currently grouped to any other player,
        this will silently be ignored.

        NOTE: This is a (deprecated) alias for cmd_set_members.
        """
        if not (player := self.get(player_id)):
            self.logger.warning("Player %s is not available", player_id)
            return

        if (
            player.active_group
            and (group_player := self.get(player.active_group))
            and (PlayerFeature.SET_MEMBERS in group_player.supported_features)
        ):
            # the player is part of a (permanent) groupplayer and the user tries to ungroup
            if player_id in group_player.static_group_members:
                raise UnsupportedFeaturedException(
                    f"Player {player.name}  is a static member of group {group_player.name} "
                    "and cannot be removed from that group!"
                )
            await group_player.set_members(player_ids_to_remove=[player_id])
            return

        if player.synced_to and (synced_player := self.get(player.synced_to)):
            # player is a sync member
            await synced_player.set_members(player_ids_to_remove=[player_id])
            return

        if not (player.synced_to or player.group_members):
            return  # nothing to do

        if PlayerFeature.SET_MEMBERS not in player.supported_features:
            self.logger.warning("Player %s does not support (un)group commands", player.name)
            return

        # forward command to the player once all checks passed
        await player.ungroup()

    @api_command("players/cmd/ungroup_many")
    async def cmd_ungroup_many(self, player_ids: list[str]) -> None:
        """Handle UNGROUP command for all the given players."""
        for player_id in list(player_ids):
            await self.cmd_ungroup(player_id)

    @api_command("players/create_group_player", required_role="admin")
    async def create_group_player(
        self, provider: str, name: str, members: list[str], dynamic: bool = True
    ) -> Player:
        """
        Create a new (permanent) Group Player.

        :param provider: The provider(id) to create the group player for
        :param name: Name of the new group player
        :param members: List of player ids to add to the group
        :param dynamic: Whether the group is dynamic (members can change)
        """
        if not (provider_instance := self.mass.get_provider(provider)):
            raise ProviderUnavailableError(f"Provider {provider} not found")
        provider_instance = cast("PlayerProvider", provider_instance)
        if ProviderFeature.CREATE_GROUP_PLAYER in provider_instance.supported_features:
            return await provider_instance.create_group_player(name, members, dynamic)
        if ProviderFeature.SYNC_PLAYERS in provider_instance.supported_features:
            # provider supports syncing but not dedicated group players
            # create a sync group instead
            return await self._sync_groups.create_group_player(
                provider_instance, name, members, dynamic=dynamic
            )
        raise UnsupportedFeaturedException(
            f"Provider {provider} does not support creating group players"
        )

    @api_command("players/remove_group_player", required_role="admin")
    async def remove_group_player(self, player_id: str) -> None:
        """
        Remove a group player.

        :param player_id: ID of the group player to remove.
        """
        if not (player := self.get(player_id)):
            # we simply permanently delete the player by wiping its config
            self.mass.config.remove(f"players/{player_id}")
            return
        if player.type != PlayerType.GROUP:
            raise UnsupportedFeaturedException(
                f"Player {player.display_name} is not a group player"
            )
        player.provider.check_feature(ProviderFeature.REMOVE_GROUP_PLAYER)
        await player.provider.remove_group_player(player_id)

    @api_command("players/add_currently_playing_to_favorites")
    async def add_currently_playing_to_favorites(self, player_id: str) -> None:
        """
        Add the currently playing item/track on given player to the favorites.

        This tries to resolve the currently playing media to an actual media item
        and add that to the favorites in the library.

        Will raise an error if the player is not currently playing anything
        or if the currently playing media can not be resolved to a media item.
        """
        player = self._get_player_with_redirect(player_id)
        # handle mass player queue active
        if mass_queue := self.get_active_queue(player):
            if not (current_item := mass_queue.current_item) or not current_item.media_item:
                raise PlayerCommandFailed("No current item to add to favorites")
            # if we're playing a radio station, try to resolve the currently playing track
            if current_item.media_item.media_type == MediaType.RADIO:
                if not (
                    (streamdetails := mass_queue.current_item.streamdetails)
                    and (stream_title := streamdetails.stream_title)
                    and " - " in stream_title
                ):
                    # no stream title available, so we can't resolve the track
                    # this can happen if the radio station does not provide metadata
                    # or there's a commercial break
                    # Possible future improvement could be to actually detect the song with a
                    # shazam-like approach.
                    raise PlayerCommandFailed("No current item to add to favorites")
                # send the streamtitle into a global search query
                search_artist, search_title_title = stream_title.split(" - ", 1)
                # strip off any additional comments in the title (such as from Radio Paradise)
                search_title_title = search_title_title.split(" | ")[0].strip()
                if track := await self.mass.music.get_track_by_name(
                    search_title_title, search_artist
                ):
                    # we found a track, so add it to the favorites
                    await self.mass.music.add_item_to_favorites(track)
                    return
                # we could not resolve the track, so raise an error
                raise PlayerCommandFailed("No current item to add to favorites")

            # else: any other media item, just add it to the favorites directly
            await self.mass.music.add_item_to_favorites(current_item.media_item)
            return

        # guard for player with no active source
        if not player.active_source:
            raise PlayerCommandFailed("Player has no active source")
        # handle other source active using the current_media with uri
        if current_media := player.current_media:
            # prefer the uri of the current media item
            if current_media.uri:
                with suppress(MusicAssistantError):
                    await self.mass.music.add_item_to_favorites(current_media.uri)
                    return
            # fallback to search based on artist and title (and album if available)
            if current_media.artist and current_media.title:
                if track := await self.mass.music.get_track_by_name(
                    current_media.title,
                    current_media.artist,
                    current_media.album,
                ):
                    # we found a track, so add it to the favorites
                    await self.mass.music.add_item_to_favorites(track)
                    return
        # if we reach here, we could not resolve the currently playing item
        raise PlayerCommandFailed("No current item to add to favorites")

    async def register(self, player: Player) -> None:
        """Register a player on the Player Controller."""
        if self.mass.closing:
            return
        player_id = player.player_id

        if player_id in self._players:
            msg = f"Player {player_id} is already registered!"
            raise AlreadyRegisteredError(msg)

        # ignore disabled players
        if not player.enabled:
            return

        # register throttler for this player
        self._player_throttlers[player_id] = Throttler(1, 0.05)

        # restore 'fake' power state from cache if available
        cached_value = await self.mass.cache.get(
            key=player.player_id,
            provider=self.domain,
            category=CACHE_CATEGORY_PLAYER_POWER,
            default=False,
        )
        if cached_value is not None:
            player.extra_data[ATTR_FAKE_POWER] = cached_value

        # finally actually register it
        self._players[player_id] = player

        # ensure we fetch and set the latest/full config for the player
        player_config = await self.mass.config.get_player_config(player_id)
        player.set_config(player_config)
        # call hook after the player is registered and config is set
        await player.on_config_updated()

        self.logger.info(
            "Player registered: %s/%s",
            player_id,
            player.display_name,
        )
        # signal event that a player was added
        # update state without signaling event first (to ensure all attributes are set correctly)
        player.update_state(signal_event=False)
        self.mass.signal_event(EventType.PLAYER_ADDED, object_id=player.player_id, data=player)

        # register playerqueue for this player
        await self.mass.player_queues.on_player_register(player)
        # always call update to fix special attributes like display name, group volume etc.
        player.update_state()

    async def register_or_update(self, player: Player) -> None:
        """Register a new player on the controller or update existing one."""
        if self.mass.closing:
            return

        if player.player_id in self._players:
            self._players[player.player_id] = player
            player.update_state()
            return

        await self.register(player)

    def trigger_player_update(self, player_id: str, force_update: bool = False) -> None:
        """Trigger an update for the given player."""
        if self.mass.closing:
            return
        if not (player := self.get(player_id)):
            return
        self.mass.loop.call_soon(player.update_state, force_update)

    async def unregister(self, player_id: str, permanent: bool = False) -> None:
        """
        Unregister a player from the player controller.

        Called (by a PlayerProvider) when a player is removed
        or no longer available (for a longer period of time).

        This will remove the player from the player controller and
        optionally remove the player's config from the mass config.

        - player_id: player_id of the player to unregister.
        - permanent: if True, remove the player permanently by deleting
        the player's config from the mass config. If False, the player config will not be removed,
        allowing for re-registration (with the same config) later.

        If the player is not registered, this will silently be ignored.
        """
        player = self._players.get(player_id)
        if player is None:
            return
        await self._cleanup_player_memberships(player_id)
        del self._players[player_id]
        self.mass.player_queues.on_player_remove(player_id, permanent=permanent)
        await player.on_unload()
        if permanent:
            # player permanent removal: delete its config
            # and signal PLAYER_REMOVED event
            self.delete_player_config(player_id)
            self.logger.info("Player removed: %s", player.name)
            self.mass.signal_event(EventType.PLAYER_REMOVED, player_id)
        else:
            # temporary unavailable: mark player as unavailable
            # note: the player will be re-registered later if it comes back online
            player.state.available = False
            self.logger.info("Player unavailable: %s", player.name)
            self.mass.signal_event(
                EventType.PLAYER_UPDATED, object_id=player.player_id, data=player.state
            )

    @api_command("players/remove", required_role="admin")
    async def remove(self, player_id: str) -> None:
        """
        Remove a player from a provider.

        Can only be called when a PlayerProvider supports ProviderFeature.REMOVE_PLAYER.
        """
        player = self.get(player_id)
        if player is None:
            # we simply permanently delete the player config since it is not registered
            self.delete_player_config(player_id)
            return
        if player.type == PlayerType.GROUP and player_id.startswith(SYNCGROUP_PREFIX):
            await self._sync_groups.remove_group_player(player_id)
            return
        if player.type == PlayerType.GROUP:
            # Handle group player removal
            player.provider.check_feature(ProviderFeature.REMOVE_GROUP_PLAYER)
            await player.provider.remove_group_player(player_id)
            return
        player.provider.check_feature(ProviderFeature.REMOVE_PLAYER)
        await player.provider.remove_player(player_id)
        # check for group memberships that need to be updated
        if player.active_group and (group_player := self.mass.players.get(player.active_group)):
            # try to remove from the group
            with suppress(UnsupportedFeaturedException, PlayerCommandFailed):
                await group_player.set_members(
                    player_ids_to_remove=[player_id],
                )
        # We removed the player and can now clean up its config
        self.delete_player_config(player_id)

    def delete_player_config(self, player_id: str) -> None:
        """
        Permanently delete a player's configuration.

        Should only be called for players that are not registered by the player controller.
        """
        # we simply permanently delete the player by wiping its config
        conf_key = f"{CONF_PLAYERS}/{player_id}"
        dsp_conf_key = f"{CONF_PLAYER_DSP}/{player_id}"
        for key in (conf_key, dsp_conf_key):
            self.mass.config.remove(key)

    def signal_player_state_update(
        self,
        player: Player,
        changed_values: dict[str, tuple[Any, Any]],
        force_update: bool = False,
        skip_forward: bool = False,
    ) -> None:
        """
        Signal a player state update.

        Called by a Player when its state has changed.
        This will update the player state in the controller and signal the event bus.
        """
        player_id = player.player_id
        if self.mass.closing:
            return

        # ignore updates for disabled players
        if not player.enabled and ATTR_ENABLED not in changed_values:
            return

        if len(changed_values) == 0 and not force_update:
            # nothing changed
            return

        # always signal update to the playerqueue
        self.mass.player_queues.on_player_update(player, changed_values)

        if changed_values.keys() == {ATTR_ELAPSED_TIME} and not force_update:
            # ignore small changes in elapsed time
            prev_value = changed_values[ATTR_ELAPSED_TIME][0] or 0
            new_value = changed_values[ATTR_ELAPSED_TIME][1] or 0
            if abs(prev_value - new_value) < 5:
                return

        # handle DSP reload of the leader when grouping/ungrouping
        if ATTR_GROUP_MEMBERS in changed_values:
            prev_group_members, new_group_members = changed_values[ATTR_GROUP_MEMBERS]
            self._handle_group_dsp_change(player, prev_group_members or [], new_group_members)

        if ATTR_GROUP_MEMBERS in changed_values:
            # Removed group members also need to be updated since they are no longer part
            # of this group and are available for playback again
            prev_group_members = changed_values[ATTR_GROUP_MEMBERS][0] or []
            new_group_members = changed_values[ATTR_GROUP_MEMBERS][1] or []
            removed_members = set(prev_group_members) - set(new_group_members)
            for _removed_player_id in removed_members:
                if removed_player := self.get(_removed_player_id):
                    removed_player.update_state()

        became_inactive = False
        if ATTR_AVAILABLE in changed_values:
            became_inactive = changed_values[ATTR_AVAILABLE][1] is False
        if not became_inactive and ATTR_ENABLED in changed_values:
            became_inactive = changed_values[ATTR_ENABLED][1] is False
        if became_inactive and (player.active_group or player.synced_to):
            self.mass.create_task(self._cleanup_player_memberships(player.player_id))

        # signal player update on the eventbus
        self.mass.signal_event(EventType.PLAYER_UPDATED, object_id=player_id, data=player)

        if skip_forward and not force_update:
            return

        # update/signal group player(s) child's when group updates
        for child_player in self.iter_group_members(player, exclude_self=True):
            child_player.update_state()
        # update/signal group player(s) when child updates
        for group_player in self._get_player_groups(player, powered_only=False):
            group_player.update_state()
        # update/signal manually synced to player when child updates
        if (synced_to := player.synced_to) and (synced_to_player := self.get(synced_to)):
            synced_to_player.update_state()
        # update/signal active groups when a group member updates
        if (active_group := player.active_group) and (
            active_group_player := self.get(active_group)
        ):
            active_group_player.update_state()

    async def register_player_control(self, player_control: PlayerControl) -> None:
        """Register a new PlayerControl on the controller."""
        if self.mass.closing:
            return
        control_id = player_control.id

        if control_id in self._controls:
            msg = f"PlayerControl {control_id} is already registered"
            raise AlreadyRegisteredError(msg)

        # make sure that the playercontrol's provider is set to the instance_id
        prov = self.mass.get_provider(player_control.provider)
        if not prov or prov.instance_id != player_control.provider:
            raise RuntimeError(f"Invalid provider ID given: {player_control.provider}")

        self._controls[control_id] = player_control

        self.logger.info(
            "PlayerControl registered: %s/%s",
            control_id,
            player_control.name,
        )

        # always call update to update any attached players etc.
        self.update_player_control(player_control.id)

    async def register_or_update_player_control(self, player_control: PlayerControl) -> None:
        """Register a new playercontrol on the controller or update existing one."""
        if self.mass.closing:
            return
        if player_control.id in self._controls:
            self._controls[player_control.id] = player_control
            self.update_player_control(player_control.id)
            return
        await self.register_player_control(player_control)

    def update_player_control(self, control_id: str) -> None:
        """Update playercontrol state."""
        if self.mass.closing:
            return
        # update all players that are using this control
        for player in self._players.values():
            if control_id in (player.power_control, player.volume_control, player.mute_control):
                self.mass.loop.call_soon(player.update_state)

    def remove_player_control(self, control_id: str) -> None:
        """Remove a player_control from the player manager."""
        control = self._controls.pop(control_id, None)
        if control is None:
            return
        self._controls.pop(control_id, None)
        self.logger.info("PlayerControl removed: %s", control.name)

    def get_player_provider(self, player_id: str) -> PlayerProvider:
        """Return PlayerProvider for given player."""
        player = self._players[player_id]
        assert player  # for type checker
        return player.provider

    def get_active_queue(self, player: Player) -> PlayerQueue | None:
        """Return the current active queue for a player (if any)."""
        # account for player that is synced (sync child)
        if player.synced_to and player.synced_to != player.player_id:
            if sync_leader := self.get(player.synced_to):
                return self.get_active_queue(sync_leader)
        # handle active group player
        if player.active_group and player.active_group != player.player_id:
            if group_player := self.get(player.active_group):
                return self.get_active_queue(group_player)
        # active_source may be filled queue id (or None)
        active_source = player.active_source or player.player_id
        if active_queue := self.mass.player_queues.get(active_source):
            return active_queue
        return None

    async def set_group_volume(self, group_player: Player, volume_level: int) -> None:
        """Handle adjusting the overall/group volume to a playergroup (or synced players)."""
        cur_volume = group_player.state.group_volume
        volume_dif = volume_level - cur_volume
        coros = []
        # handle group volume by only applying the volume to powered members
        for child_player in self.iter_group_members(
            group_player, only_powered=True, exclude_self=False
        ):
            if child_player.volume_control == PLAYER_CONTROL_NONE:
                continue
            cur_child_volume = child_player.volume_level or 0
            new_child_volume = int(cur_child_volume + volume_dif)
            new_child_volume = max(0, new_child_volume)
            new_child_volume = min(100, new_child_volume)
            # Use private method to skip permission check - already validated on group
            coros.append(self._handle_cmd_volume_set(child_player.player_id, new_child_volume))
        await asyncio.gather(*coros)

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
        if volume_strategy == "none":
            return None
        volume_level = volume_override
        if volume_level is None and volume_strategy == "absolute":
            volume_level = int(cast("float", volume_strategy_volume))
        elif volume_level is None and volume_strategy == "relative":
            if (player := self.get(player_id)) and player.volume_level is not None:
                volume_level = int(player.volume_level + cast("float", volume_strategy_volume))
        elif volume_level is None and volume_strategy == "percentual":
            if (player := self.get(player_id)) and player.volume_level is not None:
                percentual = (player.volume_level / 100) * cast("float", volume_strategy_volume)
                volume_level = int(player.volume_level + percentual)
        if volume_level is not None:
            announce_volume_min = cast(
                "float",
                self.mass.config.get_raw_player_config_value(
                    player_id,
                    CONF_ENTRY_ANNOUNCE_VOLUME_MIN.key,
                    CONF_ENTRY_ANNOUNCE_VOLUME_MIN.default_value,
                ),
            )
            volume_level = max(int(announce_volume_min), volume_level)
            announce_volume_max = cast(
                "float",
                self.mass.config.get_raw_player_config_value(
                    player_id,
                    CONF_ENTRY_ANNOUNCE_VOLUME_MAX.key,
                    CONF_ENTRY_ANNOUNCE_VOLUME_MAX.default_value,
                ),
            )
            volume_level = min(int(announce_volume_max), volume_level)
        return None if volume_level is None else int(volume_level)

    def iter_group_members(
        self,
        group_player: Player,
        only_powered: bool = False,
        only_playing: bool = False,
        active_only: bool = False,
        exclude_self: bool = True,
    ) -> Iterator[Player]:
        """Get (child) players attached to a group player or syncgroup."""
        for child_id in list(group_player.group_members):
            if child_player := self.get(child_id, False):
                if not child_player.available or not child_player.enabled:
                    continue
                if only_powered and child_player.powered is False:
                    continue
                if active_only and child_player.active_group != group_player.player_id:
                    continue
                if exclude_self and child_player.player_id == group_player.player_id:
                    continue
                if only_playing and child_player.playback_state not in (
                    PlaybackState.PLAYING,
                    PlaybackState.PAUSED,
                ):
                    continue
                yield child_player

    async def wait_for_state(
        self,
        player: Player,
        wanted_state: PlaybackState,
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
                while player.playback_state != wanted_state:
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

    async def on_player_config_change(self, config: PlayerConfig, changed_keys: set[str]) -> None:
        """Call (by config manager) when the configuration of a player changes."""
        player_disabled = ATTR_ENABLED in changed_keys and not config.enabled
        # signal player provider that the player got enabled/disabled
        if player_provider := self.mass.get_provider(config.provider):
            assert isinstance(player_provider, PlayerProvider)  # for type checking
            if ATTR_ENABLED in changed_keys and not config.enabled:
                player_provider.on_player_disabled(config.player_id)
            elif ATTR_ENABLED in changed_keys and config.enabled:
                player_provider.on_player_enabled(config.player_id)
        if not (player := self.get(config.player_id)):
            return  # guard against player not being registered (yet)
        resume_queue: PlayerQueue | None = (
            self.mass.player_queues.get(player.active_source) if player.active_source else None
        )
        if player_disabled and player.available:
            # edge case: ensure that the player is powered off if the player gets disabled
            if player.power_control != PLAYER_CONTROL_NONE:
                await self._handle_cmd_power(config.player_id, False)
            elif player.playback_state != PlaybackState.IDLE:
                await self.cmd_stop(config.player_id)
        # ensure player state gets updated with any updated config
        player.set_config(config)
        await player.on_config_updated()
        player.update_state()
        # if the PlayerQueue was playing, restart playback
        # TODO: add restart_stream property to ConfigEntry and use that instead of immediate_apply
        # to check if we need to restart playback
        if not player_disabled and resume_queue and resume_queue.state == PlaybackState.PLAYING:
            config_entries = await player.get_config_entries()
            has_value_changes = False
            all_immediate_apply = True
            for key in changed_keys:
                if not key.startswith("values/"):
                    continue  # skip root values like "enabled", "name"
                has_value_changes = True
                actual_key = key.removeprefix("values/")
                entry = next((e for e in config_entries if e.key == actual_key), None)
                if entry is None or not entry.immediate_apply:
                    all_immediate_apply = False
                    break

            if has_value_changes and all_immediate_apply:
                # All changed config entries have immediate_apply=True, so no need to restart
                # the playback
                return
            # always stop first to ensure the player uses the new config
            await self.mass.player_queues.stop(resume_queue.queue_id)
            self.mass.call_later(1, self.mass.player_queues.resume, resume_queue.queue_id, False)

    async def on_player_dsp_change(self, player_id: str) -> None:
        """Call (by config manager) when the DSP settings of a player change."""
        # signal player provider that the config changed
        if not (player := self.get(player_id)):
            return
        if player.playback_state == PlaybackState.PLAYING:
            self.logger.info("Restarting playback of Player %s after DSP change", player_id)
            # this will restart the queue stream/playback
            if player.mass_queue_active:
                self.mass.call_later(0, self.mass.player_queues.resume, player.active_source, False)
                return
            # if the player is not using a queue, we need to stop and start playback
            await self.cmd_stop(player_id)
            await self.cmd_play(player_id)

    async def _cleanup_player_memberships(self, player_id: str) -> None:
        """Ensure a player is detached from any groups or syncgroups."""
        if not (player := self.get(player_id)):
            return

        if (
            player.active_group
            and (group := self.get(player.active_group))
            and group.supports_feature(PlayerFeature.SET_MEMBERS)
        ):
            # Ungroup the player if its part of an active group, this will ignore
            # static_group_members since that is only checked when using cmd_set_members
            with suppress(UnsupportedFeaturedException, PlayerCommandFailed):
                await group.set_members(player_ids_to_remove=[player_id])
        elif player.synced_to and player.supports_feature(PlayerFeature.SET_MEMBERS):
            # Remove the player if it was synced, otherwise it will still show as
            # synced to the other player after it gets registered again
            with suppress(UnsupportedFeaturedException, PlayerCommandFailed):
                await player.ungroup()

    def _get_player_with_redirect(self, player_id: str) -> Player:
        """Get player with check if playback related command should be redirected."""
        player = self.get(player_id, True)
        assert player is not None  # for type checking
        if player.synced_to and (sync_leader := self.get(player.synced_to)):
            self.logger.info(
                "Player %s is synced to %s and can not accept "
                "playback related commands itself, "
                "redirected the command to the sync leader.",
                player.name,
                sync_leader.name,
            )
            return sync_leader
        if player.active_group and (active_group := self.get(player.active_group)):
            self.logger.info(
                "Player %s is part of a playergroup and can not accept "
                "playback related commands itself, "
                "redirected the command to the group leader.",
                player.name,
            )
            return active_group
        return player

    def _get_active_plugin_source(self, player: Player) -> PluginSource | None:
        """Get the active PluginSource for a player if any."""
        # Check if any plugin source is in use by this player
        for plugin_source in self.get_plugin_sources():
            if plugin_source.in_use_by == player.player_id:
                return plugin_source
            if player.active_source == plugin_source.id:
                return plugin_source
        return None

    def _get_player_groups(
        self, player: Player, available_only: bool = True, powered_only: bool = False
    ) -> Iterator[Player]:
        """Return all groupplayers the given player belongs to."""
        for _player in self.all(return_unavailable=not available_only):
            if _player.player_id == player.player_id:
                continue
            if _player.type != PlayerType.GROUP:
                continue
            if powered_only and _player.powered is False:
                continue
            if player.player_id in _player.group_members:
                yield _player

    async def _play_announcement(  # noqa: PLR0915
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

        This default implementation will only be used if the player
        (provider) has no native support for the PLAY_ANNOUNCEMENT feature.
        """
        prev_state = player.playback_state
        prev_power = player.powered or prev_state != PlaybackState.IDLE
        prev_synced_to = player.synced_to
        prev_group = self.get(player.active_group) if player.active_group else None
        prev_source = player.active_source
        prev_media = player.current_media
        prev_media_name = prev_media.title or prev_media.uri if prev_media else None
        if prev_synced_to:
            # ungroup player if its currently synced
            self.logger.debug(
                "Announcement to player %s - ungrouping player from %s...",
                player.display_name,
                prev_synced_to,
            )
            await self.cmd_ungroup(player.player_id)
        elif prev_group:
            # if the player is part of a group player, we need to ungroup it
            if PlayerFeature.SET_MEMBERS in prev_group.supported_features:
                self.logger.debug(
                    "Announcement to player %s - ungrouping from group player %s...",
                    player.display_name,
                    prev_group.display_name,
                )
                await prev_group.set_members(player_ids_to_remove=[player.player_id])
            else:
                # if the player is part of a group player that does not support ungrouping,
                # we need to power off the groupplayer instead
                self.logger.debug(
                    "Announcement to player %s - turning off group player %s...",
                    player.display_name,
                    prev_group.display_name,
                )
                await self._handle_cmd_power(player.player_id, False)
        elif prev_state in (PlaybackState.PLAYING, PlaybackState.PAUSED):
            # normal/standalone player: stop player if its currently playing
            self.logger.debug(
                "Announcement to player %s - stop existing content (%s)...",
                player.display_name,
                prev_media_name,
            )
            await self.cmd_stop(player.player_id)
            # wait for the player to stop
            await self.wait_for_state(player, PlaybackState.IDLE, 10, 0.4)
        # adjust volume if needed
        # in case of a (sync) group, we need to do this for all child players
        prev_volumes: dict[str, int] = {}
        async with TaskManager(self.mass) as tg:
            for volume_player_id in player.group_members or (player.player_id,):
                if not (volume_player := self.get(volume_player_id)):
                    continue
                # catch any players that have a different source active
                if (
                    volume_player.active_source
                    not in (
                        player.active_source,
                        volume_player.player_id,
                        None,
                    )
                    and volume_player.playback_state == PlaybackState.PLAYING
                ):
                    self.logger.warning(
                        "Detected announcement to playergroup %s while group member %s is playing "
                        "other content, this may lead to unexpected behavior.",
                        player.display_name,
                        volume_player.display_name,
                    )
                    tg.create_task(self.cmd_stop(volume_player.player_id))
                if volume_player.volume_control == PLAYER_CONTROL_NONE:
                    continue
                if (prev_volume := volume_player.volume_level) is None:
                    continue
                announcement_volume = self.get_announcement_volume(volume_player_id, volume_level)
                if announcement_volume is None:
                    continue
                temp_volume = announcement_volume or player.volume_level
                if temp_volume != prev_volume:
                    prev_volumes[volume_player_id] = prev_volume
                    self.logger.debug(
                        "Announcement to player %s - setting temporary volume (%s)...",
                        volume_player.display_name,
                        announcement_volume,
                    )
                    tg.create_task(
                        self._handle_cmd_volume_set(volume_player.player_id, announcement_volume)
                    )
        # play the announcement
        self.logger.debug(
            "Announcement to player %s - playing the announcement on the player...",
            player.display_name,
        )
        await self.play_media(player_id=player.player_id, media=announcement)
        # wait for the player(s) to play
        await self.wait_for_state(player, PlaybackState.PLAYING, 10, minimal_time=0.1)
        # wait for the player to stop playing
        if not announcement.duration:
            if not announcement.custom_data:
                raise ValueError("Announcement missing duration and custom_data")
            media_info = await async_parse_tags(
                announcement.custom_data["announcement_url"], require_duration=True
            )
            announcement.duration = int(media_info.duration) if media_info.duration else None

        if announcement.duration is None:
            raise ValueError("Announcement duration could not be determined")

        await self.wait_for_state(
            player,
            PlaybackState.IDLE,
            timeout=announcement.duration + 10,
            minimal_time=float(announcement.duration) + 2,
        )
        self.logger.debug(
            "Announcement to player %s - restore previous state...", player.display_name
        )
        # restore volume
        async with TaskManager(self.mass) as tg:
            for volume_player_id, prev_volume in prev_volumes.items():
                tg.create_task(self._handle_cmd_volume_set(volume_player_id, prev_volume))
        await asyncio.sleep(0.2)
        # either power off the player or resume playing
        if not prev_power:
            if player.power_control != PLAYER_CONTROL_NONE:
                self.logger.debug(
                    "Announcement to player %s - turning player off again...", player.display_name
                )
                await self._handle_cmd_power(player.player_id, False)
            # nothing to do anymore, player was not previously powered
            # and does not support power control
            return
        elif prev_synced_to:
            self.logger.debug(
                "Announcement to player %s - syncing back to %s...",
                player.display_name,
                prev_synced_to,
            )
            await self.cmd_set_members(prev_synced_to, player_ids_to_add=[player.player_id])
        elif prev_group:
            if PlayerFeature.SET_MEMBERS in prev_group.supported_features:
                self.logger.debug(
                    "Announcement to player %s - grouping back to group player %s...",
                    player.display_name,
                    prev_group.display_name,
                )
                await prev_group.set_members(player_ids_to_add=[player.player_id])
            elif prev_state == PlaybackState.PLAYING:
                # if the player is part of a group player that does not support set_members,
                # we need to restart the groupplayer
                self.logger.debug(
                    "Announcement to player %s - restarting playback on group player %s...",
                    player.display_name,
                    prev_group.display_name,
                )
                await self.cmd_play(prev_group.player_id)
        elif prev_state == PlaybackState.PLAYING:
            # player was playing something before the announcement - try to resume that here
            await self._handle_cmd_resume(player.player_id, prev_source, prev_media)

    async def _poll_players(self) -> None:
        """Background task that polls players for updates."""
        while True:
            for player in list(self._players.values()):
                # if the player is playing, update elapsed time every tick
                # to ensure the queue has accurate details
                player_playing = player.playback_state == PlaybackState.PLAYING
                if player_playing:
                    self.mass.loop.call_soon(
                        self.mass.player_queues.on_player_update,
                        player,
                        {"corrected_elapsed_time": (None, player.corrected_elapsed_time)},
                    )
                # Poll player;
                if not player.needs_poll:
                    continue
                try:
                    last_poll: float = player.extra_data[ATTR_LAST_POLL]
                except KeyError:
                    last_poll = 0.0
                if (self.mass.loop.time() - last_poll) < player.poll_interval:
                    continue
                player.extra_data[ATTR_LAST_POLL] = self.mass.loop.time()
                try:
                    await player.poll()
                except Exception as err:
                    self.logger.warning(
                        "Error while requesting latest state from player %s: %s",
                        player.display_name,
                        str(err),
                        exc_info=err if self.logger.isEnabledFor(10) else None,
                    )
                # Yield to event loop to prevent blocking
                await asyncio.sleep(0)
            await asyncio.sleep(1)

    async def _handle_select_plugin_source(
        self, player: Player, plugin_prov: PluginProvider
    ) -> None:
        """Handle playback/select of given plugin source on player."""
        plugin_source = plugin_prov.get_source()
        if plugin_source.in_use_by and plugin_source.in_use_by != player.player_id:
            self.logger.debug(
                "Plugin source %s is already in use by player %s, stopping playback there first.",
                plugin_source.name,
                plugin_source.in_use_by,
            )
            with suppress(PlayerCommandFailed):
                await self.cmd_stop(plugin_source.in_use_by)
        stream_url = await self.mass.streams.get_plugin_source_url(plugin_source, player.player_id)
        plugin_source.in_use_by = player.player_id
        # Call on_select callback if available
        if plugin_source.on_select:
            await plugin_source.on_select()
        await self.play_media(
            player_id=player.player_id,
            media=PlayerMedia(
                uri=stream_url,
                media_type=MediaType.PLUGIN_SOURCE,
                title=plugin_source.name,
                custom_data={
                    "provider": plugin_prov.instance_id,
                    "source_id": plugin_source.id,
                    "player_id": player.player_id,
                    "audio_format": plugin_source.audio_format,
                },
            ),
        )
        # trigger player update to ensure the source is set
        self.trigger_player_update(player.player_id)

    def _handle_group_dsp_change(
        self, player: Player, prev_group_members: list[str], new_group_members: list[str]
    ) -> None:
        """Handle DSP reload when group membership changes."""
        prev_child_count = len(prev_group_members)
        new_child_count = len(new_group_members)
        is_player_group = player.type == PlayerType.GROUP

        # handle special case for PlayerGroups: since there are no leaders,
        # DSP still always work with a single player in the group.
        multi_device_dsp_threshold = 1 if is_player_group else 0

        prev_is_multiple_devices = prev_child_count > multi_device_dsp_threshold
        new_is_multiple_devices = new_child_count > multi_device_dsp_threshold

        if prev_is_multiple_devices == new_is_multiple_devices:
            return  # no change in multi-device status

        supports_multi_device_dsp = PlayerFeature.MULTI_DEVICE_DSP in player.supported_features

        dsp_enabled: bool
        if player.type == PlayerType.GROUP:
            # Since player groups do not have leaders, we will use the only child
            # that was in the group before and after the change
            if prev_is_multiple_devices:
                if childs := new_group_members:
                    # We shrank the group from multiple players to a single player
                    # So the now only child will control the DSP
                    dsp_enabled = self.mass.config.get_player_dsp_config(childs[0]).enabled
                else:
                    dsp_enabled = False
            elif childs := prev_group_members:
                # We grew the group from a single player to multiple players,
                # let's see if the previous single player had DSP enabled
                dsp_enabled = self.mass.config.get_player_dsp_config(childs[0]).enabled
            else:
                dsp_enabled = False
        else:
            dsp_enabled = self.mass.config.get_player_dsp_config(player.player_id).enabled

        if dsp_enabled and not supports_multi_device_dsp:
            # We now know that the group configuration has changed so:
            # - multi-device DSP is not supported
            # - we switched from a group with multiple players to a single player
            #   (or vice versa)
            # - the leader has DSP enabled
            self.mass.create_task(self.mass.players.on_player_dsp_change(player.player_id))

    # Private command handlers (no permission checks)

    async def _handle_cmd_resume(
        self, player_id: str, source: str | None = None, media: PlayerMedia | None = None
    ) -> None:
        """
        Handle resume playback command.

        Skips the permission checks (internal use only).
        """
        player = self._get_player_with_redirect(player_id)
        source = source or player.active_source
        media = media or player.current_media
        # power on the player if needed
        if not player.powered and player.power_control != PLAYER_CONTROL_NONE:
            await self._handle_cmd_power(player.player_id, True)
        # Redirect to queue controller if it is active
        if active_queue := self.mass.player_queues.get(source or player_id):
            await self.mass.player_queues.resume(active_queue.queue_id)
            return
        # try to handle command on player directly
        # TODO: check if player has an active source with native resume support
        active_source = next((x for x in player.source_list if x.id == source), None)
        if (
            player.playback_state in (PlaybackState.IDLE, PlaybackState.PAUSED)
            and active_source
            and active_source.can_play_pause
        ):
            # player has some other source active and native resume support
            await player.play()
            return
        if active_source and not active_source.passive:
            await self.select_source(player_id, active_source.id)
            return
        if media:
            # try to re-play the current media item
            await player.play_media(media)
            return
        # fallback: just send play command - which will fail if nothing can be played
        await player.play()

    async def _handle_cmd_power(self, player_id: str, powered: bool) -> None:
        """
        Handle player power on/off command.

        Skips the permission checks (internal use only).
        """
        player = self.get(player_id, True)
        assert player is not None  # for type checking
        player_state = player.state

        if player_state.powered == powered:
            self.logger.debug(
                "Ignoring power %s command for player %s: already in state %s",
                "ON" if powered else "OFF",
                player_state.name,
                "ON" if player_state.powered else "OFF",
            )
            return  # nothing to do

        # ungroup player at power off
        player_was_synced = player.synced_to is not None
        if player.type == PlayerType.PLAYER and not powered:
            # ungroup player if it is synced (or is a sync leader itself)
            # NOTE: ungroup will be ignored if the player is not grouped or synced
            await self.cmd_ungroup(player_id)

        # always stop player at power off
        if (
            not powered
            and not player_was_synced
            and player.playback_state in (PlaybackState.PLAYING, PlaybackState.PAUSED)
        ):
            await self.cmd_stop(player_id)
            # short sleep: allow the stop command to process and prevent race conditions
            await asyncio.sleep(0.2)

        # power off all synced childs when player is a sync leader
        elif not powered and player.type == PlayerType.PLAYER and player.group_members:
            async with TaskManager(self.mass) as tg:
                for member in self.iter_group_members(player, True):
                    if member.power_control == PLAYER_CONTROL_NONE:
                        continue
                    # Use private method to skip permission check for child players
                    tg.create_task(self._handle_cmd_power(member.player_id, False))

        # handle actual power command
        if player.power_control == PLAYER_CONTROL_NONE:
            raise UnsupportedFeaturedException(
                f"Player {player.display_name} does not support power control"
            )
        if player.power_control == PLAYER_CONTROL_NATIVE:
            # player supports power command natively: forward to player provider
            async with self._player_throttlers[player_id]:
                await player.power(powered)
        elif player.power_control == PLAYER_CONTROL_FAKE:
            # user wants to use fake power control - so we (optimistically) update the state
            # and store the state in the cache
            player.extra_data[ATTR_FAKE_POWER] = powered
            player.update_state()  # trigger update of the player state
            await self.mass.cache.set(
                key=player_id,
                data=powered,
                provider=self.domain,
                category=CACHE_CATEGORY_PLAYER_POWER,
            )
        else:
            # handle external player control
            player_control = self._controls.get(player.power_control)
            control_name = player_control.name if player_control else player.power_control
            self.logger.debug("Redirecting power command to PlayerControl %s", control_name)
            if not player_control or not player_control.supports_power:
                raise UnsupportedFeaturedException(
                    f"Player control {control_name} is not available"
                )
            if powered:
                assert player_control.power_on is not None  # for type checking
                await player_control.power_on()
            else:
                assert player_control.power_off is not None  # for type checking
                await player_control.power_off()

        # always trigger a state update to update the UI
        player.update_state()

        # handle 'auto play on power on' feature
        if (
            not player.active_group
            and powered
            and player.config.get_value(CONF_AUTO_PLAY)
            and player.active_source in (None, player_id)
            and not player.extra_data.get(ATTR_ANNOUNCEMENT_IN_PROGRESS)
        ):
            await self.mass.player_queues.resume(player_id)

    async def _handle_cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """
        Handle Player volume set command.

        Skips the permission checks (internal use only).
        """
        player = self.get(player_id, True)
        assert player is not None  # for type checker
        if player.type == PlayerType.GROUP:
            # redirect to special group volume control
            await self.cmd_group_volume(player_id, volume_level)
            return

        if player.volume_control == PLAYER_CONTROL_NONE:
            raise UnsupportedFeaturedException(
                f"Player {player.display_name} does not support volume control"
            )

        if (
            player.mute_control not in (PLAYER_CONTROL_NONE, PLAYER_CONTROL_FAKE)
            and player.volume_muted
        ):
            # if player is muted, we unmute it first
            # skip this for fake mute since it uses volume to simulate mute
            self.logger.debug(
                "Unmuting player %s before setting volume",
                player.display_name,
            )
            await self.cmd_volume_mute(player_id, False)

        # Check if a plugin source is active with a volume callback
        if plugin_source := self._get_active_plugin_source(player):
            if plugin_source.on_volume:
                await plugin_source.on_volume(volume_level)

        if player.volume_control == PLAYER_CONTROL_NATIVE:
            # player supports volume command natively: forward to player
            async with self._player_throttlers[player_id]:
                await player.volume_set(volume_level)
            return
        if player.volume_control == PLAYER_CONTROL_FAKE:
            # user wants to use fake volume control - so we (optimistically) update the state
            # and store the state in the cache
            player.extra_data[ATTR_FAKE_VOLUME] = volume_level
            # trigger update
            player.update_state()
            return
        # else: handle external player control
        player_control = self._controls.get(player.volume_control)
        control_name = player_control.name if player_control else player.volume_control
        self.logger.debug("Redirecting volume command to PlayerControl %s", control_name)
        if not player_control or not player_control.supports_volume:
            raise UnsupportedFeaturedException(f"Player control {control_name} is not available")
        async with self._player_throttlers[player_id]:
            assert player_control.volume_set is not None
            await player_control.volume_set(volume_level)

    def __iter__(self) -> Iterator[Player]:
        """Iterate over all players."""
        return iter(self._players.values())
