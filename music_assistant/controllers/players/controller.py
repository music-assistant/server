"""
MusicAssistant PlayerController.

Handles all logic to control supported players,
which are provided by Player Providers.

Note that the PlayerController has a concept of a 'player' and a 'playerstate'.
The Player is the actual object that is provided by the provider,
which incorporates the (unaltered) state of the player (e.g. volume, state, etc)
and functions for controlling the player (e.g. play, pause, etc).

The playerstate is the (final) state of the player, including any user customizations
and transformations that are applied to the player.
The playerstate is the object that is exposed to the outside world (via the API).
"""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, cast

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
from music_assistant_models.player import PlayerOptionValueType  # noqa: TC002
from music_assistant_models.player_control import PlayerControl  # noqa: TC002

from music_assistant.constants import (
    ANNOUNCE_ALERT_FILE,
    ATTR_ACTIVE_SOURCE,
    ATTR_ANNOUNCEMENT_IN_PROGRESS,
    ATTR_AVAILABLE,
    ATTR_ELAPSED_TIME,
    ATTR_ENABLED,
    ATTR_FAKE_MUTE,
    ATTR_FAKE_POWER,
    ATTR_FAKE_VOLUME,
    ATTR_GROUP_MEMBERS,
    ATTR_LAST_POLL,
    ATTR_MUTE_LOCK,
    ATTR_PREVIOUS_VOLUME,
    CONF_AUTO_PLAY,
    CONF_ENTRY_ANNOUNCE_VOLUME,
    CONF_ENTRY_ANNOUNCE_VOLUME_MAX,
    CONF_ENTRY_ANNOUNCE_VOLUME_MIN,
    CONF_ENTRY_ANNOUNCE_VOLUME_STRATEGY,
    CONF_ENTRY_TTS_PRE_ANNOUNCE,
    CONF_ENTRY_ZEROCONF_INTERFACES,
    CONF_PLAYER_DSP,
    CONF_PLAYERS,
    CONF_PRE_ANNOUNCE_CHIME_URL,
)
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    get_current_user,
    get_sendspin_player_id,
)
from music_assistant.helpers.api import api_command
from music_assistant.helpers.tags import async_parse_tags
from music_assistant.helpers.throttle_retry import Throttler
from music_assistant.helpers.util import TaskManager, validate_announcement_chime_url
from music_assistant.models.core_controller import CoreController
from music_assistant.models.player import Player, PlayerMedia, PlayerState
from music_assistant.models.player_provider import PlayerProvider
from music_assistant.models.plugin import PluginProvider, PluginSource

from .helpers import AnnounceData, handle_player_command
from .protocol_linking import ProtocolLinkingMixin

if TYPE_CHECKING:
    from collections.abc import Iterator

    from music_assistant_models.config_entries import (
        ConfigEntry,
        ConfigValueType,
        CoreConfig,
        PlayerConfig,
    )
    from music_assistant_models.player_queue import PlayerQueue

    from music_assistant import MusicAssistant

CACHE_CATEGORY_PLAYER_POWER = 1

# Context variable to prevent circular calls between players and player_queues controllers
IN_QUEUE_COMMAND: ContextVar[bool] = ContextVar("IN_QUEUE_COMMAND", default=False)


class PlayerController(ProtocolLinkingMixin, CoreController):
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
        # Lock to prevent race conditions during player registration
        self._register_lock = asyncio.Lock()
        # Track pending protocol player evaluations (delayed to allow all protocols to register)
        self._pending_protocol_evaluations: dict[str, asyncio.TimerHandle] = {}

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> tuple[ConfigEntry, ...]:
        """Return Config Entries for the Player Controller."""
        return (CONF_ENTRY_ZEROCONF_INTERFACES,)

    async def setup(self, config: CoreConfig) -> None:
        """Async initialize of module."""
        self._poll_task = self.mass.create_task(self._poll_players())

    async def close(self) -> None:
        """Cleanup on exit."""
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
        # Cancel all pending protocol evaluations
        for handle in self._pending_protocol_evaluations.values():
            handle.cancel()
        self._pending_protocol_evaluations.clear()

    async def on_provider_loaded(self, provider: PlayerProvider) -> None:
        """Handle logic when a provider is loaded."""

    async def on_provider_unload(self, provider: PlayerProvider) -> None:
        """Handle logic when a provider is (about to get) unloaded."""

    @property
    def providers(self) -> list[PlayerProvider]:
        """Return all loaded/running MusicProviders."""
        return cast("list[PlayerProvider]", self.mass.get_providers(ProviderType.PLAYER))

    def all_players(
        self,
        return_unavailable: bool = True,
        return_disabled: bool = False,
        provider_filter: str | None = None,
        return_protocol_players: bool = False,
    ) -> list[Player]:
        """
        Return all registered players.

        Note that this applies user filters for players (for non admin users).

        :param return_unavailable [bool]: Include unavailable players.
        :param return_disabled [bool]: Include disabled players.
        :param provider_filter [str]: Optional filter by provider lookup key.
        :param return_protocol_players [bool]: Include protocol players (hidden by default).

        :return: List of Player objects.
        """
        current_user = get_current_user()
        user_filter = (
            current_user.player_filter
            if current_user and current_user.role != UserRole.ADMIN
            else None
        )
        current_sendspin_player = get_sendspin_player_id()
        return [
            player
            for player in list(self._players.values())
            if (player.state.available or return_unavailable)
            and (player.state.enabled or return_disabled)
            and (provider_filter is None or player.provider.instance_id == provider_filter)
            and (
                not user_filter
                or player.player_id in user_filter
                or player.player_id == current_sendspin_player
            )
            and (return_protocol_players or player.state.type != PlayerType.PROTOCOL)
        ]

    @api_command("players/all")
    def all_player_states(
        self,
        return_unavailable: bool = True,
        return_disabled: bool = False,
        provider_filter: str | None = None,
        return_protocol_players: bool = False,
    ) -> list[PlayerState]:
        """
        Return PlayerState for all registered players.

        :param return_unavailable [bool]: Include unavailable players.
        :param return_disabled [bool]: Include disabled players.
        :param provider_filter [str]: Optional filter by provider lookup key.
        :param return_protocol_players [bool]: Include protocol players (hidden by default).

        :return: List of PlayerState objects.
        """
        return [
            player.state
            for player in self.all_players(
                return_unavailable=return_unavailable,
                return_disabled=return_disabled,
                provider_filter=provider_filter,
                return_protocol_players=return_protocol_players,
            )
        ]

    def get_player(
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
            if (not player.state.available or not player.state.enabled) and raise_unavailable:
                msg = f"Player {player_id} is not available"
                raise PlayerUnavailableError(msg)
            return player
        if raise_unavailable:
            msg = f"Player {player_id} is not available"
            raise PlayerUnavailableError(msg)
        return None

    @api_command("players/get")
    def get_player_state(
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
        current_sendspin_player = get_sendspin_player_id()
        if (
            current_user
            and user_filter
            and player_id not in user_filter
            and player_id != current_sendspin_player
        ):
            msg = f"{current_user.username} does not have access to player {player_id}"
            raise InsufficientPermissions(msg)
        if player := self.get_player(player_id, raise_unavailable):
            return player.state
        return None

    def get_player_by_name(self, name: str) -> Player | None:
        """
        Return Player by name.

        Performs case-insensitive matching against the player's state name
        (the final name visible in clients and API).
        If multiple players match, logs a warning and returns the first match.

        :param name: Name of the player.
        :return: Player object or None.
        """
        name_normalized = name.strip().lower()
        matches: list[Player] = []

        for player in list(self._players.values()):
            if player.state.name.strip().lower() == name_normalized:
                matches.append(player)

        if not matches:
            return None

        if len(matches) > 1:
            player_ids = [p.player_id for p in matches]
            self.logger.warning(
                "players/get_by_name: Multiple players found with name '%s': %s - "
                "returning first match (%s). "
                "Consider using the players/get API with player_id instead "
                "for unambiguous lookups.",
                name,
                player_ids,
                matches[0].player_id,
            )

        return matches[0]

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
        current_sendspin_player = get_sendspin_player_id()
        if player := self.get_player_by_name(name):
            if (
                current_user
                and user_filter
                and player.player_id not in user_filter
                and player.player_id != current_sendspin_player
            ):
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
        # Redirect to queue controller if it is active (skip if already in queue command context)
        if not IN_QUEUE_COMMAND.get() and (active_queue := self.get_active_queue(player)):
            await self.mass.player_queues.stop(active_queue.queue_id)
            return
        # Delegate to internal handler for actual implementation
        await self._handle_cmd_stop(player.player_id)

    @api_command("players/cmd/play")
    @handle_player_command
    async def cmd_play(self, player_id: str) -> None:
        """Send PLAY (unpause) command to given player.

        - player_id: player_id of the player to handle the command.
        """
        player = self._get_player_with_redirect(player_id)
        if player.state.playback_state == PlaybackState.PLAYING:
            self.logger.info(
                "Ignore PLAY request to player %s: player is already playing", player.state.name
            )
            return
        # player is not paused: check for queue redirect, then delegate to internal handler
        if player.state.playback_state != PlaybackState.PAUSED:
            source = player.state.active_source
            if active_queue := self.mass.player_queues.get(source or player_id):
                await self.mass.player_queues.resume(active_queue.queue_id)
                return

        # Delegate to internal handler for actual implementation
        await self._handle_cmd_play(player.player_id)

    @api_command("players/cmd/pause")
    @handle_player_command
    async def cmd_pause(self, player_id: str) -> None:
        """Send PAUSE command to given player.

        - player_id: player_id of the player to handle the command.
        """
        player = self._get_player_with_redirect(player_id)
        # Redirect to queue controller if it is active (skip if already in queue command context)
        if not IN_QUEUE_COMMAND.get() and (active_queue := self.get_active_queue(player)):
            await self.mass.player_queues.pause(active_queue.queue_id)
            return
        # Delegate to internal handler for actual implementation
        await self._handle_cmd_pause(player.player_id)

    @api_command("players/cmd/play_pause")
    async def cmd_play_pause(self, player_id: str) -> None:
        """Toggle play/pause on given player.

        - player_id: player_id of the player to handle the command.
        """
        player = self._get_player_with_redirect(player_id)
        if player.state.playback_state == PlaybackState.PLAYING:
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
        if not IN_QUEUE_COMMAND.get() and (active_queue := self.get_active_queue(player)):
            await self.mass.player_queues.seek(active_queue.queue_id, position)
            return
        # handle command on player/source directly
        active_source = next((x for x in player.source_list if x.id == player.active_source), None)
        if active_source and not active_source.can_seek:
            msg = (
                f"The active source ({active_source.name}) on player "
                f"{player.display_name} does not support seeking"
            )
            raise PlayerCommandFailed(msg)
        if PlayerFeature.SEEK not in player.supported_features:
            msg = f"Player {player.display_name} does not support seeking"
            raise UnsupportedFeaturedException(msg)
        # handle command on player directly
        await player.seek(position)

    @api_command("players/cmd/next")
    async def cmd_next_track(self, player_id: str) -> None:
        """Handle NEXT TRACK command for given player."""
        player = self._get_player_with_redirect(player_id)
        active_source_id = player.state.active_source or player.player_id
        # Check if a plugin source is active with a next callback
        if plugin_source := self._get_active_plugin_source(player):
            if plugin_source.can_next_previous and plugin_source.on_next:
                await plugin_source.on_next()
                return
        # Redirect to queue controller if it is active
        if active_queue := self.get_active_queue(player):
            await self.mass.player_queues.next(active_queue.queue_id)
            return
        if PlayerFeature.NEXT_PREVIOUS in player.state.supported_features:
            # player has some other source active and native next/previous support
            active_source = next(
                (x for x in player.state.source_list if x.id == active_source_id), None
            )
            if active_source and active_source.can_next_previous:
                await player.next_track()
                return
            msg = "This action is (currently) unavailable for this source."
            raise PlayerCommandFailed(msg)
        # Player does not support next/previous feature
        msg = f"Player {player.state.name} does not support skipping to the next track."
        raise UnsupportedFeaturedException(msg)

    @api_command("players/cmd/previous")
    async def cmd_previous_track(self, player_id: str) -> None:
        """Handle PREVIOUS TRACK command for given player."""
        player = self._get_player_with_redirect(player_id)
        active_source_id = player.state.active_source or player.player_id
        # Check if a plugin source is active with a previous callback
        if plugin_source := self._get_active_plugin_source(player):
            if plugin_source.can_next_previous and plugin_source.on_previous:
                await plugin_source.on_previous()
                return
        # Redirect to queue controller if it is active
        if active_queue := self.get_active_queue(player):
            await self.mass.player_queues.previous(active_queue.queue_id)
            return
        if PlayerFeature.NEXT_PREVIOUS in player.state.supported_features:
            # player has some other source active and native next/previous support
            active_source = next(
                (x for x in player.state.source_list if x.id == active_source_id), None
            )
            if active_source and active_source.can_next_previous:
                await player.previous_track()
                return
            msg = "This action is (currently) unavailable for this source."
            raise PlayerCommandFailed(msg)
        # Player does not support next/previous feature
        msg = f"Player {player.state.name} does not support skipping to the previous track."
        raise UnsupportedFeaturedException(msg)

    @api_command("players/cmd/power")
    @handle_player_command
    async def cmd_power(self, player_id: str, powered: bool) -> None:
        """Send POWER command to given player.

        :param player_id: player_id of the player to handle the command.
        :param powered: bool if player should be powered on or off.
        """
        await self._handle_cmd_power(player_id, powered)

    @api_command("players/cmd/volume_set")
    @handle_player_command
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
        if not (player := self.get_player(player_id)):
            return
        current_volume = player.state.volume_level or 0
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
        if not (player := self.get_player(player_id)):
            return
        current_volume = player.state.volume_level or 0
        if current_volume < 5 or current_volume > 95:
            step_size = 1
        elif current_volume < 20 or current_volume > 80:
            step_size = 2
        else:
            step_size = 5
        new_volume = max(0, current_volume - step_size)
        await self.cmd_volume_set(player_id, new_volume)

    @api_command("players/cmd/group_volume")
    @handle_player_command
    async def cmd_group_volume(
        self,
        player_id: str,
        volume_level: int,
    ) -> None:
        """
        Handle adjusting the overall/group volume to a playergroup (or synced players).

        Will set a new (overall) volume level to a group player or syncgroup.

        :param player_id: Player ID of group player or syncleader to handle the command.
        :param volume_level: Volume level (0..100) to set to the group.
        """
        player = self.get_player(player_id, True)
        assert player is not None  # for type checker
        if player.state.type == PlayerType.GROUP or player.state.group_members:
            # dedicated group player or sync leader
            await self.set_group_volume(player, volume_level)
            return
        if player.state.synced_to and (sync_leader := self.get_player(player.state.synced_to)):
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
        group_player_state = self.get_player_state(player_id, True)
        assert group_player_state
        cur_volume = group_player_state.group_volume
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
        group_player_state = self.get_player_state(player_id, True)
        assert group_player_state
        cur_volume = group_player_state.group_volume
        if cur_volume < 5 or cur_volume > 95:
            step_size = 1
        elif cur_volume < 20 or cur_volume > 80:
            step_size = 2
        else:
            step_size = 5
        new_volume = max(0, cur_volume - step_size)
        await self.cmd_group_volume(player_id, new_volume)

    @api_command("players/cmd/group_volume_mute")
    @handle_player_command
    async def cmd_group_volume_mute(self, player_id: str, muted: bool) -> None:
        """Send VOLUME_MUTE command to all players in a group.

        - player_id: player_id of the group player or sync leader.
        - muted: bool if group should be muted.
        """
        player = self.get_player(player_id, True)
        assert player is not None  # for type checker
        if player.state.type == PlayerType.GROUP or player.state.group_members:
            # dedicated group player or sync leader
            coros = []
            for child_player in self.iter_group_members(
                player, only_powered=True, exclude_self=False
            ):
                coros.append(self.cmd_volume_mute(child_player.player_id, muted))
            await asyncio.gather(*coros)

    @api_command("players/cmd/volume_mute")
    @handle_player_command
    async def cmd_volume_mute(self, player_id: str, muted: bool) -> None:
        """Send VOLUME_MUTE command to given player.

        - player_id: player_id of the player to handle the command.
        - muted: bool if player should be muted.
        """
        player = self.get_player(player_id, True)
        assert player

        # Set/clear mute lock for players in a group
        # This prevents auto-unmute when group volume changes
        is_in_group = bool(player.state.synced_to or player.state.group_members)
        if muted and is_in_group:
            player.extra_data[ATTR_MUTE_LOCK] = True
        elif not muted:
            player.extra_data.pop(ATTR_MUTE_LOCK, None)

        if player.volume_control == PLAYER_CONTROL_NONE:
            raise UnsupportedFeaturedException(
                f"Player {player.state.name} does not support muting"
            )
        if player.mute_control == PLAYER_CONTROL_NATIVE:
            # player supports mute command natively: forward to player
            await player.volume_mute(muted)
            return
        if player.mute_control == PLAYER_CONTROL_FAKE:
            # user wants to use fake mute control - so we use volume instead
            self.logger.debug(
                "Using volume for muting for player %s",
                player.state.name,
            )
            if muted:
                player.extra_data[ATTR_PREVIOUS_VOLUME] = player.state.volume_level
                player.extra_data[ATTR_FAKE_MUTE] = True
                await self._handle_cmd_volume_set(player_id, 0)
                player.update_state()
            else:
                prev_volume = player.extra_data.get(ATTR_PREVIOUS_VOLUME, 1)
                player.extra_data[ATTR_FAKE_MUTE] = False
                player.update_state()
                await self._handle_cmd_volume_set(player_id, prev_volume)
            return

        # handle external player control
        if player_control := self._controls.get(player.mute_control):
            control_name = player_control.name if player_control else player.mute_control
            self.logger.debug("Redirecting mute command to PlayerControl %s", control_name)
            if not player_control or not player_control.supports_mute:
                raise UnsupportedFeaturedException(
                    f"Player control {control_name} is not available"
                )
            assert player_control.mute_set is not None
            await player_control.mute_set(muted)
            return

        # handle to protocol player as volume_mute control
        if protocol_player := self.get_player(player.state.volume_control):
            self.logger.debug(
                "Redirecting mute command to protocol player %s",
                protocol_player.provider.manifest.name,
            )
            await self.cmd_volume_mute(protocol_player.player_id, muted)
            return

    @api_command("players/cmd/play_announcement")
    @handle_player_command(lock=True)
    async def play_announcement(
        self,
        player_id: str,
        url: str,
        pre_announce: bool | None = None,
        volume_level: int | None = None,
        pre_announce_url: str | None = None,
    ) -> None:
        """
        Handle playback of an announcement (url) on given player.

        :param player_id: Player ID of the player to handle the command.
        :param url: URL of the announcement to play.
        :param pre_announce: Optional bool if pre-announce should be used.
        :param volume_level: Optional volume level to set for the announcement.
        :param pre_announce_url: Optional custom URL to use for the pre-announce chime.
        """
        player = self.get_player(player_id, True)
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
            if player.state.type == PlayerType.GROUP and (
                all(
                    PlayerFeature.PLAY_ANNOUNCEMENT in x.state.supported_features
                    for x in self.iter_group_members(player)
                )
            ):
                # forward the request to each individual player
                async with TaskManager(self.mass) as tg:
                    for group_member in player.state.group_members:
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
                player.state.name,
                pre_announce,
                url,
            )
            # determine if the player has native announcements support
            # or if any linked protocol has announcement support
            native_announce_support = False
            if announce_player := self._get_control_target(
                player,
                required_feature=PlayerFeature.PLAY_ANNOUNCEMENT,
                require_active=False,
            ):
                native_announce_support = True
            else:
                announce_player = player
            # create a PlayerMedia object for the announcement so
            # we can send a regular play-media call downstream
            announce_data = AnnounceData(
                announcement_url=url,
                pre_announce=bool(pre_announce),
                pre_announce_url=pre_announce_url,
            )
            announcement = PlayerMedia(
                uri=self.mass.streams.get_announcement_url(player_id, announce_data=announce_data),
                media_type=MediaType.ANNOUNCEMENT,
                title="Announcement",
                custom_data=dict(announce_data),
            )
            # handle native announce support (player or linked protocol)
            if native_announce_support:
                announcement_volume = self.get_announcement_volume(player_id, volume_level)
                await announce_player.play_announcement(announcement, announcement_volume)
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
        # Delegate to internal handler for actual implementation
        await self._handle_play_media(player.player_id, media)

    @api_command("players/cmd/select_sound_mode")
    @handle_player_command
    async def select_sound_mode(self, player_id: str, sound_mode: str) -> None:
        """
        Handle SELECT SOUND MODE command on given player.

        - player_id: player_id of the player to handle the command
        - sound_mode: The ID of the sound mode that needs to be activated/selected.
        """
        player = self.get_player(player_id, True)
        assert player is not None  # for type checking

        if PlayerFeature.SELECT_SOUND_MODE not in player.supported_features:
            raise UnsupportedFeaturedException(
                f"Player {player.display_name} does not support sound mode selection"
            )

        prev_sound_mode = player.active_sound_mode
        if sound_mode == prev_sound_mode:
            return

        # basic check if sound mode is valid for player
        if not any(x for x in player.sound_mode_list if x.id == sound_mode):
            raise PlayerCommandFailed(
                f"{sound_mode} is an invalid sound_mode for player {player.display_name}"
            )

        # forward to player
        await player.select_sound_mode(sound_mode)

    @api_command("players/cmd/set_option")
    @handle_player_command
    async def set_option(
        self, player_id: str, option_key: str, option_value: PlayerOptionValueType
    ) -> None:
        """
        Handle SET_OPTION command on given player.

        - player_id: player_id of the player to handle the command
        - option_key: The key of the player option that needs to be activated/selected.
        - option_value: The new value of the player option.
        """
        player = self.get_player(player_id, True)
        assert player is not None  # for type checking

        if PlayerFeature.OPTIONS not in player.supported_features:
            raise UnsupportedFeaturedException(
                f"Player {player.display_name} does not support set_option"
            )

        prev_player_option = next((x for x in player.options if x.key == option_key), None)
        if not prev_player_option:
            return
        if prev_player_option.value == option_value:
            return

        if prev_player_option.read_only:
            raise UnsupportedFeaturedException(
                f"Player {player.display_name} option {option_key} is read-only"
            )

        # forward to player
        await player.set_option(option_key=option_key, option_value=option_value)

    @api_command("players/cmd/select_source")
    @handle_player_command
    async def select_source(self, player_id: str, source: str | None) -> None:
        """
        Handle SELECT SOURCE command on given player.

        - player_id: player_id of the player to handle the command.
        - source: The ID of the source that needs to be activated/selected.
        """
        if source is None:
            source = player_id  # default to MA queue source
        player = self.get_player(player_id, True)
        assert player is not None  # for type checking
        # Check if player is currently grouped (reject for public API)
        if player.state.synced_to or player.state.active_group:
            raise PlayerCommandFailed(f"Player {player.state.name} is currently grouped")
        # Delegate to internal handler for actual implementation
        await self._handle_select_source(player_id, source)

    @handle_player_command(lock=True)
    async def enqueue_next_media(self, player_id: str, media: PlayerMedia) -> None:
        """
        Handle enqueuing of a next media item on the player.

        :param player_id: player_id of the player to handle the command.
        :param media: The Media that needs to be enqueued on the player.
        :raises UnsupportedFeaturedException: if the player does not support enqueueing.
        :raises PlayerUnavailableError: if the player is not available.
        """
        # Note: No group redirect needed here as enqueue doesn't use _get_player_with_redirect
        # Delegate to internal handler for actual implementation
        await self._handle_enqueue_next_media(player_id, media)

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
        parent_player: Player | None = self.get_player(target_player, True)
        assert parent_player is not None  # for type checking
        if PlayerFeature.SET_MEMBERS not in parent_player.state.supported_features:
            msg = f"Player {parent_player.name} does not support group commands"
            raise UnsupportedFeaturedException(msg)

        # guard edge case: player already synced to another player
        if parent_player.state.synced_to:
            raise PlayerCommandFailed(
                f"Player {parent_player.name} is already synced to another player on its own, "
                "you need to ungroup it first before you can join other players to it.",
            )
        # handle dissolve sync group if the target player is currently
        # a sync leader and is being removed from itself
        should_stop = False
        if player_ids_to_remove and target_player in player_ids_to_remove:
            self.logger.info(
                "Dissolving sync group of player %s as it is being removed from itself",
                parent_player.name,
            )
            player_ids_to_add = None
            player_ids_to_remove = [
                x for x in parent_player.state.group_members if x != target_player
            ]
            should_stop = True
        # filter all player ids on compatibility and availability
        final_player_ids_to_add: list[str] = []
        for child_player_id in player_ids_to_add or []:
            if child_player_id == target_player:
                continue
            if child_player_id in final_player_ids_to_add:
                continue
            if (
                not (child_player := self.get_player(child_player_id))
                or not child_player.state.available
            ):
                self.logger.warning("Player %s is not available", child_player_id)
                continue

            # check if player can be synced/grouped with the target player
            # state.can_group_with already handles all expansion and translation
            if child_player_id not in parent_player.state.can_group_with:
                self.logger.warning(
                    "Player %s can not be grouped with %s",
                    child_player.name,
                    parent_player.name,
                )
                continue

            if (
                child_player.state.synced_to
                and child_player.state.synced_to == target_player
                and child_player_id in parent_player.state.group_members
            ):
                continue  # already synced to this target

            # power on the player if needed
            if (
                not child_player.state.powered
                and child_player.state.power_control != PLAYER_CONTROL_NONE
            ):
                await self._handle_cmd_power(child_player.player_id, True)
            # if we reach here, all checks passed
            final_player_ids_to_add.append(child_player_id)

        # process player ids to remove and filter out invalid/unavailable players and edge cases
        final_player_ids_to_remove: list[str] = []
        if player_ids_to_remove:
            for child_player_id in player_ids_to_remove:
                if child_player_id not in parent_player.state.group_members:
                    continue
                final_player_ids_to_remove.append(child_player_id)

        # Forward command to the appropriate player after all (base) sanity checks
        # GROUP players (sync_group, universal_group) manage their own members internally
        # and don't need protocol translation - call their set_members directly
        if parent_player.type == PlayerType.GROUP:
            await parent_player.set_members(
                player_ids_to_add=final_player_ids_to_add,
                player_ids_to_remove=final_player_ids_to_remove,
            )
            return
        # For regular players, handle protocol selection and translation
        # Store playback state before changing members to detect protocol changes
        was_playing = parent_player.playback_state in (
            PlaybackState.PLAYING,
            PlaybackState.PAUSED,
        )
        previous_protocol = parent_player.active_output_protocol if was_playing else None

        await self._handle_set_members_with_protocols(
            parent_player, final_player_ids_to_add, final_player_ids_to_remove
        )

        if should_stop:
            # Stop playback on the player if it is being removed from itself
            await self._handle_cmd_stop(parent_player.player_id)
            return

        # Check if protocol changed due to member change and restart playback if needed
        if not should_stop and was_playing:
            # Determine which protocol would be used now with new members
            _new_target_player, new_protocol = self._select_best_output_protocol(parent_player)
            new_protocol_id = new_protocol.output_protocol_id if new_protocol else "native"
            previous_protocol_id = previous_protocol or "native"

            # If protocol changed, restart playback
            if new_protocol_id != previous_protocol_id:
                self.logger.info(
                    "Protocol changed from %s to %s due to member change, restarting playback",
                    previous_protocol_id,
                    new_protocol_id,
                )
                # Restart playback on the new protocol using resume
                await self.cmd_resume(
                    parent_player.player_id,
                    parent_player.state.active_source,
                    parent_player.state.current_media,
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
        This is a (deprecated) alias for cmd_set_members.
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
        if not (player := self.get_player(player_id)):
            self.logger.warning("Player %s is not available", player_id)
            return

        if (
            player.state.active_group
            and (group_player := self.get_player(player.state.active_group))
            and (PlayerFeature.SET_MEMBERS in group_player.state.supported_features)
        ):
            # the player is part of a (permanent) groupplayer and the user tries to ungroup
            if player_id in group_player.static_group_members:
                raise UnsupportedFeaturedException(
                    f"Player {player.name}  is a static member of group {group_player.name} "
                    "and cannot be removed from that group!"
                )
            await group_player.set_members(player_ids_to_remove=[player_id])
            return

        if player.state.synced_to and (synced_player := self.get_player(player.state.synced_to)):
            # player is a sync member
            await synced_player.set_members(player_ids_to_remove=[player_id])
            return

        if not (player.state.synced_to or player.state.group_members):
            return  # nothing to do

        if PlayerFeature.SET_MEMBERS not in player.state.supported_features:
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

        :param provider: The provider (id) to create the group player for.
        :param name: Name of the new group player.
        :param members: List of player ids to add to the group.
        :param dynamic: Whether the group is dynamic (members can change).
        """
        if not (provider_instance := self.mass.get_provider(provider)):
            raise ProviderUnavailableError(f"Provider {provider} not found")
        provider_instance = cast("PlayerProvider", provider_instance)
        if ProviderFeature.CREATE_GROUP_PLAYER not in provider_instance.supported_features:
            raise UnsupportedFeaturedException(
                f"Provider {provider} does not support creating group players"
            )
        return await provider_instance.create_group_player(name, members, dynamic)

    @api_command("players/remove_group_player", required_role="admin")
    async def remove_group_player(self, player_id: str) -> None:
        """Remove a group player."""
        if not (player := self.get_player(player_id)):
            # we simply permanently delete the player by wiping its config
            self.mass.config.remove(f"players/{player_id}")
            return
        if player.state.type != PlayerType.GROUP:
            raise UnsupportedFeaturedException(f"Player {player.state.name} is not a group player")
        player.provider.check_feature(ProviderFeature.REMOVE_GROUP_PLAYER)
        await player.provider.remove_group_player(player_id)

    @api_command("players/add_currently_playing_to_favorites")
    async def add_currently_playing_to_favorites(self, player_id: str) -> None:
        """
        Add the currently playing item/track on given player to the favorites.

        This tries to resolve the currently playing media to an actual media item
        and add that to the favorites in the library. Will raise an error if the
        player is not currently playing anything or if the currently playing media
        can not be resolved to a media item.
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
        if not player.state.active_source:
            raise PlayerCommandFailed("Player has no active source")
        # handle other source active using the current_media with uri
        if current_media := player.state.current_media:
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

        # Use lock to prevent race conditions during concurrent player registrations
        async with self._register_lock:
            player_id = player.player_id

            if player_id in self._players:
                msg = f"Player {player_id} is already registered!"
                raise AlreadyRegisteredError(msg)

            # ignore disabled players
            if not player.state.enabled:
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
            # update state without signaling event first (ensure all attributes are set)
            player.update_state(signal_event=False)

            # ensure we fetch and set the latest/full config for the player
            player_config = await self.mass.config.get_player_config(player_id)
            player.set_config(player_config)
            # call hook after the player is registered and config is set
            await player.on_config_updated()

            # Handle protocol linking
            # First enrich identifiers with real MAC (resolves virtual MACs via ARP)
            await self._enrich_player_identifiers(player)
            self._evaluate_protocol_links(player)

            self.logger.info(
                "Player (type %s) registered: %s/%s",
                player.state.type.value,
                player_id,
                player.state.name,
            )
            # signal event that a player was added

            if player.state.type != PlayerType.PROTOCOL:
                self.mass.signal_event(
                    EventType.PLAYER_ADDED, object_id=player.player_id, data=player
                )

            # register playerqueue for this player
            # Skip if this is a protocol player pending evaluation (queue created when promoted)
            if (
                player.state.type != PlayerType.PROTOCOL
                and player.player_id not in self._pending_protocol_evaluations
            ):
                await self.mass.player_queues.on_player_register(player)

        # always call update to fix special attributes like display name, group volume etc.
        player.update_state()

        # Schedule debounced update of all players since can_group_with values may change
        # when a new player is added (provider IDs expand to include the new player)
        self._schedule_update_all_players()

    async def register_or_update(self, player: Player) -> None:
        """Register a new player on the controller or update existing one."""
        if self.mass.closing:
            return

        if player.player_id in self._players:
            self._players[player.player_id] = player
            player.update_state()
            # Also schedule update when replacing existing player
            self._schedule_update_all_players()
            return

        await self.register(player)

    def trigger_player_update(
        self, player_id: str, force_update: bool = False, debounce_delay: float = 0.25
    ) -> None:
        """Trigger a (debounced) update for the given player."""
        if self.mass.closing:
            return
        if not (player := self.get_player(player_id)):
            return
        task_id = f"player_update_state_{player_id}"
        self.mass.call_later(
            debounce_delay,
            player.update_state,
            force_update=force_update,
            task_id=task_id,
        )

    async def unregister(self, player_id: str, permanent: bool = False) -> None:
        """
        Unregister a player from the player controller.

        Called (by a PlayerProvider) when a player is removed or no longer available
        (for a longer period of time). This will remove the player from the player
        controller and optionally remove the player's config from the mass config.
        If the player is not registered, this will silently be ignored.

        :param player_id: Player ID of the player to unregister.
        :param permanent: If True, remove the player permanently by deleting its config.
                          If False, the player config will not be removed.
        """
        player = self._players.get(player_id)
        if player is None:
            return
        await self._cleanup_player_memberships(player_id)
        del self._players[player_id]
        self.mass.player_queues.on_player_remove(player_id, permanent=permanent)
        await player.on_unload()
        if permanent:
            # player permanent removal: cleanup protocol links, delete config
            # and signal PLAYER_REMOVED event
            self._cleanup_protocol_links(player)
            self.delete_player_config(player_id)
            self.logger.info("Player removed: %s", player.name)
            if player.state.type != PlayerType.PROTOCOL:
                self.mass.signal_event(EventType.PLAYER_REMOVED, player_id)
        else:
            # temporary unavailable: mark player as unavailable
            # note: the player will be re-registered later if it comes back online
            player.state.available = False
            self.logger.info("Player unavailable: %s", player.name)
            if player.state.type != PlayerType.PROTOCOL:
                self.mass.signal_event(
                    EventType.PLAYER_UPDATED, object_id=player.player_id, data=player.state
                )
        # Schedule debounced update of all players since can_group_with values may change
        self._schedule_update_all_players()

    @api_command("players/remove", required_role="admin")
    async def remove(self, player_id: str) -> None:
        """
        Remove a player from a provider.

        Can only be called when a PlayerProvider supports ProviderFeature.REMOVE_PLAYER.
        """
        player = self.get_player(player_id)
        if player is None:
            # we simply permanently delete the player config since it is not registered
            self.delete_player_config(player_id)
            return
        if player.state.type == PlayerType.GROUP:
            # Handle group player removal
            player.provider.check_feature(ProviderFeature.REMOVE_GROUP_PLAYER)
            await player.provider.remove_group_player(player_id)
            return
        player.provider.check_feature(ProviderFeature.REMOVE_PLAYER)
        await player.provider.remove_player(player_id)
        # check for group memberships that need to be updated
        if player.state.active_group and (
            group_player := self.mass.players.get_player(player.state.active_group)
        ):
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
        if not player.state.enabled and ATTR_ENABLED not in changed_values:
            return

        if len(changed_values) == 0 and not force_update:
            # nothing changed
            return

        # always signal update to the playerqueue
        if player.state.type != PlayerType.PROTOCOL:
            self.mass.player_queues.on_player_update(player, changed_values)

        # to prevent spamming the eventbus on small changes (e.g. elapsed time),
        # we check if there are only changes in the elapsed time
        clean_changed_keys = set(changed_values.keys()) - {"current_media.elapsed_time"}
        if clean_changed_keys == {ATTR_ELAPSED_TIME} and not force_update:
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
                if removed_player := self.get_player(_removed_player_id):
                    removed_player.update_state()

        # Handle external source takeover - detect when active_source changes to
        # something external while we have a grouped protocol active
        if ATTR_ACTIVE_SOURCE in changed_values:
            prev_source, new_source = changed_values[ATTR_ACTIVE_SOURCE]
            task_id = f"external_source_takeover_{player_id}"
            self.mass.call_later(
                3,
                self._handle_external_source_takeover,
                player,
                prev_source,
                new_source,
                task_id=task_id,
            )
        became_inactive = False
        if ATTR_AVAILABLE in changed_values:
            became_inactive = changed_values[ATTR_AVAILABLE][1] is False
        if not became_inactive and ATTR_ENABLED in changed_values:
            became_inactive = changed_values[ATTR_ENABLED][1] is False
        if became_inactive and (player.state.active_group or player.state.synced_to):
            self.mass.create_task(self._cleanup_player_memberships(player.player_id))

        # signal player update on the eventbus
        if player.state.type != PlayerType.PROTOCOL:
            self.mass.signal_event(EventType.PLAYER_UPDATED, object_id=player_id, data=player)

        # signal a separate PlayerOptionsUpdated event
        if options := changed_values.get("options"):
            self.mass.signal_event(
                EventType.PLAYER_OPTIONS_UPDATED, object_id=player_id, data=options
            )

        if skip_forward and not force_update:
            return

        # update/signal group player(s) child's when group updates
        for child_player in self.iter_group_members(player, exclude_self=True):
            self.trigger_player_update(child_player.player_id)
        # update/signal group player(s) when child updates
        for group_player in self._get_player_groups(player, powered_only=False):
            self.trigger_player_update(group_player.player_id)
        # update/signal manually synced to player when child updates
        if (synced_to := player.state.synced_to) and (
            synced_to_player := self.get_player(synced_to)
        ):
            self.trigger_player_update(synced_to_player.player_id)
        # update/signal active groups when a group member updates
        if (active_group := player.state.active_group) and (
            active_group_player := self.get_player(active_group)
        ):
            self.trigger_player_update(active_group_player.player_id)
        # If this is a protocol player, forward the state update to the parent player
        if player.protocol_parent_id and (
            parent_player := self.mass.players.get_player(player.protocol_parent_id)
        ):
            self.trigger_player_update(parent_player.player_id)
        # If this is a parent player with linked protocols, forward state updates
        # to linked protocol players so their state reflects parent dependencies
        if player.state.type != PlayerType.PROTOCOL and player.linked_output_protocols:
            for linked in player.linked_output_protocols:
                if protocol_player := self.mass.players.get_player(linked.output_protocol_id):
                    self.mass.players.trigger_player_update(protocol_player.player_id)
        # trigger update of all players in a provider if group related fields changed
        if any(key in changed_values for key in ("group_members", "synced_to", "available")):
            for prov_player in player.provider.players:
                self.trigger_player_update(prov_player.player_id)

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
        for player in list(self._players.values()):
            if control_id in (
                player.state.power_control,
                player.state.volume_control,
                player.state.mute_control,
            ):
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
            if sync_leader := self.get_player(player.synced_to):
                return self.get_active_queue(sync_leader)
        # handle active group player
        if player.state.active_group and player.state.active_group != player.player_id:
            if group_player := self.get_player(player.state.active_group):
                return self.get_active_queue(group_player)
        # active_source may be filled queue id (or None)
        active_source = player.state.active_source or player.player_id
        if active_queue := self.mass.player_queues.get(active_source):
            return active_queue
        # handle active protocol player with parent player queue
        if player.type == PlayerType.PROTOCOL and player.protocol_parent_id:
            if parent_player := self.mass.players.get_player(player.protocol_parent_id):
                return self.get_active_queue(parent_player)
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
            if child_player.state.volume_control == PLAYER_CONTROL_NONE:
                continue
            cur_child_volume = child_player.state.volume_level or 0
            new_child_volume = int(cur_child_volume + volume_dif)
            new_child_volume = max(0, new_child_volume)
            new_child_volume = min(100, new_child_volume)
            # Use private method to skip permission check - already validated on group
            # ATTR_MUTE_LOCK on muted players prevents auto-unmute during group volume changes
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
            if (player := self.get_player(player_id)) and player.state.volume_level is not None:
                volume_level = int(
                    player.state.volume_level + cast("float", volume_strategy_volume)
                )
        elif volume_level is None and volume_strategy == "percentual":
            if (player := self.get_player(player_id)) and player.state.volume_level is not None:
                percentual = (player.state.volume_level / 100) * cast(
                    "float", volume_strategy_volume
                )
                volume_level = int(player.state.volume_level + percentual)
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
        for child_id in list(group_player.state.group_members):
            if child_player := self.get_player(child_id, False):
                if not child_player.state.available or not child_player.state.enabled:
                    continue
                if only_powered and child_player.state.powered is False:
                    continue
                if active_only and child_player.state.active_group != group_player.player_id:
                    continue
                if exclude_self and child_player.player_id == group_player.player_id:
                    continue
                if only_playing and child_player.state.playback_state not in (
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
            "Waiting for player %s to reach state %s", player.state.name, wanted_state
        )
        try:
            async with asyncio.timeout(timeout):
                while player.state.playback_state != wanted_state:
                    await asyncio.sleep(0.1)

        except TimeoutError:
            self.logger.debug(
                "Player %s did not reach state %s within the timeout of %s seconds",
                player.state.name,
                wanted_state,
                timeout,
            )
        elapsed_time = round(time.time() - start_timestamp, 2)
        if elapsed_time < minimal_time:
            self.logger.debug(
                "Player %s reached state %s too soon (%s vs %s seconds) - add fallback sleep...",
                player.state.name,
                wanted_state,
                elapsed_time,
                minimal_time,
            )
            await asyncio.sleep(minimal_time - elapsed_time)
        else:
            self.logger.debug(
                "Player %s reached state %s within %s seconds",
                player.state.name,
                wanted_state,
                elapsed_time,
            )

    async def on_player_config_change(self, config: PlayerConfig, changed_keys: set[str]) -> None:
        """Call (by config manager) when the configuration of a player changes."""
        player = self.get_player(config.player_id)
        player_provider = self.mass.get_provider(config.provider)
        player_disabled = ATTR_ENABLED in changed_keys and not config.enabled
        player_enabled = ATTR_ENABLED in changed_keys and config.enabled

        if player_disabled and player and player.state.available:
            # edge case: ensure that the player is powered off if the player gets disabled
            if player.state.power_control != PLAYER_CONTROL_NONE:
                await self._handle_cmd_power(config.player_id, False)
            elif player.state.playback_state != PlaybackState.IDLE:
                await self.cmd_stop(config.player_id)

        # signal player provider that the player got enabled/disabled
        if (player_enabled or player_disabled) and player_provider:
            assert isinstance(player_provider, PlayerProvider)  # for type checking
            if player_disabled:
                player_provider.on_player_disabled(config.player_id)
            elif player_enabled:
                player_provider.on_player_enabled(config.player_id)
            return  # enabling/disabling a player will be handled by the provider

        if not player:
            return  # guard against player not being registered (yet)

        resume_queue: PlayerQueue | None = (
            self.mass.player_queues.get(player.state.active_source)
            if player.state.active_source
            else None
        )

        # ensure player state gets updated with any updated config
        player.set_config(config)
        await player.on_config_updated()
        player.update_state()
        # if the PlayerQueue was playing, restart playback
        if resume_queue and resume_queue.state == PlaybackState.PLAYING:
            requires_restart = any(
                v for v in config.values.values() if v.key in changed_keys and v.requires_reload
            )
            if requires_restart:
                # always stop first to ensure the player uses the new config
                await self.mass.player_queues.stop(resume_queue.queue_id)
                self.mass.call_later(
                    1, self.mass.player_queues.resume, resume_queue.queue_id, False
                )

    async def on_player_dsp_change(self, player_id: str) -> None:
        """Call (by config manager) when the DSP settings of a player change."""
        # signal player provider that the config changed
        if not (player := self.get_player(player_id)):
            return
        if player.state.playback_state == PlaybackState.PLAYING:
            self.logger.info("Restarting playback of Player %s after DSP change", player_id)
            # this will restart the queue stream/playback
            if player.mass_queue_active:
                self.mass.call_later(
                    0, self.mass.player_queues.resume, player.state.active_source, False
                )
                return
            # if the player is not using a queue, we need to stop and start playback
            await self.cmd_stop(player_id)
            await self.cmd_play(player_id)

    async def _cleanup_player_memberships(self, player_id: str) -> None:
        """Ensure a player is detached from any groups or syncgroups."""
        if not (player := self.get_player(player_id)):
            return

        if (
            player.state.active_group
            and (group := self.get_player(player.state.active_group))
            and group.supports_feature(PlayerFeature.SET_MEMBERS)
        ):
            # Ungroup the player if its part of an active group, this will ignore
            # static_group_members since that is only checked when using cmd_set_members
            with suppress(UnsupportedFeaturedException, PlayerCommandFailed):
                await group.set_members(player_ids_to_remove=[player_id])
        elif player.state.synced_to and player.supports_feature(PlayerFeature.SET_MEMBERS):
            # Remove the player if it was synced, otherwise it will still show as
            # synced to the other player after it gets registered again
            with suppress(UnsupportedFeaturedException, PlayerCommandFailed):
                await player.ungroup()

    def _get_player_with_redirect(self, player_id: str) -> Player:
        """Get player with check if playback related command should be redirected."""
        player = self.get_player(player_id, True)
        assert player is not None  # for type checking
        if player.state.synced_to and (sync_leader := self.get_player(player.state.synced_to)):
            self.logger.info(
                "Player %s is synced to %s and can not accept "
                "playback related commands itself, "
                "redirected the command to the sync leader.",
                player.name,
                sync_leader.name,
            )
            return sync_leader
        if player.state.active_group and (
            active_group := self.get_player(player.state.active_group)
        ):
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
            if player.state.active_source == plugin_source.id:
                return plugin_source
        return None

    def _get_player_groups(
        self, player: Player, available_only: bool = True, powered_only: bool = False
    ) -> Iterator[Player]:
        """Return all groupplayers the given player belongs to."""
        for _player in self.all_players(return_unavailable=not available_only):
            if _player.player_id == player.player_id:
                continue
            if _player.state.type != PlayerType.GROUP:
                continue
            if powered_only and _player.state.powered is False:
                continue
            if player.player_id in _player.state.group_members:
                yield _player

    # Protocol linking methods are provided by ProtocolLinkingMixin (protocol_linking.py)

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
        prev_state = player.state.playback_state
        prev_power = player.state.powered or prev_state != PlaybackState.IDLE
        prev_synced_to = player.state.synced_to
        prev_group = (
            self.get_player(player.state.active_group) if player.state.active_group else None
        )
        prev_source = player.state.active_source
        prev_media = player.state.current_media
        prev_media_name = prev_media.title or prev_media.uri if prev_media else None
        if prev_synced_to:
            # ungroup player if its currently synced
            self.logger.debug(
                "Announcement to player %s - ungrouping player from %s...",
                player.state.name,
                prev_synced_to,
            )
            await self.cmd_ungroup(player.player_id)
        elif prev_group:
            # if the player is part of a group player, we need to ungroup it
            if PlayerFeature.SET_MEMBERS in prev_group.supported_features:
                self.logger.debug(
                    "Announcement to player %s - ungrouping from group player %s...",
                    player.state.name,
                    prev_group.display_name,
                )
                await prev_group.set_members(player_ids_to_remove=[player.player_id])
            else:
                # if the player is part of a group player that does not support ungrouping,
                # we need to power off the groupplayer instead
                self.logger.debug(
                    "Announcement to player %s - turning off group player %s...",
                    player.state.name,
                    prev_group.display_name,
                )
                await self._handle_cmd_power(player.player_id, False)
        elif prev_state in (PlaybackState.PLAYING, PlaybackState.PAUSED):
            # normal/standalone player: stop player if its currently playing
            self.logger.debug(
                "Announcement to player %s - stop existing content (%s)...",
                player.state.name,
                prev_media_name,
            )
            await self.cmd_stop(player.player_id)
            # wait for the player to stop
            await self.wait_for_state(player, PlaybackState.IDLE, 10, 0.4)
        # adjust volume if needed
        # in case of a (sync) group, we need to do this for all child players
        prev_volumes: dict[str, int] = {}
        async with TaskManager(self.mass) as tg:
            for volume_player_id in player.state.group_members or (player.player_id,):
                if not (volume_player := self.get_player(volume_player_id)):
                    continue
                # catch any players that have a different source active
                if (
                    volume_player.state.active_source
                    not in (
                        player.state.active_source,
                        volume_player.player_id,
                        None,
                    )
                    and volume_player.state.playback_state == PlaybackState.PLAYING
                ):
                    self.logger.warning(
                        "Detected announcement to playergroup %s while group member %s is playing "
                        "other content, this may lead to unexpected behavior.",
                        player.state.name,
                        volume_player.state.name,
                    )
                    tg.create_task(self.cmd_stop(volume_player.player_id))
                if volume_player.state.volume_control == PLAYER_CONTROL_NONE:
                    continue
                if (prev_volume := volume_player.state.volume_level) is None:
                    continue
                announcement_volume = self.get_announcement_volume(volume_player_id, volume_level)
                if announcement_volume is None:
                    continue
                temp_volume = announcement_volume or player.state.volume_level
                if temp_volume != prev_volume:
                    prev_volumes[volume_player_id] = prev_volume
                    self.logger.debug(
                        "Announcement to player %s - setting temporary volume (%s)...",
                        volume_player.state.name,
                        announcement_volume,
                    )
                    tg.create_task(
                        self._handle_cmd_volume_set(volume_player.player_id, announcement_volume)
                    )
        # play the announcement
        self.logger.debug(
            "Announcement to player %s - playing the announcement on the player...",
            player.state.name,
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
            "Announcement to player %s - restore previous state...", player.state.name
        )
        # restore volume
        async with TaskManager(self.mass) as tg:
            for volume_player_id, prev_volume in prev_volumes.items():
                tg.create_task(self._handle_cmd_volume_set(volume_player_id, prev_volume))
        await asyncio.sleep(0.2)
        # either power off the player or resume playing
        if not prev_power:
            if player.state.power_control != PLAYER_CONTROL_NONE:
                self.logger.debug(
                    "Announcement to player %s - turning player off again...", player.state.name
                )
                await self._handle_cmd_power(player.player_id, False)
            # nothing to do anymore, player was not previously powered
            # and does not support power control
            return
        if prev_synced_to:
            self.logger.debug(
                "Announcement to player %s - syncing back to %s...",
                player.state.name,
                prev_synced_to,
            )
            await self.cmd_set_members(prev_synced_to, player_ids_to_add=[player.player_id])
        elif prev_group:
            if PlayerFeature.SET_MEMBERS in prev_group.supported_features:
                self.logger.debug(
                    "Announcement to player %s - grouping back to group player %s...",
                    player.state.name,
                    prev_group.display_name,
                )
                await prev_group.set_members(player_ids_to_add=[player.player_id])
            elif prev_state == PlaybackState.PLAYING:
                # if the player is part of a group player that does not support set_members,
                # we need to restart the groupplayer
                self.logger.debug(
                    "Announcement to player %s - restarting playback on group player %s...",
                    player.state.name,
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
                player_playing = player.state.playback_state == PlaybackState.PLAYING
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
                        player.state.name,
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
        is_player_group = player.state.type == PlayerType.GROUP

        # handle special case for PlayerGroups: since there are no leaders,
        # DSP still always work with a single player in the group.
        multi_device_dsp_threshold = 1 if is_player_group else 0

        prev_is_multiple_devices = prev_child_count > multi_device_dsp_threshold
        new_is_multiple_devices = new_child_count > multi_device_dsp_threshold

        if prev_is_multiple_devices == new_is_multiple_devices:
            return  # no change in multi-device status

        supports_multi_device_dsp = (
            PlayerFeature.MULTI_DEVICE_DSP in player.state.supported_features
        )

        dsp_enabled: bool
        if player.state.type == PlayerType.GROUP:
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

    def _handle_external_source_takeover(
        self, player: Player, prev_source: str | None, new_source: str | None
    ) -> None:
        """
        Handle when an external source takes over playback on a player.

        When a player has an active grouped output protocol (e.g., AirPlay group) and
        an external source (e.g., Spotify Connect, TV input) takes over playback,
        we need to clear the active output protocol and ungroup the protocol players.

        This prevents the situation where the player appears grouped via protocol
        but is actually playing from a different source.

        :param player: The player whose active_source changed.
        :param prev_source: The previous active_source value.
        :param new_source: The new active_source value.
        """
        # Only relevant for non-protocol players
        if player.type == PlayerType.PROTOCOL:
            return

        # Not a takeover if the player is not actively playing
        if player.playback_state != PlaybackState.PLAYING:
            return

        # Only relevant if we have an active output protocol (not native)
        if not player.active_output_protocol or player.active_output_protocol == "native":
            return

        # Check if new source is external (not MA-managed)
        if self._is_ma_managed_source(player, new_source):
            return

        # Get the active protocol player
        protocol_player = self.get_player(player.active_output_protocol)
        if not protocol_player:
            return

        # If the source matches the active protocol's domain, it's expected - not a takeover
        # e.g., source "airplay" when using AirPlay protocol is normal
        if new_source and new_source.lower() == protocol_player.provider.domain.lower():
            return

        # Confirmed external source takeover
        self.logger.info(
            "External source '%s' took over on %s while grouped via protocol %s - "
            "clearing active output protocol and ungrouping",
            new_source,
            player.display_name,
            protocol_player.provider.domain,
        )

        # Clear active output protocol
        player.set_active_output_protocol(None)

        # Ungroup the protocol player (async task)
        self.mass.create_task(protocol_player.ungroup())

    def _is_ma_managed_source(self, player: Player, source: str | None) -> bool:
        """
        Check if a source is managed by Music Assistant.

        MA-managed sources include:
        - None (=autodetect, no source explicitly set by player)
        - The player's own ID (MA queue)
        - Any active queue ID
        - Any plugin source ID

        :param player: The player to check.
        :param source: The source ID to check.
        :return: True if the source is MA-managed, False if external.
        """
        if source is None:
            return True

        # Player's own ID means MA queue is active
        if source == player.player_id:
            return True

        # Check if it's a known queue ID
        if self.mass.player_queues.get(source):
            return True

        # Check if it's a plugin source
        return any(plugin_source.id == source for plugin_source in self.get_plugin_sources())

    def _schedule_update_all_players(self, delay: float = 2.0) -> None:
        """
        Schedule a debounced update of all players' state.

        Used when a new player is registered to ensure all existing players
        update their dynamic properties (like can_group_with) that may have changed.

        :param delay: Delay in seconds before triggering updates (default 2.0).
        """
        if self.mass.closing:
            return

        async def _update_all_players() -> None:
            if self.mass.closing:
                return

            for player in self.all_players(
                return_unavailable=True,
                return_disabled=False,
                return_protocol_players=True,
            ):
                # Use call_soon to schedule updates without blocking
                # This spreads the updates across event loop iterations
                self.mass.loop.call_soon(player.update_state)

        # Use mass.call_later with task_id for automatic debouncing
        # Each call resets the timer, so rapid registrations only trigger one update
        task_id = "update_all_players_on_registration"
        self.mass.call_later(delay, _update_all_players, task_id=task_id)

    async def _handle_set_members_with_protocols(
        self,
        parent_player: Player,
        player_ids_to_add: list[str],
        player_ids_to_remove: list[str],
    ) -> None:
        """
        Handle set_members considering protocol and native members.

        Translates visible player IDs to protocol player IDs when appropriate,
        and forwards to the correct player's set_members.

        :param parent_player: The parent player to add/remove members to/from.
        :param player_ids_to_add: List of visible player IDs to add as members.
        :param player_ids_to_remove: List of visible player IDs to remove from members.
        """
        # Get parent's active protocol domain and player if available
        parent_protocol_domain = None
        parent_protocol_player = None
        if (
            parent_player.active_output_protocol
            and parent_player.active_output_protocol != "native"
        ):
            parent_protocol_player = self.get_player(parent_player.active_output_protocol)
            if parent_protocol_player:
                parent_protocol_domain = parent_protocol_player.provider.domain

        self.logger.debug(
            "set_members on %s: active_protocol=%s, adding=%s, removing=%s",
            parent_player.state.name,
            parent_protocol_domain or "none",
            player_ids_to_add,
            player_ids_to_remove,
        )

        # Translate members to add
        (
            protocol_members_to_add,
            native_members_to_add,
            parent_protocol_player,
            parent_protocol_domain,
        ) = self._translate_members_for_protocols(
            parent_player, player_ids_to_add, parent_protocol_player, parent_protocol_domain
        )

        self.logger.debug(
            "Translated members: protocol=%s (domain=%s), native=%s",
            protocol_members_to_add,
            parent_protocol_domain,
            native_members_to_add,
        )

        # Translate members to remove
        protocol_members_to_remove, native_members_to_remove = (
            self._translate_members_to_remove_for_protocols(
                parent_player, player_ids_to_remove, parent_protocol_player, parent_protocol_domain
            )
        )

        # Forward protocol members to protocol player's set_members
        if (protocol_members_to_add or protocol_members_to_remove) and parent_protocol_player:
            await self._forward_protocol_set_members(
                parent_player,
                parent_protocol_player,
                protocol_members_to_add,
                protocol_members_to_remove,
            )

        # Forward native members to parent player's set_members
        if native_members_to_add or native_members_to_remove:
            filtered_native_add = self._filter_native_members(native_members_to_add, parent_player)
            # For removal, allow protocol players if they're actually in the parent's group_members
            # This handles native protocol players (e.g., native AirPlay) where group_members
            # contains protocol player IDs
            filtered_native_remove = [
                pid
                for pid in native_members_to_remove
                if (p := self.get_player(pid))
                and (p.type != PlayerType.PROTOCOL or pid in parent_player.group_members)
            ]
            self.logger.debug(
                "Native grouping on %s: filtered_add=%s, filtered_remove=%s",
                parent_player.state.name,
                filtered_native_add,
                filtered_native_remove,
            )
            if filtered_native_add or filtered_native_remove:
                self.logger.info(
                    "Calling set_members on native player %s with add=%s, remove=%s",
                    parent_player.state.name,
                    filtered_native_add,
                    filtered_native_remove,
                )
                await parent_player.set_members(
                    player_ids_to_add=filtered_native_add or None,
                    player_ids_to_remove=filtered_native_remove or None,
                )

    # Private command handlers (no permission checks)

    async def _handle_cmd_resume(
        self, player_id: str, source: str | None = None, media: PlayerMedia | None = None
    ) -> None:
        """
        Handle resume playback command.

        Skips the permission checks (internal use only).
        """
        player = self._get_player_with_redirect(player_id)
        source = source or player.state.active_source
        media = media or player.state.current_media
        # power on the player if needed
        if not player.state.powered and player.state.power_control != PLAYER_CONTROL_NONE:
            await self._handle_cmd_power(player.player_id, True)
        # Redirect to queue controller if it is active
        if active_queue := self.mass.player_queues.get(source or player_id):
            await self.mass.player_queues.resume(active_queue.queue_id)
            return
        # try to handle command on player directly
        # TODO: check if player has an active source with native resume support
        active_source = next((x for x in player.state.source_list if x.id == source), None)
        if (
            player.state.playback_state in (PlaybackState.IDLE, PlaybackState.PAUSED)
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
        player = self.get_player(player_id, True)
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
        player_was_synced = player.state.synced_to is not None
        if player.type == PlayerType.PLAYER and not powered:
            # ungroup player if it is synced (or is a sync leader itself)
            # NOTE: ungroup will be ignored if the player is not grouped or synced
            await self.cmd_ungroup(player_id)

        # always stop player at power off
        if (
            not powered
            and not player_was_synced
            and player_state.playback_state in (PlaybackState.PLAYING, PlaybackState.PAUSED)
        ):
            await self.cmd_stop(player_id)
            # short sleep: allow the stop command to process and prevent race conditions
            await asyncio.sleep(0.2)

        # power off all synced childs when player is a sync leader
        elif not powered and player_state.type == PlayerType.PLAYER and player_state.group_members:
            async with TaskManager(self.mass) as tg:
                for member in self.iter_group_members(player, True):
                    if member.power_control == PLAYER_CONTROL_NONE:
                        continue
                    tg.create_task(self._handle_cmd_power(member.player_id, False))

        # handle actual power command
        if player_state.power_control == PLAYER_CONTROL_NONE:
            self.logger.debug(
                "Player %s does not support power control, ignoring power command",
                player_state.name,
            )
            return
        if player_state.power_control == PLAYER_CONTROL_NATIVE:
            # player supports power command natively: forward to player provider
            await player.power(powered)
        elif player_state.power_control == PLAYER_CONTROL_FAKE:
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
            player_control = self._controls.get(player.state.power_control)
            control_name = player_control.name if player_control else player.state.power_control
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
            not player_state.active_group
            and not player_state.synced_to
            and powered
            and player.config.get_value(CONF_AUTO_PLAY)
            and player_state.active_source in (None, player_id)
            and not player.extra_data.get(ATTR_ANNOUNCEMENT_IN_PROGRESS)
        ):
            await self.mass.player_queues.resume(player_id)

    async def _handle_cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """
        Handle Player volume set command.

        Skips the permission checks (internal use only).
        """
        player = self.get_player(player_id, True)
        assert player is not None  # for type checker
        if player.type == PlayerType.GROUP:
            # redirect to special group volume control
            await self.cmd_group_volume(player_id, volume_level)
            return

        # Check if player has mute lock (set when individually muted in a group)
        # If locked, don't auto-unmute when volume changes
        has_mute_lock = player.extra_data.get(ATTR_MUTE_LOCK, False)
        if (
            not has_mute_lock
            # use player.state here to get accumulated mute control from any linked protocol players
            and player.state.mute_control not in (PLAYER_CONTROL_NONE, PLAYER_CONTROL_FAKE)
            and player.state.volume_muted
        ):
            # if player is muted and not locked, we unmute it first
            # skip this for fake mute since it uses volume to simulate mute
            self.logger.debug(
                "Unmuting player %s before setting volume",
                player.state.name,
            )
            await self.cmd_volume_mute(player_id, False)

        # Check if a plugin source is active with a volume callback
        if plugin_source := self._get_active_plugin_source(player):
            if plugin_source.on_volume:
                await plugin_source.on_volume(volume_level)
        # Handle native volume control support
        if player.volume_control == PLAYER_CONTROL_NATIVE:
            # player supports volume command natively: forward to player
            await player.volume_set(volume_level)
            return
        # Handle fake volume control support
        if player.volume_control == PLAYER_CONTROL_FAKE:
            # user wants to use fake volume control - so we (optimistically) update the state
            # and store the state in the cache
            player.extra_data[ATTR_FAKE_VOLUME] = volume_level
            # trigger update
            player.update_state()
            return
        # player has no volume support at all
        if player.volume_control == PLAYER_CONTROL_NONE:
            raise UnsupportedFeaturedException(
                f"Player {player.state.name} does not support volume control"
            )
        # handle external player control
        if player_control := self._controls.get(player.state.volume_control):
            control_name = player_control.name if player_control else player.state.volume_control
            self.logger.debug("Redirecting volume command to PlayerControl %s", control_name)
            if not player_control or not player_control.supports_volume:
                raise UnsupportedFeaturedException(
                    f"Player control {control_name} is not available"
                )
            assert player_control.volume_set is not None
            await player_control.volume_set(volume_level)
            return
        if protocol_player := self.get_player(player.state.volume_control):
            # redirect to protocol player volume control
            self.logger.debug(
                "Redirecting volume command to protocol player %s",
                protocol_player.provider.manifest.name,
            )
            await self._handle_cmd_volume_set(protocol_player.player_id, volume_level)
            return

    async def _handle_play_media(self, player_id: str, media: PlayerMedia) -> None:
        """
        Handle play media command without group redirect.

        Skips permission checks and all redirect logic (internal use only).

        :param player_id: player_id of the player to handle the command.
        :param media: The Media that needs to be played on the player.
        """
        player = self.get_player(player_id, raise_unavailable=True)
        assert player is not None
        # set active source if media has a source_id (e.g. plugin source or mass queue source)
        if media.source_id:
            player.set_active_mass_source(media.source_id)

        # Select best output protocol for playback
        target_player, output_protocol = self._select_best_output_protocol(player)

        if target_player.player_id != player.player_id:
            # Playing via linked protocol - update active output protocol
            # output_protocol is guaranteed to be non-None when target_player != player
            assert output_protocol is not None
            self.logger.debug(
                "Starting playback on %s via protocol %s (target=%s), group_members=%s",
                player.state.name,
                output_protocol.output_protocol_id,
                target_player.display_name,
                target_player.state.group_members,
            )
            player.set_active_output_protocol(output_protocol.output_protocol_id)
            # if the (protocol)player has power control and is currently powered off,
            # we need to power it on before playback
            if (
                target_player.state.powered is False
                and target_player.power_control != PLAYER_CONTROL_NONE
            ):
                await self._handle_cmd_power(target_player.player_id, True)
            # forward play media command to protocol player
            await target_player.play_media(media)
            # notify the native player that protocol playback started
            await player.on_protocol_playback(output_protocol=output_protocol)
        else:
            # Native playback
            self.logger.debug(
                "Starting playback on %s via native, group_members=%s",
                player.state.name,
                player.state.group_members,
            )
            player.set_active_output_protocol("native")
            await player.play_media(media)

    async def _handle_enqueue_next_media(self, player_id: str, media: PlayerMedia) -> None:
        """
        Handle enqueue next media command without group redirect.

        Skips permission checks and all redirect logic (internal use only).

        :param player_id: player_id of the player to handle the command.
        :param media: The Media that needs to be enqueued on the player.
        """
        player = self.get_player(player_id, raise_unavailable=True)
        assert player is not None
        if target_player := self._get_control_target(
            player,
            required_feature=PlayerFeature.ENQUEUE,
            require_active=True,
        ):
            self.logger.debug(
                "Redirecting enqueue command to protocol player %s",
                target_player.provider.manifest.name,
            )
            await target_player.enqueue_next_media(media)
            return

        if PlayerFeature.ENQUEUE not in player.state.supported_features:
            raise UnsupportedFeaturedException(
                f"Player {player.state.name} does not support enqueueing"
            )
        await player.enqueue_next_media(media)

    async def _handle_select_source(self, player_id: str, source: str | None) -> None:
        """
        Handle select source command without group redirect.

        Skips permission checks and all redirect logic (internal use only).

        :param player_id: player_id of the player to handle the command.
        :param source: The ID of the source that needs to be activated/selected.
        """
        if source is None:
            source = player_id  # default to MA queue source
        player = self.get_player(player_id, True)
        assert player is not None
        # check if player is already playing and source is different
        # in that case we need to stop the player first
        prev_source = player.state.active_source
        if prev_source and source != prev_source:
            with suppress(PlayerCommandFailed, RuntimeError):
                # just try to stop (regardless of state)
                await self._handle_cmd_stop(player_id)
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
        if PlayerFeature.SELECT_SOURCE not in player.state.supported_features:
            raise UnsupportedFeaturedException(
                f"Player {player.state.name} does not support source selection"
            )
        # basic check if source is valid for player
        if not any(x for x in player.state.source_list if x.id == source):
            raise PlayerCommandFailed(
                f"{source} is an invalid source for player {player.state.name}"
            )
        # forward to player
        await player.select_source(source)

    async def _handle_cmd_stop(self, player_id: str) -> None:
        """
        Handle stop command without any redirects.

        Skips permission checks and all redirect logic (internal use only).

        :param player_id: player_id of the player to handle the command.
        """
        player = self.get_player(player_id, raise_unavailable=True)
        assert player is not None
        player.mark_stop_called()
        # Delegate to active protocol player if one is active
        target_player = player
        if (
            player.active_output_protocol
            and player.active_output_protocol != "native"
            and (protocol_player := self.get_player(player.active_output_protocol))
        ):
            target_player = protocol_player
            if PlayerFeature.POWER in target_player.supported_features:
                # if protocol player supports/requires power,
                # we power it off instead of just stopping (which also stops playback)
                await self._handle_cmd_power(target_player.player_id, False)
                return

        # handle command on player(protocol) directly
        await target_player.stop()

    async def _handle_cmd_play(self, player_id: str) -> None:
        """
        Handle play command without group redirect.

        Skips permission checks and all redirect logic (internal use only).

        :param player_id: player_id of the player to handle the command.
        """
        player = self.get_player(player_id, raise_unavailable=True)
        assert player is not None
        if player.state.playback_state == PlaybackState.PLAYING:
            self.logger.info(
                "Ignore PLAY request to player %s: player is already playing", player.state.name
            )
            return
        # Check if a plugin source is active with a play callback
        if plugin_source := self._get_active_plugin_source(player):
            if plugin_source.can_play_pause and plugin_source.on_play:
                await plugin_source.on_play()
                return
        # handle unpause (=play if player is paused)
        if player.state.playback_state == PlaybackState.PAUSED:
            active_source = next(
                (x for x in player.state.source_list if x.id == player.state.active_source), None
            )
            # raise if active source does not support play/pause
            if active_source and not active_source.can_play_pause:
                msg = (
                    f"The active source ({active_source.name}) on player "
                    f"{player.state.name} does not support play/pause"
                )
                raise PlayerCommandFailed(msg)
            # Delegate to active protocol player if one is active
            if target_player := self._get_control_target(
                player, PlayerFeature.PAUSE, require_active=True
            ):
                await target_player.play()
                return

        # player is not paused: try to resume the player
        # Note: We handle resume inline here without calling _handle_cmd_resume
        active_source = next(
            (x for x in player.state.source_list if x.id == player.state.active_source), None
        )
        media = player.state.current_media
        # power on the player if needed
        if not player.state.powered and player.state.power_control != PLAYER_CONTROL_NONE:
            await self._handle_cmd_power(player.player_id, True)
        if active_source and not active_source.passive:
            await self._handle_select_source(player_id, active_source.id)
            return
        if media:
            # try to re-play the current media item
            await player.play_media(media)
            return
        # fallback: just send play command - which will fail if nothing can be played
        await player.play()

    async def _handle_cmd_pause(self, player_id: str) -> None:
        """
        Handle pause command without any redirects.

        Skips permission checks and all redirect logic (internal use only).

        :param player_id: player_id of the player to handle the command.
        """
        player = self.get_player(player_id, raise_unavailable=True)
        assert player is not None
        # Check if a plugin source is active with a pause callback
        if plugin_source := self._get_active_plugin_source(player):
            if plugin_source.can_play_pause and plugin_source.on_pause:
                await plugin_source.on_pause()
                return
        # handle command on player/source directly
        active_source = next(
            (x for x in player.state.source_list if x.id == player.state.active_source), None
        )
        if active_source and not active_source.can_play_pause:
            # raise if active source does not support play/pause
            msg = (
                f"The active source ({active_source.name}) on player "
                f"{player.state.name} does not support play/pause"
            )
            raise PlayerCommandFailed(msg)
        # Delegate to active protocol player if one is active
        if not (
            target_player := self._get_control_target(
                player, PlayerFeature.PAUSE, require_active=True
            )
        ):
            # if player(protocol) does not support pause, we need to send stop
            self.logger.debug(
                "Player/protocol %s does not support pause, using STOP instead",
                player.state.name,
            )
            await self._handle_cmd_stop(player.player_id)
            return
        # handle command on player(protocol) directly
        await target_player.pause()

    def __iter__(self) -> Iterator[Player]:
        """Iterate over all players."""
        return iter(self._players.values())
