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
import contextlib
import time
import weakref
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.auth import Scope
from music_assistant_models.background_task import TaskSchedule
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.constants import (
    PLAYER_CONTROL_FAKE,
    PLAYER_CONTROL_NATIVE,
    PLAYER_CONTROL_NONE,
)
from music_assistant_models.enums import (
    ConfigEntryType,
    EventType,
    IdentifierType,
    MediaType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
    ProviderFeature,
    ProviderType,
    RepeatMode,
    SourceControl,
)
from music_assistant_models.errors import (
    AlreadyRegisteredError,
    InsufficientPermissions,
    InvalidCommand,
    InvalidDataError,
    MusicAssistantError,
    PlayerCommandFailed,
    PlayerUnavailableError,
    ProviderUnavailableError,
    UnsupportedFeaturedException,
)
from music_assistant_models.media_items import AudioSource
from music_assistant_models.player import PlayerOptionValueType  # noqa: TC002
from music_assistant_models.player_control import PlayerControl  # noqa: TC002

from music_assistant.constants import (
    ATTR_ACTIVE_SOURCE,
    ATTR_ANNOUNCEMENT_IN_PROGRESS,
    ATTR_AVAILABLE,
    ATTR_ENABLED,
    ATTR_FAKE_MUTE,
    ATTR_FAKE_POWER,
    ATTR_FAKE_VOLUME,
    ATTR_GROUP_MEMBERS,
    ATTR_GROUP_VOLUME_SNAPSHOT,
    ATTR_LAST_POLL,
    ATTR_MUTE_CONTROL,
    ATTR_MUTE_LOCK,
    ATTR_POWER_CONTROL,
    ATTR_POWERED,
    ATTR_PREVIOUS_VOLUME,
    ATTR_SUPPORTED_FEATURES,
    ATTR_VOLUME_CONTROL,
    ATTR_VOLUME_TARGET,
    CONF_ANNOUNCE_TTS_ENGINE,
    CONF_AUTO_PLAY,
    CONF_CACHED_ARP_MAC,
    CONF_ENTRY_MAX_VOLUME,
    CONF_ENTRY_MIN_VOLUME,
    CONF_GROUP_MEMBERS,
    CONF_MAX_VOLUME,
    CONF_MIN_VOLUME,
    CONF_MUTE_CONTROL,
    CONF_PLAY_MEDIA_OVERRIDES_GROUP,
    CONF_PLAYER_DSP,
    CONF_PLAYER_QUEUES,
    CONF_PLAYERS,
    CONF_POWER_CONTROL,
    CONF_PROTOCOL_PARENT_ID,
    CONF_REPORTED_MAC,
    CONF_VOLUME_CONTROL,
    CONF_VOLUME_STEP,
    VERBOSE_LOG_LEVEL,
)
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    get_current_user,
    get_sendspin_player_id,
    has_scope,
)
from music_assistant.helpers.api import api_command
from music_assistant.helpers.colors import get_palette_for_url
from music_assistant.helpers.plugin_engines import create_tts_engine_config_entries
from music_assistant.helpers.util import (
    TaskManager,
    enrich_device_mac_address,
    is_valid_mac_address,
)
from music_assistant.models.core_controller import CoreController
from music_assistant.models.player import Player, PlayerMedia, PlayerState
from music_assistant.models.player_provider import PlayerProvider
from music_assistant.models.plugin import PluginProvider, SourceControlValue

from .announcements import AnnouncementsMixin
from .audio_sources import AudioSourceMixin, AudioSourceSession
from .constants import PlayerLockPurpose
from .helpers import handle_player_command, wait_for_power_on
from .protocol_linking import ProtocolLinkingMixin

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from music_assistant_models.config_entries import (
        CoreConfig,
        PlayerConfig,
    )
    from music_assistant_models.player import OutputProtocol
    from music_assistant_models.player_queue import PlayerQueue

    from music_assistant import MusicAssistant
    from music_assistant.helpers.json import SerializableType

CACHE_CATEGORY_PLAYER_POWER = 1

# state keys that carry the current_media playback-position anchor; these only
# change on discrete position events (play/pause/seek/track change/buffer correction)
POSITION_ANCHOR_KEYS = frozenset(
    {
        "current_media.elapsed_time",
        "current_media.elapsed_time_last_updated",
    }
)

# How long the volume level of the last command outranks the level the player reports.
# Long enough to cover a burst of volume nudges on a player that only reports its volume
# back some time later, short enough for a change made on the device itself to win again.
VOLUME_TARGET_EXPIRY = 2.0

# How long a freshly started source session may wait for its first stream request
# before it is considered never started and released.
AUDIO_SOURCE_CLAIM_TIMEOUT = 30

# Sentinel used to detect omitted optional arguments where ``None`` is a valid value.
_SENTINEL: Any = object()


class PlayerController(AnnouncementsMixin, AudioSourceMixin, ProtocolLinkingMixin, CoreController):
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
        self._player_command_locks: dict[str, asyncio.Lock] = {}
        # Re-entrancy tracking for get_player_lock, keyed on the task object
        # (weak ref auto-clears entries if a task is GC'd before its finally runs).
        self._task_held_locks: weakref.WeakKeyDictionary[asyncio.Task[Any], set[str]] = (
            weakref.WeakKeyDictionary()
        )
        # Lock to prevent race conditions during player registration
        self._register_lock = asyncio.Lock()
        # Track pending protocol player evaluations (delayed to allow all protocols to register)
        self._pending_protocol_evaluations: dict[str, asyncio.TimerHandle] = {}
        # Serialize delayed evaluations to prevent race conditions
        self._delayed_evaluation_lock = asyncio.Lock()
        # Live external AudioSource playing on a player, keyed on player_id
        self._source_sessions: dict[str, AudioSourceSession] = {}
        # Subscribers for player state updates (called with player + changed_values)
        self._state_update_subscribers: list[
            Callable[[Player, dict[str, tuple[Any, Any]]], None]
        ] = []

    @contextlib.asynccontextmanager
    async def get_player_lock(
        self, player_id: str, purpose: PlayerLockPurpose = PlayerLockPurpose.PLAYBACK
    ) -> AsyncIterator[None]:
        """
        Acquire a purpose-scoped lock for a player, with re-entrant support.

        Tracks lock ownership per asyncio Task so that nested calls within the same
        task skip re-acquisition (preventing deadlocks), while deferred callbacks
        (call_later / create_task) correctly acquire a fresh lock.

        If the lock can't be acquired within 30s the body runs anyway, to keep
        the player responsive when a previous holder is stuck on a hung command.

        :param player_id: The player to lock.
        :param purpose: Lock category. Commands with different purposes can run
            concurrently on the same player.
        """
        lock_key = f"{purpose.value}_{player_id}"
        task = asyncio.current_task()

        if task is not None and lock_key in self._task_held_locks.get(task, set()):
            yield
            return

        lock = self._player_command_locks.setdefault(lock_key, asyncio.Lock())
        # Two-stage acquire: a slow-acquire log at 5s and a hard give-up at 30s.
        # If the previous holder is stuck (e.g. on a dead provider socket), we
        # proceed without the lock so this player stays responsive.
        acquired = False
        try:
            async with asyncio.timeout(5):
                await lock.acquire()
            acquired = True
        except TimeoutError:
            self.logger.debug(
                "Acquiring %s lock for player %s is slow (>5s)", purpose.value, player_id
            )
            try:
                async with asyncio.timeout(25):
                    await lock.acquire()
                acquired = True
            except TimeoutError:
                self.logger.warning(
                    "Timed out (30s) acquiring %s lock for player %s — "
                    "previous holder appears stuck; proceeding without lock",
                    purpose.value,
                    player_id,
                )

        if acquired and task is not None:
            self._task_held_locks.setdefault(task, set()).add(lock_key)
        try:
            yield
        finally:
            if acquired:
                if task is not None and (held := self._task_held_locks.get(task)) is not None:
                    held.discard(lock_key)
                    if not held:
                        del self._task_held_locks[task]
                lock.release()

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config Entries for the Player Controller."""
        return (
            ConfigEntry(
                key=CONF_VOLUME_STEP,
                type=ConfigEntryType.INTEGER,
                default_value=0,
                range=(0, 10),
                required=False,
                category="generic",
            ),
            *await create_tts_engine_config_entries(
                self.mass, CONF_ANNOUNCE_TTS_ENGINE, category="announcements"
            ),
        )

    async def setup(self, config: CoreConfig) -> None:
        """Async initialize of module."""
        self._repair_protocol_parent_links()
        self._poll_task = self.mass.create_task(self._poll_players())
        self.mass.tasks.register_scheduled_task(
            task_id="fix_group_member_configs",
            name="Fix sync group member configurations",
            handler=self._fix_group_member_configs,
            schedule=TaskSchedule.weekly(
                days_of_week=[0],
                hour=4,
                minute=0,
            ),
            initial_delay=300,
        )

    async def close(self) -> None:
        """Cleanup on exit."""
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
        # Cancel all pending protocol evaluations
        for handle in self._pending_protocol_evaluations.values():
            handle.cancel()
        self._pending_protocol_evaluations.clear()
        for player in self._players.values():
            if player.sleep_timer_expires_at is not None:
                self.mass.cancel_timer(self._sleep_timer_task_id(player.player_id))

    async def get_diagnostics(self) -> dict[str, SerializableType]:
        """Return diagnostics info for this controller to include in diagnostics reports."""
        players = list(self._players.values())
        return {
            "players_synced": sum(player.state.synced_to is not None for player in players),
            "players_with_active_group": sum(
                player.state.active_group is not None for player in players
            ),
            "announcements_in_progress": sum(
                bool(player.extra_data.get(ATTR_ANNOUNCEMENT_IN_PROGRESS)) for player in players
            ),
            "pending_protocol_evaluations": len(self._pending_protocol_evaluations),
        }

    async def on_provider_loaded(self, provider: PlayerProvider) -> None:
        """Handle logic when a provider is loaded."""

    async def on_provider_unload(self, provider: PlayerProvider) -> None:
        """Handle logic when a provider is (about to get) unloaded."""

    @property
    def providers(self) -> list[PlayerProvider]:
        """Return all loaded/running MusicProviders."""
        return cast("list[PlayerProvider]", self.mass.get_providers(ProviderType.PLAYER))

    def iter_players(
        self,
        return_unavailable: bool = True,
        return_disabled: bool = False,
        provider_filter: str | None = None,
        return_protocol_players: bool = False,
    ) -> Iterator[Player]:
        """
        Iterate over all registered players, regardless of who is asking.

        Use this for internal logic - state derivation, bookkeeping and topology
        lookups - which must stay correct no matter which user's command happened
        to trigger it. Use :meth:`all_players` for anything presented to a user.

        :param return_unavailable [bool]: Include unavailable players.
        :param return_disabled [bool]: Include disabled players.
        :param provider_filter [str]: Optional filter by provider lookup key.
        :param return_protocol_players [bool]: Include protocol players (hidden by default).
        """
        for player in list(self._players.values()):
            if not (player.state.available or return_unavailable):
                continue
            if not (player.state.enabled or return_disabled):
                continue
            if not player.initialized.is_set():
                continue
            if provider_filter is not None and player.provider.instance_id != provider_filter:
                continue
            if not return_protocol_players and player.state.type == PlayerType.PROTOCOL:
                continue
            yield player

    def all_players(
        self,
        return_unavailable: bool = True,
        return_disabled: bool = False,
        provider_filter: str | None = None,
        return_protocol_players: bool = False,
    ) -> list[Player]:
        """
        Return the registered players the current user is allowed to see.

        Note that this applies user filters for players (for non admin users),
        which makes it unsuitable for internal logic - use :meth:`iter_players` there.

        :param return_unavailable [bool]: Include unavailable players.
        :param return_disabled [bool]: Include disabled players.
        :param provider_filter [str]: Optional filter by provider lookup key.
        :param return_protocol_players [bool]: Include protocol players (hidden by default).

        :return: List of Player objects.
        """
        current_user = get_current_user()
        user_filter = (
            current_user.player_filter
            if current_user and not has_scope(current_user, Scope.ALL)
            else None
        )
        current_sendspin_player = get_sendspin_player_id()
        return [
            player
            for player in self.iter_players(
                return_unavailable=return_unavailable,
                return_disabled=return_disabled,
                provider_filter=provider_filter,
                return_protocol_players=return_protocol_players,
            )
            if not user_filter
            or player.player_id in user_filter
            or player.player_id == current_sendspin_player
        ]

    @api_command("players/all", required_scope=Scope.PLAYERS_READ)
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

    @api_command("players/get", required_scope=Scope.PLAYERS_READ)
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
            if current_user and not has_scope(current_user, Scope.ALL)
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

    @api_command("players/get_by_name", required_scope=Scope.PLAYERS_READ)
    def get_player_state_by_name(self, name: str) -> PlayerState | None:
        """
        Return PlayerState by name.

        :param name: Name of the player.
        :return: PlayerState object or None.
        """
        current_user = get_current_user()
        user_filter = (
            current_user.player_filter
            if current_user and not has_scope(current_user, Scope.ALL)
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

    @api_command("players/player_controls", required_scope=Scope.PLAYERS_READ)
    def player_controls(
        self,
    ) -> list[PlayerControl]:
        """Return all registered playercontrols."""
        return list(self._controls.values())

    @api_command("players/player_control", required_scope=Scope.PLAYERS_READ)
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

    @api_command("players/sleep_timer/get", required_scope=Scope.PLAYERS_READ)
    def get_sleep_timer(self, player_id: str) -> float | None:
        """
        Return the active sleep timer expiry timestamp for the player.

        :param player_id: Player ID to check.
        """
        player = self._get_player_with_redirect(player_id)
        return player.sleep_timer_expires_at

    @api_command("players/sleep_timer/set", required_scope=Scope.PLAYERS_CONTROL)
    def set_sleep_timer(self, player_id: str, seconds: int) -> float:
        """
        Set a sleep timer for the player.

        :param player_id: Player ID to set the timer for.
        :param seconds: Delay in seconds before playback is stopped.
        """
        if seconds <= 0:
            msg = "Sleep timer duration must be greater than zero seconds"
            raise InvalidDataError(msg)
        player = self._get_player_with_redirect(player_id)
        try:
            # guard against absurd durations that overflow the float timestamp math
            expires_at = time.time() + seconds
        except OverflowError:
            msg = "Sleep timer duration is too large to schedule"
            raise InvalidDataError(msg) from None
        player.set_sleep_timer_expires_at(expires_at)
        player.update_state()
        self._signal_sleep_timer_updated(player, expires_at)
        self.mass.call_later(
            seconds,
            self._handle_sleep_timer_expired,
            player.player_id,
            task_id=self._sleep_timer_task_id(player.player_id),
        )
        return expires_at

    @api_command("players/sleep_timer/clear", required_scope=Scope.PLAYERS_CONTROL)
    def clear_sleep_timer(self, player_id: str) -> None:
        """
        Clear the active sleep timer for the player.

        :param player_id: Player ID to clear the timer for.
        """
        player = self._get_player_with_redirect(player_id)
        self._clear_sleep_timer(player)

    # Player commands

    @api_command("players/cmd/stop", required_scope=Scope.PLAYERS_CONTROL)
    @handle_player_command
    async def cmd_stop(self, player_id: str) -> None:
        """
        Send STOP command to given player.

        - player_id: player_id of the player to handle the command.
        """
        player = self._get_player_with_redirect(player_id)
        async with self.get_player_lock(player.player_id, PlayerLockPurpose.PLAYBACK):
            # Redirect to queue controller if it is active (skip if already in queue command context)
            if active_queue := self.get_active_queue(player):
                await self.mass.player_queues.stop(active_queue.queue_id)
                return
            # Delegate to internal handler for actual implementation
            await self._handle_cmd_stop(player.player_id)

    @api_command("players/cmd/play", required_scope=Scope.PLAYERS_CONTROL)
    @handle_player_command
    async def cmd_play(self, player_id: str) -> None:
        """
        Send PLAY (unpause) command to given player.

        - player_id: player_id of the player to handle the command.
        """
        player = self._get_player_with_redirect(player_id)
        async with self.get_player_lock(player.player_id, PlayerLockPurpose.PLAYBACK):
            if player.state.playback_state == PlaybackState.PLAYING:
                self.logger.info(
                    "Ignore PLAY request to player %s: player is already playing",
                    player.state.name,
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

    @api_command("players/cmd/pause", required_scope=Scope.PLAYERS_CONTROL)
    @handle_player_command
    async def cmd_pause(self, player_id: str) -> None:
        """
        Send PAUSE command to given player.

        - player_id: player_id of the player to handle the command.
        """
        player = self._get_player_with_redirect(player_id)
        # Redirect to queue controller if it is active (skip if already in queue command context)
        if active_queue := self.get_active_queue(player):
            await self.mass.player_queues.pause(active_queue.queue_id)
            return
        # Delegate to internal handler for actual implementation
        await self._handle_cmd_pause(player.player_id)

    @api_command("players/cmd/play_pause", required_scope=Scope.PLAYERS_CONTROL)
    async def cmd_play_pause(self, player_id: str) -> None:
        """
        Toggle play/pause on given player.

        - player_id: player_id of the player to handle the command.
        """
        player = self._get_player_with_redirect(player_id)
        if player.state.playback_state == PlaybackState.PLAYING:
            await self.cmd_pause(player.player_id)
        else:
            await self.cmd_play(player.player_id)

    @api_command("players/cmd/resume", required_scope=Scope.PLAYERS_CONTROL)
    @handle_player_command
    async def cmd_resume(
        self, player_id: str, source: str | None = None, media: PlayerMedia | None = None
    ) -> None:
        """
        Send RESUME command to given player.

        Resume (or restart) playback on the player.

        :param player_id: player_id of the player to handle the command.
        :param source: Optional source to resume.
        :param media: Optional media to resume.
        """
        player = self._get_player_with_redirect(player_id)
        async with self.get_player_lock(player.player_id, PlayerLockPurpose.PLAYBACK):
            await self._handle_cmd_resume(player.player_id, source, media)

    @api_command("players/cmd/seek", required_scope=Scope.PLAYERS_CONTROL)
    @handle_player_command
    async def cmd_seek(self, player_id: str, position: int) -> None:
        """
        Handle SEEK command for given player.

        - player_id: player_id of the player to handle the command.
        - position: position in seconds to seek to in the current playing item.
        """
        player = self._get_player_with_redirect(player_id)
        if await self._forward_to_external_source(player, SourceControl.SEEK, position):
            return
        # Redirect to queue controller if it is active
        if active_queue := self.get_active_queue(player):
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

    @api_command("players/cmd/shuffle", required_scope=Scope.PLAYERS_CONTROL)
    @handle_player_command
    async def cmd_shuffle(
        self, player_id: str, shuffle_enabled: bool, source_id: str | None = None
    ) -> None:
        """
        Handle SHUFFLE command for given player.

        Applies to whatever the player is playing: a live external source orders its
        own session, a source the device runs itself orders its own content, and
        Music Assistant's queue orders its own items.

        :param player_id: player_id of the player to handle the command.
        :param shuffle_enabled: Whether to play the current content shuffled.
        :param source_id: Optional source (id) the command is aimed at, as listed in the
            player's source_list. Given one, the command is refused when that source is
            no longer playing, so it can never land on whatever took the player since.
        """
        player = self._get_player_with_redirect(player_id)
        active_source_id = self._resolve_command_target(player, source_id)
        if await self._forward_to_external_source(player, SourceControl.SHUFFLE, shuffle_enabled):
            return
        if active_queue := self.get_active_queue(player):
            await self.mass.player_queues.set_shuffle(active_queue.queue_id, shuffle_enabled)
            return
        if active_source := next(
            (x for x in player.state.source_list if x.id == active_source_id), None
        ):
            # the source belongs to the player itself (its own Spotify Connect, a device input)
            if not active_source.can_shuffle:
                msg = "This action is (currently) unavailable for this source."
                raise PlayerCommandFailed(msg)
            await player.set_shuffle(shuffle_enabled)
            return
        msg = f"There is nothing playing on {player.state.name} to shuffle."
        raise PlayerCommandFailed(msg)

    @api_command("players/cmd/repeat", required_scope=Scope.PLAYERS_CONTROL)
    @handle_player_command
    async def cmd_repeat(
        self, player_id: str, repeat_mode: RepeatMode, source_id: str | None = None
    ) -> None:
        """
        Handle REPEAT command for given player.

        Applies to whatever the player is playing: a live external source repeats
        within its own session, a source the device runs itself repeats its own
        content, and Music Assistant's queue repeats its own items.

        :param player_id: player_id of the player to handle the command.
        :param repeat_mode: The repeat mode to apply.
        :param source_id: Optional source (id) the command is aimed at, as listed in the
            player's source_list. Given one, the command is refused when that source is
            no longer playing, so it can never land on whatever took the player since.
        """
        if repeat_mode == RepeatMode.UNKNOWN:
            # not a mode to set: it is what a source reports when it cannot say
            raise InvalidCommand("Cannot set an unknown repeat mode")
        player = self._get_player_with_redirect(player_id)
        active_source_id = self._resolve_command_target(player, source_id)
        if await self._forward_to_external_source(player, SourceControl.REPEAT, repeat_mode):
            return
        if active_queue := self.get_active_queue(player):
            await self.mass.player_queues.set_repeat(active_queue.queue_id, repeat_mode)
            return
        if active_source := next(
            (x for x in player.state.source_list if x.id == active_source_id), None
        ):
            # the source belongs to the player itself (its own Spotify Connect, a device input)
            if not active_source.can_repeat:
                msg = "This action is (currently) unavailable for this source."
                raise PlayerCommandFailed(msg)
            await player.set_repeat(repeat_mode)
            return
        msg = f"There is nothing playing on {player.state.name} to repeat."
        raise PlayerCommandFailed(msg)

    @api_command("players/cmd/next", required_scope=Scope.PLAYERS_CONTROL)
    @handle_player_command
    async def cmd_next_track(self, player_id: str) -> None:
        """Handle NEXT TRACK command for given player."""
        player = self._get_player_with_redirect(player_id)
        active_source_id = player.state.active_source or player.player_id
        if await self._forward_to_external_source(player, SourceControl.NEXT):
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

    @api_command("players/cmd/previous", required_scope=Scope.PLAYERS_CONTROL)
    @handle_player_command
    async def cmd_previous_track(self, player_id: str) -> None:
        """Handle PREVIOUS TRACK command for given player."""
        player = self._get_player_with_redirect(player_id)
        active_source_id = player.state.active_source or player.player_id
        if await self._forward_to_external_source(player, SourceControl.PREVIOUS):
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

    @api_command("players/cmd/power", required_scope=Scope.PLAYERS_CONTROL)
    @handle_player_command(lock=PlayerLockPurpose.PLAYBACK)
    async def cmd_power(self, player_id: str, powered: bool) -> None:
        """
        Send POWER command to given player.

        :param player_id: player_id of the player to handle the command.
        :param powered: bool if player should be powered on or off.
        """
        # Power is serialized with PLAYBACK because powering on a sync/group player
        # forms the group (and powering off dissolves it) - this must not race with
        # play_media / cmd_resume / cmd_set_members on the same player.
        await self._handle_cmd_power(player_id, powered)

    @api_command("players/cmd/volume_set", required_scope=Scope.PLAYERS_CONTROL)
    @handle_player_command
    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """
        Send VOLUME_SET command to given player.

        :param player_id: player_id of the player to handle the command.
        :param volume_level: volume level (0..100) to set on the player.
        """
        volume_level = max(0, min(100, volume_level))
        # record the level and invalidate the group volume state up front, before waiting
        # for the volume lock: a command that is still queued would otherwise undo what a
        # command issued after it already recorded.
        # skip for group players since _handle_cmd_volume_set redirects those to
        # set_group_volume which creates/uses the snapshot itself
        if (player := self.get_player(player_id)) and player.type != PlayerType.GROUP:
            self._record_volume_target(player, volume_level)
            self._invalidate_group_volume_snapshot(player_id)
        async with self.get_player_lock(player_id, PlayerLockPurpose.VOLUME):
            await self._handle_cmd_volume_set(player_id, volume_level, record_target=False)

    @api_command("players/cmd/volume_up", required_scope=Scope.PLAYERS_CONTROL)
    @handle_player_command
    async def cmd_volume_up(self, player_id: str) -> None:
        """
        Send VOLUME_UP command to given player.

        - player_id: player_id of the player to handle the command.
        """
        if not (player := self.get_player(player_id)):
            return
        if player.type == PlayerType.GROUP:
            await self.cmd_group_volume_up(player_id)
            return
        current_volume = self._volume_nudge_base(player) or 0
        new_volume = min(100, current_volume + self._get_volume_step(current_volume))
        await self.cmd_volume_set(player_id, new_volume)

    @api_command("players/cmd/volume_down", required_scope=Scope.PLAYERS_CONTROL)
    @handle_player_command
    async def cmd_volume_down(self, player_id: str) -> None:
        """
        Send VOLUME_DOWN command to given player.

        - player_id: player_id of the player to handle the command.
        """
        if not (player := self.get_player(player_id)):
            return
        if player.type == PlayerType.GROUP:
            await self.cmd_group_volume_down(player_id)
            return
        current_volume = self._volume_nudge_base(player) or 0
        new_volume = max(0, current_volume - self._get_volume_step(current_volume))
        await self.cmd_volume_set(player_id, new_volume)

    @api_command("players/cmd/group_volume", required_scope=Scope.PLAYERS_CONTROL)
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
        group_player = self._resolve_group_volume_player(player)
        if group_player is None:
            # treat as normal player volume change
            await self.cmd_volume_set(player_id, volume_level)
            return
        async with self.get_player_lock(group_player.player_id, PlayerLockPurpose.GROUP_VOLUME):
            await self.set_group_volume(group_player, volume_level)

    @api_command("players/cmd/group_volume_up", required_scope=Scope.PLAYERS_CONTROL)
    @handle_player_command
    async def cmd_group_volume_up(self, player_id: str) -> None:
        """
        Send VOLUME_UP command to given playergroup.

        - player_id: player_id of the player to handle the command.
        """
        player = self.get_player(player_id, True)
        assert player is not None  # for type checker
        # step from the volume of the group as a whole, which is not the volume of the
        # addressed player when the command is addressed to one of its synced members
        group_player = self._resolve_group_volume_player(player) or player
        async with self.get_player_lock(group_player.player_id, PlayerLockPurpose.GROUP_VOLUME):
            cur_volume = self._group_volume_nudge_base(group_player)
            if cur_volume is None:
                return
            new_volume = min(100, cur_volume + self._get_volume_step(cur_volume))
            await self.cmd_group_volume(player_id, new_volume)

    @api_command("players/cmd/group_volume_down", required_scope=Scope.PLAYERS_CONTROL)
    @handle_player_command
    async def cmd_group_volume_down(self, player_id: str) -> None:
        """
        Send VOLUME_DOWN command to given playergroup.

        - player_id: player_id of the player to handle the command.
        """
        player = self.get_player(player_id, True)
        assert player is not None  # for type checker
        group_player = self._resolve_group_volume_player(player) or player
        async with self.get_player_lock(group_player.player_id, PlayerLockPurpose.GROUP_VOLUME):
            cur_volume = self._group_volume_nudge_base(group_player)
            if cur_volume is None:
                return
            new_volume = max(0, cur_volume - self._get_volume_step(cur_volume))
            await self.cmd_group_volume(player_id, new_volume)

    @api_command("players/cmd/group_volume_mute", required_scope=Scope.PLAYERS_CONTROL)
    @handle_player_command
    async def cmd_group_volume_mute(self, player_id: str, muted: bool) -> None:
        """
        Handle muting a playergroup (or synced players) as a whole.

        A group player or syncleader mutes all of its members, a synced player is
        redirected to its syncleader and an ungrouped player is muted on its own.

        :param player_id: Player ID of the player to handle the command.
        :param muted: bool if the group should be muted.
        """
        player = self.get_player(player_id, True)
        assert player is not None  # for type checker
        if player.state.type == PlayerType.GROUP or player.state.group_members:
            # dedicated group player or sync leader
            await self._mute_group_members(player, muted)
            return
        if player.state.synced_to and (sync_leader := self.get_player(player.state.synced_to)):
            # redirect to sync leader
            await self._mute_group_members(sync_leader, muted)
            return
        # treat as normal player mute
        await self.cmd_volume_mute(player_id, muted)

    @api_command("players/cmd/volume_mute", required_scope=Scope.PLAYERS_CONTROL)
    @handle_player_command(lock=PlayerLockPurpose.VOLUME)
    async def cmd_volume_mute(self, player_id: str, muted: bool) -> None:
        """
        Send VOLUME_MUTE command to given player.

        - player_id: player_id of the player to handle the command.
        - muted: bool if player should be muted.
        """
        player = self.get_player(player_id, True)
        assert player

        if player.type == PlayerType.GROUP:
            # redirect to special group mute control
            await self.cmd_group_volume_mute(player_id, muted)
            return

        # clearing the mute lock may not depend on mute support, otherwise a lock set
        # while the player still had a mute control would outlive a control change
        if not muted:
            player.extra_data.pop(ATTR_MUTE_LOCK, None)

        mute_control = player.mute_control
        if mute_control == PLAYER_CONTROL_NONE:
            raise UnsupportedFeaturedException(
                f"Player {player.state.name} does not support muting"
            )

        # Set mute lock for players in a group
        # This prevents auto-unmute when group volume changes
        had_mute_lock = ATTR_MUTE_LOCK in player.extra_data
        if muted and self._is_in_group(player.state):
            player.extra_data[ATTR_MUTE_LOCK] = True

        try:
            await self._handle_cmd_volume_mute(player, mute_control, muted)
        except Exception:
            # a mute that did not happen may not leave a lock behind, but a lock
            # earned by an earlier successful mute must survive
            if not had_mute_lock:
                player.extra_data.pop(ATTR_MUTE_LOCK, None)
            raise

    @handle_player_command
    async def play_media(self, player_id: str, media: PlayerMedia) -> None:
        """
        Handle PLAY MEDIA on given player.

        :param player_id: player_id of the player to handle the command.
        :param media: The Media that needs to be played on the player.
        """
        # An explicit play_media on a captured player honors the player's
        # CONF_PLAY_MEDIA_OVERRIDES_GROUP preference (default: True) — the
        # player is released from its group/sync first, then plays the media
        # standalone. With the preference off, behavior falls back to the
        # legacy "redirect to group leader" path below.
        # Note: the release step runs outside the PLAYBACK lock to avoid an
        # AB-BA cycle with cmd_set_members(group), which acquires lock(group)
        # then lock(sync_leader) via the sync_group provider.
        target_player = self.get_player(player_id, True)
        if target_player is not None and (
            target_player.state.synced_to or target_player.state.active_group
        ):
            override = bool(
                self.mass.config.get_raw_player_config_value(
                    target_player.player_id,
                    CONF_PLAY_MEDIA_OVERRIDES_GROUP,
                    True,
                )
            )
            if override:
                await self._release_player_for_play_media(target_player)
                async with self.get_player_lock(
                    target_player.player_id, PlayerLockPurpose.PLAYBACK
                ):
                    await self._handle_play_media(target_player.player_id, media)
                return
        player = self._get_player_with_redirect(player_id)
        async with self.get_player_lock(player.player_id, PlayerLockPurpose.PLAYBACK):
            await self._handle_play_media(player.player_id, media)

    @api_command("players/cmd/select_sound_mode", required_scope=Scope.PLAYERS_CONTROL)
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

    @api_command("players/cmd/set_option", required_scope=Scope.PLAYERS_CONTROL)
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

    @api_command("players/cmd/select_source", required_scope=Scope.PLAYERS_CONTROL)
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
        # If player is currently grouped, handle it so the source switch can proceed.
        # This allows external sources (e.g. Spotify Connect, AirPlay) to take over a grouped player.
        if player.state.active_group and (
            group_player := self.get_player(player.state.active_group)
        ):
            if player_id in group_player.state.static_group_members:
                # player is a static member of a permanent group - stop the group
                # and power it off if supported, rather than removing the member
                await self._handle_cmd_stop(group_player.player_id)
                if group_player.state.power_control != PLAYER_CONTROL_NONE:
                    await self._handle_cmd_power(group_player.player_id, False)
            else:
                await self.cmd_ungroup(player_id)
        elif player.state.synced_to:
            await self.cmd_ungroup(player_id)
        # Delegate to internal handler for actual implementation
        async with self.get_player_lock(player_id, PlayerLockPurpose.PLAYBACK):
            await self._handle_select_source(player_id, source)

    async def deselect_source(
        self,
        player_id: str,
        stop_playback: bool = True,
        provider_instance_id: str | None = None,
        source_id: str | None = None,
        playback_session_id: str | None = None,
    ) -> None:
        """
        Give up the source a player was playing, and stop it.

        Call this from a plugin when its session ends — the player has nothing to play
        any more, so it goes back to reporting its own queue rather than a source that
        has gone. Pausing is not this: a paused source keeps the player, so that its
        session survives being resumed.

        :param player_id: player_id of the player to give the source up on.
        :param stop_playback: Whether to stop the player as well. Pass False when the
            caller has already stopped it, or is about to.
        :param provider_instance_id: Optional provider instance that owns the source session.
        :param source_id: Optional provider-scoped source id that owns the source session.
        :param playback_session_id: Optional playback session expected to own the player.
        """
        async with self.get_player_lock(player_id, PlayerLockPurpose.PLAYBACK):
            player = self.get_player(player_id, raise_unavailable=False)
            if not player:
                return
            session = self._source_sessions.get(player_id)
            active_provider_instance_id = session.provider_instance_id if session else None
            active_source_id = session.source_id if session else None
            active_playback_session_id = session.playback_session_id if session else None
            if provider_instance_id is not None and (
                active_provider_instance_id != provider_instance_id
                or (source_id is not None and active_source_id != source_id)
                or playback_session_id is None
                or active_playback_session_id != playback_session_id
            ):
                self.logger.debug(
                    "Ignoring source release for provider %s source %s session %s on player %s: "
                    "active source is provider %s source %s session %s",
                    provider_instance_id,
                    source_id,
                    playback_session_id,
                    player_id,
                    active_provider_instance_id,
                    active_source_id,
                    active_playback_session_id,
                )
                return
            try:
                if stop_playback:
                    with suppress(PlayerCommandFailed, PlayerUnavailableError, RuntimeError):
                        await self._handle_cmd_stop(player_id)
            finally:
                if session is not None:
                    current_session = self._source_sessions.get(player_id)
                    if (
                        current_session is session
                        and current_session.playback_session_id == active_playback_session_id
                    ):
                        await self._release_audio_source(player_id)
                    else:
                        self.logger.debug(
                            "Not releasing provider %s source %s session %s on player %s: "
                            "the source changed while playback was stopping",
                            provider_instance_id,
                            source_id,
                            playback_session_id,
                            player_id,
                        )

    async def release_provider_sources(self, provider_instance_id: str) -> None:
        """
        Give up the sources a plugin owns on every player playing one.

        Call this when the plugin goes away: a session outliving its provider leaves
        the player naming a source that can no longer be streamed nor handed back,
        with its own queue held inactive behind it.

        :param provider_instance_id: Instance id of the plugin that is going away.
        """
        sessions = [
            (player_id, session.source_id, session.playback_session_id)
            for player_id, session in self._source_sessions.items()
            if session.provider_instance_id == provider_instance_id
        ]
        for player_id, source_id, playback_session_id in sessions:
            self.logger.debug(
                "Provider %s is unloading, releasing its source on player %s",
                provider_instance_id,
                player_id,
            )
            await self.deselect_source(
                player_id,
                provider_instance_id=provider_instance_id,
                source_id=source_id,
                playback_session_id=playback_session_id,
            )

    @handle_player_command(lock=PlayerLockPurpose.PLAYBACK)
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

    @api_command("players/cmd/set_members", required_scope=Scope.PLAYERS_CONTROL)
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

        # if the target player is a member of an active group player (e.g. a syncgroup),
        # redirect the command to that group player so it can manage the member change
        if (
            parent_player.type != PlayerType.GROUP
            and parent_player.state.active_group
            and (group_player := self.get_player(parent_player.state.active_group))
            and group_player.type == PlayerType.GROUP
            and PlayerFeature.SET_MEMBERS in group_player.state.supported_features
        ):
            self.logger.debug(
                "Redirecting set_members from %s to its group player %s",
                parent_player.name,
                group_player.name,
            )
            await self.cmd_set_members(
                parent_player.state.active_group, player_ids_to_add, player_ids_to_remove
            )
            return

        if parent_player.synced_to:
            # handle edge case: target player is already synced itself to another player
            # automatically ungroup it first and wait for state to propagate
            await self._auto_ungroup_if_synced(parent_player, "setting members")

        # Use lock for playback commands to prevent protocol switches from
        # racing with concurrent play_media / play_index / resume calls.
        async with self.get_player_lock(parent_player.player_id, PlayerLockPurpose.PLAYBACK):
            await self._handle_set_members(parent_player, player_ids_to_add, player_ids_to_remove)

    @api_command("players/cmd/group", required_scope=Scope.PLAYERS_CONTROL)
    @handle_player_command
    async def cmd_group(self, player_id: str, target_player: str) -> None:
        """
        Handle GROUP command for given player.

        Join/add the given player(id) to the given (leader) player/sync group.
        If the target player itself is already synced to another player, this may fail.
        If the player can not be synced with the given target player, this may fail.

        NOTE: This is a convenience helper for cmd_set_members.

        :param player_id: player_id of the player to handle the command.
        :param target_player: player_id of the syncgroup leader or group player.

        :raises UnsupportedFeaturedException: if the target player does not support grouping.
        :raises PlayerCommandFailed: if the target player is already synced to another player.
        :raises PlayerUnavailableError: if the target player is not available.
        :raises PlayerCommandFailed: if the player is already grouped to another player.
        """
        await self.cmd_set_members(target_player, player_ids_to_add=[player_id])

    @api_command("players/cmd/group_many", required_scope=Scope.PLAYERS_CONTROL)
    async def cmd_group_many(self, target_player: str, child_player_ids: list[str]) -> None:
        """
        Join given player(s) to target player.

        Will add the given player(s) to the target player (sync leader or group player).
        This is a (deprecated) alias for cmd_set_members.
        """
        await self.cmd_set_members(target_player, player_ids_to_add=child_player_ids)

    @api_command("players/cmd/ungroup", required_scope=Scope.PLAYERS_CONTROL)
    @handle_player_command
    async def cmd_ungroup(self, player_id: str) -> None:
        """
        Handle UNGROUP command for given player.

        Remove the given player from any (sync)groups it currently is synced to.
        If the player is not currently grouped to any other player,
        this will silently be ignored.
        """
        if not (player := self.get_player(player_id)):
            self.logger.warning("Player %s is not available", player_id)
            return

        # Ungroup on a group player is interpreted as 'release the captured
        # session entirely'. This avoids the "Cannot remove static member"
        # error path when transfer_queue or HA's unjoin asks us to release a
        # group that has static members.
        if player.state.type == PlayerType.GROUP:
            if player.state.power_control != PLAYER_CONTROL_NONE:
                await self._handle_cmd_power(player.player_id, False)
            else:
                await self._handle_cmd_stop(player.player_id)
            return

        if player.state.active_group:
            group = self.get_player(player.state.active_group)
            is_static_member = group is not None and player_id in group.state.static_group_members
            if is_static_member:
                # Static members can't be released individually — recurse so
                # the group-player branch above stops/dissolves the session.
                if group is not None:
                    await self.cmd_ungroup(group.player_id)
                return
            # dynamic or non-static member — remove just this player
            await self.cmd_set_members(player.state.active_group, player_ids_to_remove=[player_id])
            return

        if player.state.synced_to:
            # player is a sync member
            await self.cmd_set_members(player.state.synced_to, player_ids_to_remove=[player_id])
            return

        if player.state.group_members:
            # player is a sync leader (a non-group player with synced followers).
            # Remove only the leader itself: _handle_set_members will either transfer
            # leadership to a remaining member (keeping playback alive) or, when no
            # members remain / nothing is playing, dissolve the group and stop.
            await self.cmd_set_members(player.player_id, player_ids_to_remove=[player.player_id])
            return
        # unjoin from any dynamic sync groups if we're currently in one (edge case)
        # this is in particular used for the Home Assistant integration which does
        # not have a set_members command and only supports a single unjoin command
        for player in self.iter_players(False):
            if not player.state.group_members or player.state.synced_to:
                continue
            if PlayerFeature.SET_MEMBERS not in player.state.supported_features:
                continue
            if player_id in player.state.static_group_members:
                continue
            if player_id in player.state.group_members:
                await self.cmd_set_members(player.player_id, player_ids_to_remove=[player_id])
                return

    @api_command("players/cmd/ungroup_many", required_scope=Scope.PLAYERS_CONTROL)
    async def cmd_ungroup_many(self, player_ids: list[str]) -> None:
        """Handle UNGROUP command for all the given players."""
        for player_id in list(player_ids):
            await self.cmd_ungroup(player_id)

    @api_command("players/create_group_player", required_scope=Scope.CONFIG_PLAYERS_WRITE)
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

    @api_command("players/remove_group_player", required_scope=Scope.CONFIG_PLAYERS_WRITE)
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

    @api_command("players/add_currently_playing_to_favorites", required_scope=Scope.LIBRARY_WRITE)
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
        if self._teardown_in_progress(player):
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

            if player.type not in (PlayerType.GROUP, PlayerType.STEREO_PAIR):
                await self._resolve_mac_addresses(player)

            # restore 'fake' power state from cache if available.
            # Group players intentionally do NOT restore their fake-power
            # state across restarts: at boot there is no sync session yet, so
            # a restored 'powered=True' would put the group in an inconsistent
            # 'active without captured session' state where children appear
            # owned by a group that has no leader. Users who want their
            # 'group captured' state preserved across restarts would need
            # explicit session restoration which is out of scope here.
            if player.type != PlayerType.GROUP:
                cached_value = await self.mass.cache.get(
                    key=player.player_id,
                    provider=self.domain,
                    category=CACHE_CATEGORY_PLAYER_POWER,
                    default=False,
                )
                if cached_value is not None:
                    player.extra_data[ATTR_FAKE_POWER] = cached_value

            # _registration_aborted below only works once the player is in the registry;
            # until then the unregister pass of a provider unload cannot see it, so re-check
            # the guard from the top of this method, which the awaits above may have staled
            if self._teardown_in_progress(player):
                return

            # finally actually register it

            # Despite the fact that the player is not fully ready yet
            # (config not loaded, protocol links not evaluated),
            # we already add it to the _players dict here because we
            # want to make sure the player is available in the controller
            # during the rest of the registration process
            # (such as when fetching config or evaluating protocol links).
            # We use the 'initialized' attribute to indicate that the player
            # is still in the process of being registered so we can filter it out where needed.
            self._players[player_id] = player
            try:
                # update state to ensure player.state reflects the final attributes
                # (e.g. player type) set after super().__init__() in the player subclass,
                # before we fetch config (which relies on state.type for entry resolution)
                player.update_state(signal_event=False)
                # ensure we fetch and set the latest/full config for the player
                player_config = await self.mass.config.get_player_config(player_id)
                if self._registration_aborted(player):
                    return
                player.set_config(player_config)
                # update state again now that config is loaded
                player.update_state(signal_event=False)
                self._save_underlying_player_id(player)
                # call hook after the player is registered and config is set
                await player.on_config_updated()
                if self._registration_aborted(player):
                    return

                # Handle protocol linking
                self._evaluate_protocol_links(player)
            except Exception, asyncio.CancelledError:
                # a player whose setup failed never becomes initialized, which hides it
                # everywhere while it keeps blocking every later registration of the same id.
                # Cancellation counts too: a re-triggered provider discovery aborts the task
                # this runs in. Only roll back while the player is still ours: an unregister
                # may have dropped it already, and it unloads the player itself.
                if self._players.get(player_id) is player:
                    del self._players[player_id]
                    # players claim resources in their constructor (event subscriptions,
                    # connections) that only on_unload releases. Best-effort, so a failing
                    # teardown cannot mask the error that got us here.
                    try:
                        await player.on_unload()
                    except Exception:
                        self.logger.exception("Error unloading player %s", player.name)
                raise

            # now we're ready to signal the player is added and available
            player.set_initialized()
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
            # register playerqueue for this player (if not a protocol player)
            if player.state.type != PlayerType.PROTOCOL:
                await self.mass.player_queues.on_player_register(player)
                if self._registration_aborted(player):
                    # the queue restore outlived the unregister that already cleaned it up,
                    # so drop the queue we just recreated for a player that is gone
                    self.mass.player_queues.on_player_remove(player_id, permanent=False)

        # Schedule debounced update of all players since can_group_with values may change
        # when a new player is added (provider IDs expand to include the new player)
        self._schedule_update_all_players(2)

    async def register_or_update(self, player: Player) -> None:
        """Register a new player on the controller or update existing one."""
        if self._teardown_in_progress(player):
            return

        # the register lock ensures a replacement is never swapped in while register()
        # is still setting the player up
        async with self._register_lock:
            if (existing := self._players.get(player.player_id)) is not None:
                # a protocol player is hidden behind its parent and owns no queue, every
                # other player does. Reading the role the player is leaving off that
                # published reality keeps it independent of when the player's state was
                # last recalculated, which providers cannot control (they flip the type
                # before this call).
                was_protocol = self.mass.player_queues.get(player.player_id) is None
                becomes_protocol = player.type == PlayerType.PROTOCOL
                role_changed = becomes_protocol != was_protocol
                if role_changed:
                    # release the topology of the role the player is leaving
                    self._cleanup_player_type_transition(
                        existing, becomes_protocol=becomes_protocol
                    )
                self._players[player.player_id] = player
                if existing is not player:
                    # a fresh instance starts out with a base config only, so it needs
                    # the config the registration resolved before it can be used
                    player.set_config(existing.config)
                    await player.on_config_updated()
                    if self._registration_aborted(player):
                        return
                # the replacement takes over the identity of an already registered
                # player, so it must be marked initialized as well
                player.set_initialized()
                player.update_state()
                # the derived-transport edge may have been set/revoked after the
                # initial registration (e.g. via a bridge claim)
                self._save_underlying_player_id(player)
                if role_changed:
                    await self._finish_player_type_transition(player)
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
        # mark dirty right away (not at execution): a trigger means state the player
        # derives from changed, and a direct update_state call may come in before
        # the debounced one runs
        player.mark_state_dirty()
        task_id = f"player_update_state_{player_id}"
        self.mass.call_later(
            debounce_delay,
            player.update_state,
            force_update=force_update,
            task_id=task_id,
        )

    async def unregister(
        self,
        player_id: str,
        permanent: bool = False,
        replacement_player_id: str | None = None,
    ) -> None:
        """
        Unregister a player from the player controller.

        Called (by a PlayerProvider) when a player is removed or no longer available
        (for a longer period of time). This will remove the player from the player
        controller and optionally remove the player's config from the mass config.
        If the player is not registered, this will silently be ignored.

        :param player_id: Player ID of the player to unregister.
        :param permanent: If True, remove the player permanently by deleting its config.
                          If False, the player config will not be removed.
        :param replacement_player_id: Player ID that takes this player's place, only
                                      used for a permanent removal.
        """
        player = self._players.get(player_id)
        if player is None:
            return
        # a player that is going away is done with any live source it was playing,
        # so let the owning plugin release an upstream session pointing at us
        await self._release_audio_source(player_id)
        del self._players[player_id]
        # clean up all lock entries for this player
        for prefix in [p.value for p in PlayerLockPurpose]:
            self._player_command_locks.pop(f"{prefix}_{player_id}", None)
        if handle := self._pending_protocol_evaluations.pop(player_id, None):
            handle.cancel()
        self._clear_sleep_timer(player)
        self.mass.player_queues.on_player_remove(player_id, permanent=permanent)
        # teardown is best-effort: a provider that fails to release its player must not
        # strand the other players of that provider, nor the provider unload itself
        try:
            await player.on_unload()
        except Exception:
            self.logger.exception("Error unloading player %s", player.name)
        if permanent:
            # player permanent removal: cleanup protocol links, delete config
            # and signal PLAYER_REMOVED event.
            # No group detach is issued here: the player is already out of the registry,
            # so it is filtered out of every group's live member list, and its persisted
            # membership is settled by delete_player_config below.
            self._cleanup_protocol_links(player)
            self.delete_player_config(player_id, replacement_player_id)
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

    @api_command("players/remove", required_scope=Scope.CONFIG_PLAYERS_WRITE)
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

    def delete_player_config(
        self, player_id: str, replacement_player_id: str | None = None
    ) -> None:
        """
        Permanently delete a player's configuration, including its DSP and queue settings.

        The saved queue of a player that is no longer registered is dropped along with it,
        so a device that returns under the same id starts out fresh. The player itself is
        not unregistered.
        The config of a linked protocol player is wiped along with it, so the device
        returns as a brand new player once it is discovered again. Protocol players that
        are still registered or that already moved to another parent keep their config;
        registered ones are detached from the removed player and re-evaluated.
        Any group that lists the player as a member follows the replacement, or loses
        the member when there is none.

        :param player_id: Player ID of the player to delete the configuration of.
        :param replacement_player_id: Player ID that takes this player's place, so users
                                      restricted to it and groups it belongs to follow
                                      the replacement.
        """
        self._detach_protocol_children(player_id)
        self._update_group_memberships(player_id, replacement_player_id)
        player_ids = [
            protocol_id
            for protocol_id in self.mass.config.get(CONF_PLAYERS, {})
            if self._get_cached_protocol_parent_id(protocol_id) == player_id
            and self.get_player(protocol_id) is None
        ]
        player_ids.append(player_id)
        for pid in player_ids:
            for key in (
                f"{CONF_PLAYERS}/{pid}",
                f"{CONF_PLAYER_DSP}/{pid}",
                f"{CONF_PLAYER_QUEUES}/{pid}",
            ):
                self.mass.config.remove(key)
            if self.get_player(pid) is None:
                self.mass.player_queues.purge_saved_queue(pid)
        # a user access filter is an allow-list of player ids, so it must not be left
        # pointing at a player whose config was just wiped: a replaced player hands its
        # entries over to its replacement, a removed one has them dropped
        if replacement_player_id:
            self.mass.create_task(
                self.mass.webserver.auth.replace_player_in_user_filters(
                    player_id, replacement_player_id, removed_player_ids=player_ids
                )
            )
        else:
            self.mass.create_task(
                self.mass.webserver.auth.remove_from_user_filters(player_ids=player_ids)
            )

    def scale_volume_to_device(self, player_id: str, logical_volume: int) -> int:
        """Scale logical volume (0-100) to device volume (min_volume-max_volume)."""
        min_volume, max_volume = self._get_volume_limits(player_id)
        if min_volume == 0 and max_volume == 100:
            return logical_volume
        # Scale: logical 0 -> min_volume, logical 100 -> max_volume
        return min_volume + (logical_volume * (max_volume - min_volume)) // 100

    def scale_volume_from_device(self, player_id: str, device_volume: int) -> int:
        """Scale device volume (min_volume-max_volume) to logical volume (0-100)."""
        min_volume, max_volume = self._get_volume_limits(player_id)
        if min_volume == 0 and max_volume == 100:
            return device_volume
        volume_range = max_volume - min_volume
        if volume_range == 0:
            return 0
        # Scale to 0-100 without clamping so that out-of-range device volumes
        # produce distinct logical values, ensuring state change detection triggers
        # volume limit enforcement
        return ((device_volume - min_volume) * 100) // volume_range

    def on_player_position_jumped(self, player: Player) -> None:
        """
        Handle a discrete jump of a player's corrected playback position.

        Called by a Player when its corrected position moved significantly
        outside regular playback progression (seek or buffer correction). This
        is not an event by itself: it re-bases the active queue's timing on the
        fresh position and nudges related players so derived positions stay in
        sync; current_media then re-anchors from the corrected queue time on
        the follow-up update, which emits the actual update event.
        """
        if self.mass.closing:
            return
        self.mass.player_queues.on_player_elapsed_time_corrected(player)
        self.trigger_player_update(player.player_id)
        self._forward_state_update(player, {})

    def signal_player_state_update(
        self,
        player: Player,
        changed_values: dict[str, tuple[Any, Any]],
        force_update: bool = False,
        skip_forward: bool = False,
        media_position_jumped: bool = False,
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

        # The current_media position anchor only changes on discrete events
        # (play/pause/seek/track change/buffer correction), so a change set holding
        # only anchor keys represents a position correction rather than a regular
        # state change.
        non_anchor_keys = changed_values.keys() - POSITION_ANCHOR_KEYS
        if len(non_anchor_keys) == 0 and not force_update:
            if not media_position_jumped:
                # anchor adoption without a significant corrected-position change
                return
            # current_media's corrected position jumped (seek or buffer correction
            # reached the current media): emit the full player update below so
            # consumers see the fresh position

        if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            self.logger.log(
                VERBOSE_LOG_LEVEL,
                "Player state updated for %s: changed fields: %s",
                player.name,
                ", ".join(changed_values.keys()),
            )

        # signal update to the playerqueue
        if player.state.type != PlayerType.PROTOCOL:
            self.mass.call_later(
                0.5,
                self.mass.player_queues.on_player_update,
                player,
                changed_values,
                task_id=f"queue_on_player_update_{player.player_id}",
            )

        # Kick async palette extraction on cold cache. On transition prefetch
        # the next queue item too. Skip players that mirror another player's media
        # (grouped/synced members, protocol children): their current_media - palette
        # included - is taken wholesale from the owner, so resolving it per member is
        # wasted work that also produces duplicate state updates across the group.
        if (
            not self._mirrors_parent_media(player)
            and (current_media := player.state.current_media)
            and current_media.image_url
        ):
            if current_media.palette is None:
                self._schedule_palette_fetch(player_id, current_media.image_url)
            if "current_media.image_url" in changed_values or "current_media" in changed_values:
                self._schedule_next_queue_item_palette_prefetch(player_id, current_media)

        # handle DSP reload of the leader when grouping/ungrouping
        if ATTR_GROUP_MEMBERS in changed_values:
            prev_group_members, new_group_members = changed_values[ATTR_GROUP_MEMBERS]
            self._handle_group_dsp_change(player, prev_group_members or [], new_group_members)
            # Removed group members also need to be updated since they are no longer part
            # of this group and are available for playback again
            removed_members = set(prev_group_members or []) - set(new_group_members or [])
            for _removed_player_id in removed_members:
                if removed_player := self.get_player(_removed_player_id):
                    removed_player.refresh_state()

        # detect when active_source changes to
        # something external while we have a grouped protocol active
        if ATTR_ACTIVE_SOURCE in changed_values:
            task_id = f"external_source_takeover_{player_id}"
            self.mass.call_later(
                5,
                self._check_external_source_takeover,
                player,
                task_id=task_id,
            )
        # only steer into the (relatively expensive) membership cleanup when a field
        # that can require an unsync actually changed - this runs on every state tick
        if changed_values.keys() & {ATTR_AVAILABLE, ATTR_ENABLED, ATTR_POWERED}:
            self._handle_membership_cleanup_on_state_change(player, changed_values)

        # enforce volume limits when volume changes externally
        if "volume_level" in changed_values:
            corrected = self._enforce_volume_limits(player)
            # a level set on the device itself makes the reference a group volume change
            # interpolates from obsolete. a member on its way to a level we did send
            # reports levels too, and a group only ever reports what its members are at,
            # so neither of those counts. a correction always is the device's own doing:
            # the levels we command never fall outside the configured range
            if player.state.type != PlayerType.GROUP and (
                corrected or self._unexpired_volume_target(player) is None
            ):
                self._invalidate_group_volume_snapshot(player_id)
        # dispatch to internal state update subscribers (with changed_values)
        self._dispatch_state_update_subscribers(player, changed_values)

        # signal player update on the eventbus
        if player.state.type != PlayerType.PROTOCOL:
            self.mass.signal_event(EventType.PLAYER_UPDATED, object_id=player_id, data=player)

        # signal a separate PlayerOptionsUpdated event
        if options := changed_values.get("options"):
            self.mass.signal_event(
                EventType.PLAYER_OPTIONS_UPDATED, object_id=player_id, data=options
            )
        # signal player config update event if playerfeatures changed
        # this is temporary needed for the Home Assistant integration which only
        # re-evalues the entity's supported features on a PLAYER_CONFIG_UPDATED event.
        # TODO: Remove this temporary workaround once the HA integration is updated to
        # also re-evaluate supported features on PLAYER_UPDATED events.
        if changed_values.keys() & {
            ATTR_SUPPORTED_FEATURES,
            ATTR_MUTE_CONTROL,
            ATTR_VOLUME_CONTROL,
            ATTR_POWER_CONTROL,
        }:
            self.mass.signal_event(
                EventType.PLAYER_CONFIG_UPDATED, object_id=player_id, data=player.config
            )

        if not skip_forward or force_update:
            self._forward_state_update(player, changed_values)

        # trigger update of all players in a provider if group related fields changed
        # this ensures that calculated fields like can_group_with are updated on all players
        if any(key in changed_values for key in ("group_members", "synced_to", "available")):
            for prov_player in player.provider.players:
                self.trigger_player_update(prov_player.player_id, debounce_delay=2)

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
        self.update_player_control(player_control.id, include_configured=True)

    async def register_or_update_player_control(self, player_control: PlayerControl) -> None:
        """Register a new playercontrol on the controller or update existing one."""
        if self.mass.closing:
            return
        if player_control.id in self._controls:
            self._controls[player_control.id] = player_control
            self.update_player_control(player_control.id, include_configured=True)
            return
        await self.register_player_control(player_control)

    def update_player_control(self, control_id: str, include_configured: bool = False) -> None:
        """
        Refresh the players that use the given player control.

        :param control_id: The control whose state or availability changed.
        :param include_configured: Also refresh the players that select this control in their
            config but do not currently resolve to it. Needed when a control (re)appears,
            because such a player has already fallen back to another control and would
            otherwise never pick this one back up.
        """
        if self.mass.closing:
            return
        # update all players that are using this control
        for player in list(self._players.values()):
            if control_id in (
                player.state.power_control,
                player.state.volume_control,
                player.state.mute_control,
            ) or (
                include_configured and control_id in self._configured_control_ids(player.player_id)
            ):
                self.mass.loop.call_soon(player.refresh_state)

    def remove_player_control(self, control_id: str) -> None:
        """Remove a player_control from the player manager."""
        control = self._controls.pop(control_id, None)
        if control is None:
            return
        self.logger.info("PlayerControl removed: %s", control.name)
        # players configured to use this control still resolve to it until they are
        # refreshed, so let them fall back to their remaining options right away
        self.update_player_control(control_id)

    def get_player_provider(self, player_id: str) -> PlayerProvider:
        """Return PlayerProvider for given player."""
        player = self._players[player_id]
        assert player  # for type checker
        return player.provider

    def get_active_queue(self, player: Player) -> PlayerQueue | None:
        """Return the current active queue for a player (if any)."""
        # account for player that is synced (sync child)
        if player.state.synced_to and player.state.synced_to != player.player_id:
            if sync_leader := self.get_player(player.state.synced_to):
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
        """
        Set the overall volume for a player group or synced players.

        Uses interpolation to adjust all child volumes while preserving their
        relative balance. A snapshot of child volumes is cached on first call and
        used as the reference point for subsequent adjustments.

        :param group_player: The group player or sync leader.
        :param volume_level: Target volume level (0..100).
        """
        cur_volume = group_player.state.group_volume
        if cur_volume is None:
            return

        children: list[Player] = []
        for child_player in self.iter_group_members(
            group_player, only_powered=True, exclude_self=False
        ):
            if child_player.state.volume_control == PLAYER_CONTROL_NONE:
                continue
            children.append(child_player)
        if not children:
            return

        # cache a snapshot of child volumes on the group player as reference for interpolation.
        # scaling up: each child interpolates from its snapshot value toward 100.
        # scaling down: each child interpolates from its snapshot value toward 0.
        # this ensures the relative balance is preserved and all children converge
        # to 0 and 100 at the extremes. the snapshot is invalidated when a child's
        # individual volume or the group membership changes, and rebuilt when the
        # children it holds are no longer the ones being adjusted.
        # the levels a nudge steps from are the ones the members were last commanded, so
        # the snapshot has to read the same source, or a change a member has not confirmed
        # yet puts the reference above the level being set and turns a step up into one down
        snapshot: dict[str, int] | None = group_player.extra_data.get(ATTR_GROUP_VOLUME_SNAPSHOT)
        if snapshot is None or snapshot.keys() != {c.player_id for c in children}:
            snapshot = {c.player_id: self._volume_nudge_base(c) or 0 for c in children}
            group_player.extra_data[ATTR_GROUP_VOLUME_SNAPSHOT] = snapshot

        base_group = max(snapshot.values())

        coros = []
        for child_player in children:
            child_base = snapshot.get(child_player.player_id, 0)
            if volume_level >= base_group:
                # scaling up: interpolate each child from snapshot toward 100
                if base_group >= 100:
                    new_child_volume = child_base
                else:
                    progress = (volume_level - base_group) / (100 - base_group)
                    new_child_volume = round(child_base + (100 - child_base) * progress)
            elif base_group == 0:
                new_child_volume = 0
            else:
                # scaling down: interpolate each child from snapshot toward 0
                progress = volume_level / base_group
                new_child_volume = round(child_base * progress)
            new_child_volume = max(0, min(100, new_child_volume))
            coros.append(self._set_member_volume(child_player.player_id, new_child_volume))
        await asyncio.gather(*coros)

        # notify active AudioSource once at the group level to prevent
        # feedback loops from per-child callbacks with different volume values
        await self._notify_source_volume_change(group_player, volume_level)

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

    def subscribe_player_state_update(
        self,
        callback: Callable[[Player, dict[str, tuple[Any, Any]]], None],
    ) -> Callable[[], None]:
        """
        Subscribe to player state update notifications.

        The callback receives the Player and a dict of changed values
        (mapping attribute name to a (previous, new) tuple).

        :param callback: Function to invoke for each player state update.
        :return: An unsubscribe function.
        """
        self._state_update_subscribers.append(callback)

        def _unsub() -> None:
            with suppress(ValueError):
                self._state_update_subscribers.remove(callback)

        return _unsub

    @contextlib.asynccontextmanager
    async def wait_for_player_update(
        self,
        player_id: str,
        attribute_name: str | None = None,
        attribute_value: Any = _SENTINEL,
        timeout: float = 5.0,
    ) -> AsyncIterator[None]:
        """
        Async context manager that waits for a player state update.

        Subscribes to player state updates on entry, runs the body (typically
        the action that triggers the expected update), then waits for a
        matching update on exit. If ``attribute_name`` and ``attribute_value``
        are both provided and the current value already matches at entry, the
        wait is skipped.

        Example::

            async with mass.players.wait_for_player_update(
                player_id, attribute_name="playback_state",
                attribute_value=PlaybackState.IDLE, timeout=5,
            ):
                await mass.players._handle_cmd_stop(player_id)

        :param player_id: The player ID to wait for.
        :param attribute_name: Optional state attribute to watch for changes
            (e.g. ``"playback_state"``). If omitted, any state change satisfies
            the wait.
        :param attribute_value: Optional value the watched attribute must reach.
            Only meaningful in combination with ``attribute_name``.
        :param timeout: Maximum time to wait in seconds.
        """
        update_event = asyncio.Event()

        def _on_state_update(player: Player, changed_values: dict[str, tuple[Any, Any]]) -> None:
            if player.player_id != player_id:
                return
            if attribute_name is None:
                update_event.set()
                return
            if attribute_name not in changed_values:
                return
            if attribute_value is _SENTINEL:
                update_event.set()
                return
            _prev, new_val = changed_values[attribute_name]
            if new_val == attribute_value:
                update_event.set()

        # short-circuit when the desired value is already the current state
        already_satisfied = (
            attribute_name is not None
            and attribute_value is not _SENTINEL
            and (player := self.get_player(player_id)) is not None
            and getattr(player.state, attribute_name, _SENTINEL) == attribute_value
        )

        unsub = self.subscribe_player_state_update(_on_state_update)
        try:
            yield
            if already_satisfied:
                return
            try:
                async with asyncio.timeout(timeout):
                    await update_event.wait()
            except TimeoutError:
                self.logger.debug(
                    "Timed out waiting for player update on %s (attr=%s value=%s)",
                    player_id,
                    attribute_name,
                    attribute_value,
                )
        finally:
            unsub()

    async def on_player_config_change(self, config: PlayerConfig, changed_keys: set[str]) -> None:
        """Call (by config manager) when the configuration of a player changes."""
        min_vol_changed = f"values/{CONF_MIN_VOLUME}" in changed_keys
        max_vol_changed = f"values/{CONF_MAX_VOLUME}" in changed_keys
        if min_vol_changed or max_vol_changed:
            raw_min = config.get_value(CONF_MIN_VOLUME)
            raw_max = config.get_value(CONF_MAX_VOLUME)
            min_vol = int(cast("int", raw_min)) if raw_min is not None else 0
            max_vol = int(cast("int", raw_max)) if raw_max is not None else 100
            if min_vol > max_vol:
                msg = "Minimum volume cannot exceed maximum volume"
                raise InvalidDataError(msg)
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
            # Collect linked protocol IDs to cascade the enable/disable to.
            # Without this, a disabled native parent leaves its linked protocols
            # registered after restart; they then fail to find their parent and
            # get wrapped in a fresh Universal Player.
            cascade_protocol_ids: list[str] = []
            parent_is_protocol = player.state.type == PlayerType.PROTOCOL if player else False
            if not parent_is_protocol:
                if player and player.linked_output_protocols:
                    cascade_protocol_ids = [
                        link.output_protocol_id for link in player.linked_output_protocols
                    ]
                else:
                    cascade_protocol_ids = self._get_cached_protocol_ids(config.player_id)
            if player_disabled:
                player_provider.on_player_disabled(config.player_id)
            elif player_enabled:
                player_provider.on_player_enabled(config.player_id)
            for protocol_id in cascade_protocol_ids:
                protocol_raw = self.mass.config.get(f"{CONF_PLAYERS}/{protocol_id}")
                if not protocol_raw:
                    continue
                if bool(protocol_raw.get("enabled", True)) == bool(player_enabled):
                    continue
                self.mass.create_task(
                    self.mass.config.save_player_config(
                        protocol_id, {ATTR_ENABLED: bool(player_enabled)}
                    )
                )
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
                v.requires_reload
                for v in config.values.values()
                if f"values/{v.key}" in changed_keys
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
            if self.get_active_queue(player):
                self.mass.call_later(
                    0, self.mass.player_queues.resume, player.state.active_source, False
                )
                return
            # if the player is not using a queue, we need to stop and start playback
            await self.cmd_stop(player_id)
            await self.cmd_play(player_id)

    def schedule_active_output_protocol_clear(self, player: Player) -> None:
        """
        Clear the player's active output protocol once it stops playing.

        A device may keep reporting PLAYING for a short while after a stop
        command, so the clear is deferred until the player reports IDLE (with a
        timeout as fallback). Starting a new session cancels the pending clear
        (see Player.set_active_output_protocol).

        :param player: The player whose active output protocol must be cleared.
        """
        # Deduplicated per player via task_id: if a clear is already pending we
        # keep it, so the single tracked task stays cancellable by a new session.
        self.mass.create_task(
            self._clear_active_output_protocol_when_idle(player),
            task_id=f"clear_active_protocol_{player.player_id}",
        )

    def __iter__(self) -> Iterator[Player]:
        """Iterate over all players."""
        return iter(self._players.values())

    async def _resolve_mac_addresses(self, player: Player) -> None:
        """
        Resolve and persist the MAC addresses used to match the player against protocols.

        :param player: The player to resolve the MAC address(es) for.
        """
        conf_base = f"{CONF_PLAYERS}/{player.player_id}/values"
        # Save the original MAC reported by the provider (before ARP enrichment)
        reported_mac = player.device_info.identifiers.get(IdentifierType.MAC_ADDRESS)

        # Try to use cached ARP MAC from config for fast matching on restart.
        # This allows protocol linking to work immediately even if ARP is slow/fails.
        cached_arp_mac: str | None = self.mass.config.get(
            f"{conf_base}/{CONF_CACHED_ARP_MAC}", None
        )
        if cached_arp_mac and is_valid_mac_address(cached_arp_mac):
            player.device_info.add_identifier(IdentifierType.MAC_ADDRESS, cached_arp_mac)

        # Enrich device MAC address via ARP if needed
        # (handles invalid MACs, locally-administered MACs, and missing MACs)
        await enrich_device_mac_address(player.device_info, self.logger)

        # Cache the resolved MAC for fast matching on subsequent restarts
        current_mac = player.device_info.identifiers.get(IdentifierType.MAC_ADDRESS)
        if current_mac and is_valid_mac_address(current_mac) and current_mac != cached_arp_mac:
            self.mass.config.set(f"{conf_base}/{CONF_CACHED_ARP_MAC}", current_mac)

        # Store original reported MAC if it differs from the resolved MAC.
        # This enables multi-MAC matching for devices with multiple interfaces
        # (e.g., WiFi + Ethernet) where ARP resolves one interface but the
        # protocol reports the other.
        if reported_mac and is_valid_mac_address(reported_mac) and current_mac:
            if reported_mac.upper() != current_mac.upper():
                player.extra_data["reported_mac"] = reported_mac
                self.mass.config.set(f"{conf_base}/{CONF_REPORTED_MAC}", reported_mac)
            else:
                # Provider's reported MAC matches the resolved MAC; clear any stale
                # stored reported MAC to avoid false-positive multi-MAC matches.
                self.mass.config.set(f"{conf_base}/{CONF_REPORTED_MAC}", None)
        elif not reported_mac or not is_valid_mac_address(reported_mac):
            # Restore reported MAC from config on restart only when the provider
            # did not supply a usable MAC address.
            cached_reported_mac: str | None = self.mass.config.get(
                f"{conf_base}/{CONF_REPORTED_MAC}", None
            )
            if cached_reported_mac and is_valid_mac_address(cached_reported_mac):
                if current_mac and cached_reported_mac.upper() == current_mac.upper():
                    # Cached value matches the resolved MAC; clear stale entry.
                    self.mass.config.set(f"{conf_base}/{CONF_REPORTED_MAC}", None)
                else:
                    player.extra_data["reported_mac"] = cached_reported_mac

    def _teardown_in_progress(self, player: Player) -> bool:
        """
        Return True if the server or this player's provider is shutting down.

        :param player: The player that is in the process of being registered.
        """
        return self.mass.closing or player.provider.unloading

    def _registration_aborted(self, player: Player) -> bool:
        """
        Return True if the given player is no longer the registered player for its ID.

        :param player: The player that is in the process of being registered.
        """
        # registration awaits provider I/O while the player is already in the registry,
        # so an unregister (e.g. a provider unload or a device disconnect) can drop or
        # replace it in the meantime, after which registration must stop
        if self._players.get(player.player_id) is player:
            return False
        self.logger.debug(
            "Registration of player %s aborted: it was unregistered while setting up",
            player.player_id,
        )
        return True

    async def _finish_player_type_transition(self, player: Player) -> None:
        """
        Publish a registered player that moved in or out of the protocol role.

        :param player: The player, with its new type already applied to its state.
        """
        self._evaluate_protocol_links(player)
        if player.state.type == PlayerType.PROTOCOL:
            # the player is hidden behind its parent from now on and no longer owns a queue.
            # only the queue is dropped, never the playback: the player either just became a
            # (hidden) bridge client with nothing playing on it, or is already serving its
            # parent, where a stop would cut that parent's stream short. A protocol player
            # has no active group of its own either, so there is nothing to detach here.
            self.mass.signal_event(EventType.PLAYER_REMOVED, player.player_id)
            self.mass.player_queues.on_player_remove(player.player_id, permanent=False)
            return
        # the player surfaces on its own, which leaves it unusable without a queue
        self.mass.signal_event(EventType.PLAYER_ADDED, object_id=player.player_id, data=player)
        await self.mass.player_queues.on_player_register(player)
        if self._registration_aborted(player):
            # the queue restore outlived the unregister that already cleaned it up,
            # so drop the queue we just recreated for a player that is gone
            self.mass.player_queues.on_player_remove(player.player_id, permanent=False)

    async def _release_player_for_play_media(self, player: Player) -> None:
        """
        Release a captured player so a play_media command can target it directly.

        :param player: The captured player to release.
        """
        # Strategy is picked from how the player is currently captured:
        #   synced_to            → unsync this player (cmd_ungroup)
        #   dynamic group member → remove from group via cmd_set_members
        #   static group member  → dissolve the whole group (power off if it
        #                          has a real power control, otherwise stop)
        # In every branch we wait for the relevant state attribute to actually
        # clear before returning. Providers (Sonos in particular) reject a
        # play_media on a player whose synced_to/active_group is still set
        # locally even though the release command has been acknowledged.
        if player.state.synced_to:
            self.logger.debug(
                "Unsyncing %s from %s to honor explicit play_media target",
                player.state.name,
                player.state.synced_to,
            )
            async with self.wait_for_player_update(
                player.player_id,
                attribute_name="synced_to",
                attribute_value=None,
                timeout=5,
            ):
                await self.cmd_ungroup(player.player_id)
            return
        if not player.state.active_group:
            return
        group = self.get_player(player.state.active_group)
        if group is None:
            return
        is_dynamic_member = (
            PlayerFeature.SET_MEMBERS in group.state.supported_features
            and player.player_id not in group.state.static_group_members
        )
        if is_dynamic_member:
            self.logger.debug(
                "Removing %s from dynamic group %s to honor explicit play_media target",
                player.state.name,
                group.state.name,
            )
            async with self.wait_for_player_update(
                player.player_id,
                attribute_name="active_group",
                attribute_value=None,
                timeout=5,
            ):
                await self.cmd_set_members(group.player_id, player_ids_to_remove=[player.player_id])
            return
        # static member: a single member can't be released, so the whole
        # group must dissolve. Prefer cmd_power when an explicit power
        # control is set so the user-visible state stays consistent.
        async with self.wait_for_player_update(
            player.player_id,
            attribute_name="active_group",
            attribute_value=None,
            timeout=5,
        ):
            if group.state.power_control != PLAYER_CONTROL_NONE and group.state.powered:
                self.logger.debug(
                    "Powering off %s to honor explicit play_media target on %s",
                    group.state.name,
                    player.state.name,
                )
                await self._handle_cmd_power(group.player_id, False)
            else:
                self.logger.debug(
                    "Stopping %s to honor explicit play_media target on %s",
                    group.state.name,
                    player.state.name,
                )
                await self._handle_cmd_stop(group.player_id)

    def _mirrors_parent_media(self, player: Player) -> bool:
        """
        Return True if the player's current_media is taken from another player.

        Grouped/synced members and protocol children mirror their parent's
        current_media (palette included), so they must not resolve it themselves.

        :param player: The player to check.
        """
        state = player.state
        # a self-referential active_group/synced_to is not a real parent (mirror the
        # != self guard in Player.__final_current_media), so it must not skip resolution
        parent_id = state.active_group or state.synced_to
        if parent_id and parent_id != player.player_id:
            return True
        return state.type == PlayerType.PROTOCOL and player.protocol_parent_id is not None

    def _schedule_palette_fetch(
        self, player_id: str, image_url: str | None, *, trigger_update: bool = True
    ) -> None:
        """
        Kick off an async palette extraction for an image URL.

        :param player_id: Player the palette is scoped to (used for task dedup).
        :param image_url: Image URL to extract from. No-op when empty or already cached.
        :param trigger_update: When True, re-emit player state once palette is ready
                               (current track). When False, only warm the cache (prefetch).
        """
        if not image_url:
            return
        # Key the task on the image (not just the player) so a track change always
        # schedules a fetch for the new image instead of being dropped by an in-flight
        # fetch for the previous one; repeated schedules for the same image still dedupe.
        slot = "current" if trigger_update else "next"
        self.mass.create_task(
            self._fetch_palette(player_id, image_url, trigger_update=trigger_update),
            task_id=f"palette_fetch_{player_id}_{slot}_{image_url}",
            abort_existing=False,
        )

    async def _fetch_palette(self, player_id: str, image_url: str, *, trigger_update: bool) -> None:
        palette = await get_palette_for_url(self.mass, image_url)
        if palette is None or not trigger_update:
            return  # prefetch only warms the cache controller; nothing to attach
        player = self.get_player(player_id)
        if player is None:
            return
        current = player.state.current_media
        if current is None or current.image_url != image_url:
            return  # media changed while fetching
        # Carry the palette on player state so the (sync) serialization reads it back.
        player.set_resolved_palette(image_url, palette)
        # Avoid trigger_player_update so a concurrent state-change debounce
        # doesn't cancel our timer via the shared player_update_state task_id.
        self.mass.call_later(
            0,
            player.update_state,
            force_update=True,
            task_id=f"palette_player_update_{player_id}",
        )

    def _schedule_next_queue_item_palette_prefetch(
        self, player_id: str, current_media: PlayerMedia
    ) -> None:
        """Warm the palette cache for the next queue item so it's hot at transition."""
        queue_id, item_id = current_media.source_id, current_media.queue_item_id
        if not queue_id or not item_id:
            return
        next_item = self.mass.player_queues.get_next_item(queue_id, item_id)
        if next_item is None or not next_item.image:
            return
        next_url = self.mass.metadata.get_image_url(
            next_item.image, size=512, prefer_stream_server=True
        )
        self._schedule_palette_fetch(player_id, next_url, trigger_update=False)

    def _configured_control_ids(self, player_id: str) -> set[str]:
        """Return the player control ids the given player's config selects."""
        return {
            str(value)
            for conf_key in (CONF_POWER_CONTROL, CONF_VOLUME_CONTROL, CONF_MUTE_CONTROL)
            if (value := self.mass.config.get_raw_player_config_value(player_id, conf_key))
        }

    def _get_volume_step(self, current_volume: int) -> int:
        """
        Return the step size for a single volume increment at the given level.

        A configured (non-zero) `volume_step` is a flat step. The default of 0 keeps the
        adaptive ladder, which takes finer steps near the ends of the range.
        """
        if configured := self.get_config_value(CONF_VOLUME_STEP, 0, return_type=int):
            return configured
        if current_volume < 10 or current_volume > 90:
            return 1
        if current_volume < 30 or current_volume > 70:
            return 2
        return 3

    def _get_volume_limits(self, player_id: str) -> tuple[int, int]:
        """Get the configured min/max volume limits for a player."""
        min_volume = int(
            cast(
                "int",
                self.mass.config.get_raw_player_config_value(
                    player_id, CONF_MIN_VOLUME, CONF_ENTRY_MIN_VOLUME.default_value
                ),
            )
        )
        max_volume = int(
            cast(
                "int",
                self.mass.config.get_raw_player_config_value(
                    player_id, CONF_MAX_VOLUME, CONF_ENTRY_MAX_VOLUME.default_value
                ),
            )
        )
        return min_volume, max_volume

    def _enforce_volume_limits(self, player: Player) -> bool:
        """
        Clamp device volume to min/max range when changed externally.

        :param player: The player to check the volume of.
        :return: True if the volume was outside the configured range and got corrected.
        """
        player_id = player.player_id
        min_volume, max_volume = self._get_volume_limits(player_id)
        if min_volume == 0 and max_volume == 100:
            return False
        # state.volume_level is the resolved logical volume, available for all
        # volume control types; a device volume outside the configured range
        # surfaces here as a value outside 0-100 (scaling does not clamp)
        logical_volume = player.state.volume_level
        if logical_volume is None or 0 <= logical_volume <= 100:
            return False
        clamped = max(0, min(100, logical_volume))
        # correct via the regular volume-set path so scaling and redirection apply
        self.mass.create_task(self._handle_cmd_volume_set(player_id, clamped))
        return True

    def _forward_state_update(
        self, player: Player, changed_values: dict[str, tuple[Any, Any]]
    ) -> None:
        """Forward a player state update to related players (groups, sync parent, protocols)."""
        # TODO: make this fan-out change-aware (skip relatives that derive nothing from
        # the changed fields) once reverse indexes for synced_to/active_group exist.
        # Propagate group or sync-leader updates to child players.
        if player.state.group_members:
            for child_player in self.iter_group_members(player, exclude_self=True):
                if player.type == PlayerType.GROUP:
                    child_player.on_group_updated(player, changed_values)
                else:
                    child_player.on_sync_parent_updated(player, changed_values)
        # update/signal group player(s) when a member updates. A sync leader is a member of the
        # group player that formed the sync group and gaining members of its own does not change
        # that: a group player mirrors its leader, so it depends on exactly these updates.
        for group_player in self._get_player_groups(player):
            group_player.on_group_member_updated(player, changed_values)

        # update/signal manually sync-parent player when child updates
        if (_sync_parent_id := player.state.synced_to) and (
            _sync_parent := self.get_player(_sync_parent_id)
        ):
            self.trigger_player_update(_sync_parent.player_id)
        # If this is a protocol player, forward the state update to the parent player
        if (
            player.type == PlayerType.PROTOCOL
            and player.protocol_parent_id
            and (_protocol_parent := self.mass.players.get_player(player.protocol_parent_id))
        ):
            _protocol_parent.on_protocol_player_updated(player, changed_values)
        # If this is a parent player with linked protocols, forward state updates
        # to linked protocol players so their state reflects parent dependencies
        if player.state.type != PlayerType.PROTOCOL and player.linked_output_protocols:
            for linked in player.linked_output_protocols:
                if protocol_player := self.mass.players.get_player(linked.output_protocol_id):
                    protocol_player.on_protocol_parent_updated(player, changed_values)

    def _invalidate_group_volume_snapshot(self, player_id: str) -> None:
        """Clear the cached group volume snapshot for all groups this player belongs to."""
        player = self.get_player(player_id)
        if not player:
            return
        if player.state.group_members:
            player.extra_data.pop(ATTR_GROUP_VOLUME_SNAPSHOT, None)
        for group_player in self._get_player_groups(player):
            group_player.extra_data.pop(ATTR_GROUP_VOLUME_SNAPSHOT, None)
        if player.state.synced_to and (leader := self.get_player(player.state.synced_to)):
            leader.extra_data.pop(ATTR_GROUP_VOLUME_SNAPSHOT, None)

    def _record_volume_target(self, player: Player, volume_level: int) -> None:
        """Remember the volume level just commanded, as the base for the next nudge."""
        if self._stays_silent_on_volume_change(player):
            volume_level = 0
        player.extra_data[ATTR_VOLUME_TARGET] = (volume_level, time.monotonic())

    def _volume_nudge_base(self, player: Player) -> int | None:
        """Return the volume level a volume nudge for the given player steps from."""
        target = self._unexpired_volume_target(player)
        if target is not None:
            return target
        return player.state.volume_level

    def _group_volume_nudge_base(self, group_player: Player) -> int | None:
        """Return the volume level a group volume nudge for the given group steps from."""
        if not group_player.state.group_members:
            # an ungrouped player is stepped through its own volume, so it is that
            # volume the command lands on and that a following nudge steps from
            return self._volume_nudge_base(group_player)
        # mirrors Player.group_volume, but steps from the level last commanded to each
        # member instead of the level it reports, so the group is not held back by a
        # member that has not confirmed the previous nudge yet
        base: int | None = None
        for child_player in self.iter_group_members(
            group_player, only_powered=True, exclude_self=group_player.type != PlayerType.PLAYER
        ):
            if child_player.state.volume_control == PLAYER_CONTROL_NONE:
                continue
            if (child_volume := self._volume_nudge_base(child_player)) is None:
                continue
            if base is None or child_volume > base:
                base = child_volume
        return base

    def _unexpired_volume_target(self, player: Player) -> int | None:
        """Return the volume level last commanded, or None once it is too old to trust."""
        if (target := player.extra_data.get(ATTR_VOLUME_TARGET)) is None:
            return None
        volume_level, issued_at = target
        if time.monotonic() - issued_at < VOLUME_TARGET_EXPIRY:
            return cast("int", volume_level)
        del player.extra_data[ATTR_VOLUME_TARGET]
        return None

    def _dispatch_state_update_subscribers(
        self, player: Player, changed_values: dict[str, tuple[Any, Any]]
    ) -> None:
        """Notify all internal subscribers of a player state update."""
        for subscriber in list(self._state_update_subscribers):
            try:
                subscriber(player, changed_values)
            except Exception:
                self.logger.exception(
                    "Error in player state update subscriber for %s", player.player_id
                )

    async def _wait_for_playback_state(
        self,
        player: Player,
        wanted_state: PlaybackState,
        timeout: float,
        minimal_time: float = 0,
    ) -> None:
        """Wait for a player to reach a playback state, with optional minimum wait time."""
        start_timestamp = time.time()
        async with self.wait_for_player_update(
            player.player_id,
            attribute_name="playback_state",
            attribute_value=wanted_state,
            timeout=timeout,
        ):
            pass
        elapsed = time.time() - start_timestamp
        if elapsed < minimal_time:
            await asyncio.sleep(minimal_time - elapsed)

    async def _clear_active_output_protocol_when_idle(self, player: Player) -> None:
        """Wait for the player to stop playing, then clear its active output protocol."""
        await self._wait_for_playback_state(player, PlaybackState.IDLE, timeout=10)
        player.set_active_output_protocol(None)

    def _handle_membership_cleanup_on_state_change(
        self, player: Player, changed_values: dict[str, tuple[Any, Any]]
    ) -> None:
        """Detach a player from its (sync)groups when a state change requires it."""
        # A player that became unavailable or disabled can no longer be commanded,
        # so we drop it from its parent group/leader directly.
        became_inactive = (
            ATTR_AVAILABLE in changed_values and changed_values[ATTR_AVAILABLE][1] is False
        ) or (ATTR_ENABLED in changed_values and changed_values[ATTR_ENABLED][1] is False)
        if became_inactive and (player.state.active_group or player.state.synced_to):
            self.mass.create_task(self._cleanup_player_memberships(player.player_id))

        # A player whose power was turned off outside of an MA power command (e.g. its
        # linked power control was switched off directly) must be unsynced too. We act
        # only on an explicit on->off transition, leaving players without power control
        # (powered == None) untouched. The player is still reachable here, so we route
        # through cmd_ungroup which also transfers leadership when it is a sync leader.
        if (
            changed_values.get(ATTR_POWERED) == (True, False)
            and player.state.type == PlayerType.PLAYER
            and (player.state.synced_to or player.state.active_group or player.state.group_members)
        ):
            self.mass.create_task(self.cmd_ungroup(player.player_id))

    async def _cleanup_player_memberships(self, player_id: str) -> None:
        """Ensure a player is detached from any groups or syncgroups."""
        if not (player := self.get_player(player_id)):
            return
        with suppress(UnsupportedFeaturedException, PlayerCommandFailed, PlayerUnavailableError):
            if parent_id := (player.state.active_group or player.state.synced_to):
                # the player is part of a (permanent) groupplayer and the user tries to ungroup
                if parent_player := self.get_player(parent_id):
                    await self._handle_set_members(parent_player, player_ids_to_remove=[player_id])
                return

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

    def _get_active_audio_source(self, player: Player) -> tuple[AudioSource, PluginProvider] | None:
        """
        Return the live AudioSource a player is playing, and its owning PluginProvider.

        A player hearing its group's or sync leader's audio is playing that player's
        source, so the owner is resolved the same way its active queue is. Returns
        None when no source is playing on it, or when the owning plugin is gone.

        :param player: The player whose source to resolve.
        """
        return self.get_player_audio_source(self._audio_source_owner(player).player_id)

    def _audio_source_owner(self, player: Player) -> Player:
        """
        Return the player whose source the given player is hearing.

        Mirrors ``get_active_queue``: a sync child hears its leader, a group member
        hears its group, and a protocol player hears its parent.

        :param player: The player to resolve the owner for.
        """
        if player.state.synced_to and player.state.synced_to != player.player_id:
            if sync_leader := self.get_player(player.state.synced_to):
                return self._audio_source_owner(sync_leader)
        if player.state.active_group and player.state.active_group != player.player_id:
            if group_player := self.get_player(player.state.active_group):
                return self._audio_source_owner(group_player)
        if player.type == PlayerType.PROTOCOL and player.protocol_parent_id:
            if parent_player := self.get_player(player.protocol_parent_id):
                return self._audio_source_owner(parent_player)
        return player

    def _get_player_groups(self, player: Player) -> Iterator[Player]:
        """
        Return all group players the given player is a member of.

        :param player: The player to look up the group memberships for.
        """
        # A group player mirrors its members, so it is also included while unavailable -
        # skipping it there is exactly how its state goes stale.
        player_id = player.player_id
        for _player in self.iter_players():
            if _player.player_id == player_id:
                continue
            if _player.state.type != PlayerType.GROUP:
                continue
            if player_id in _player.state.group_members:
                yield _player

    # Protocol linking methods are provided by ProtocolLinkingMixin (protocol_linking.py)

    def _repair_protocol_parent_links(self) -> None:
        """
        Repair protocol parent links in player configs on startup.

        Scans player configs with a protocol_parent_id set and clears parent_ids
        that point to player configs that no longer exist (e.g., deleted universal
        players). A valid parent link also proves the player is a protocol child,
        so a stale player_type (left behind by an aborted registration) is healed.
        """
        all_player_configs = self.mass.config.get(CONF_PLAYERS, {})
        for player_id, player_config in all_player_configs.items():
            values = player_config.get("values") or {}
            parent_id = values.get(CONF_PROTOCOL_PARENT_ID)
            if not parent_id:
                continue
            # Check if parent config still exists
            parent_config = all_player_configs.get(parent_id)
            if not parent_config:
                self.logger.debug(
                    "Clearing stale protocol_parent_id %s for %s (parent config deleted)",
                    parent_id,
                    player_id,
                )
                conf_key = f"{CONF_PLAYERS}/{player_id}/values/{CONF_PROTOCOL_PARENT_ID}"
                self.mass.config.set(conf_key, None)
                continue
            if player_config.get("player_type") != PlayerType.PROTOCOL.value:
                self.logger.info(
                    "Repairing player type of %s - linked as protocol child of %s",
                    player_id,
                    parent_id,
                )
                self.mass.config.set_player_type(player_id, PlayerType.PROTOCOL)

    async def _fix_group_member_configs(self) -> None:
        """
        Fix stale protocol player IDs in sync group member configs.

        When a sync group references a protocol player ID instead of
        the parent player ID, correct it using the cached protocol parent mapping.
        """
        all_player_configs = self.mass.config.get(CONF_PLAYERS, {})
        total_fixes = 0
        fixed_groups: list[str] = []

        for group_id, group_config in list(all_player_configs.items()):
            if group_config.get("provider") != "sync_group":
                continue
            old_members: list[str] = group_config.get("values", {}).get(CONF_GROUP_MEMBERS, [])
            if not old_members:
                continue

            new_members: list[str] = []
            changes = 0
            for member_id in old_members:
                parent_id = self._get_cached_protocol_parent_id(member_id)
                corrected_id = parent_id or member_id
                if corrected_id != member_id:
                    changes += 1
                    self.logger.debug(
                        "Sync group %s: corrected member %s -> %s",
                        group_id,
                        member_id,
                        corrected_id,
                    )
                if corrected_id not in new_members:
                    new_members.append(corrected_id)

            if changes:
                self.mass.config.set_raw_player_config_value(
                    group_id, CONF_GROUP_MEMBERS, new_members
                )
                total_fixes += changes
                fixed_groups.append(group_id)

        for group_id in fixed_groups:
            if (group_player := self.get_player(group_id)) and group_player.available:
                await group_player.on_config_updated()

        if total_fixes:
            self.logger.info(
                "Fixed %d stale member reference(s) across %d sync group(s)",
                total_fixes,
                len(fixed_groups),
            )

    async def _poll_players(self) -> None:
        """Background task that polls players for updates."""
        while True:
            for player in list(self._players.values()):
                # if the player is playing, update elapsed time every tick
                # to ensure the queue has accurate details
                player_playing = player.state.playback_state == PlaybackState.PLAYING
                if player_playing and player.type != PlayerType.PROTOCOL:
                    self.mass.call_later(
                        0.5,
                        self.mass.player_queues.on_player_update,
                        player,
                        {"corrected_elapsed_time": (None, player.state.corrected_elapsed_time)},
                        task_id=f"queue_on_player_update_{player.player_id}",
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

    def _handle_group_dsp_change(
        self, player: Player, prev_group_members: list[str], new_group_members: list[str]
    ) -> None:
        """Handle DSP reload when group membership changes."""
        # reset cached group volume snapshot since membership changed
        player.extra_data.pop(ATTR_GROUP_VOLUME_SNAPSHOT, None)
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

    def _check_external_source_takeover(self, player: Player) -> None:
        """
        Handle when an external source takes over playback on a player.

        When a player has an active grouped output protocol (e.g., AirPlay group) and
        an external source (e.g., Spotify Connect, TV input) takes over playback,
        we need to clear the active output protocol and ungroup the protocol players.

        This prevents the situation where the player appears grouped via protocol
        but is actually playing from a different source.

        :param player: The player whose active_source changed.
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

        new_source = player.state.active_source

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

        if (
            new_source
            and new_source.lower() in ("airplay", "cast", "chromecast", "network")
            and protocol_player.provider.domain.lower() == "sendspin"
        ):
            # Special case for Sendspin bridge: if the new source matches cast or airplay and the
            # active protocol is Sendspin, we consider this a normal behavior and not a takeover
            return

        # Confirmed external source takeover
        self.logger.info(
            "External source '%s' took over on %s while playing via protocol %s - "
            "clearing active output protocol and ungrouping",
            new_source,
            player.display_name,
            protocol_player.provider.domain,
        )

        # Set active output protocol to native
        player.set_active_output_protocol("native")

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
        return self.mass.player_queues.get(source) is not None

    def _schedule_update_all_players(self, delay: float = 2.0) -> None:
        """
        Schedule a debounced update of all players' state.

        Used when a new player is registered to ensure all existing players
        update their dynamic properties (like can_group_with) that may have changed.

        :param delay: Delay in seconds before triggering updates (default 2.0).
        """
        if self.mass.closing:
            return

        for player in self.all_players(
            return_unavailable=True,
            return_disabled=False,
            return_protocol_players=True,
        ):
            self.trigger_player_update(player.player_id, debounce_delay=delay)

    async def _auto_ungroup_if_synced(self, player: Player, log_context: str) -> None:
        """
        Automatically ungroup a player if it's synced to another player.

        :param player: The player to check and potentially ungroup.
        :param log_context: Additional context for the log message (e.g., target player name).
        """
        if not player.state.synced_to and not player.state.active_group:
            return
        self.logger.info(
            "Player %s is already synced to %s, ungrouping it first before %s",
            player.name,
            player.state.synced_to or player.state.active_group,
            log_context,
        )
        # Use internal _handle_set_members to avoid deadlocking on the play lock
        # (we're already inside a cmd_set_members chain that holds a play lock).
        synced_to = player.state.synced_to or player.state.active_group
        if synced_to and (parent := self.get_player(synced_to)):
            try:
                async with self.wait_for_player_update(player.player_id, timeout=5):
                    await self._handle_set_members(parent, player_ids_to_remove=[player.player_id])
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.warning(
                    "Failed to auto-ungroup %s from %s, proceeding anyway",
                    player.name,
                    synced_to,
                )

    async def _handle_set_members(
        self,
        parent_player: Player,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """
        Handle the actual set_members logic.

        Skips permission checks and locking (internal use only).

        :param parent_player: The parent player to add/remove members to/from.
        :param player_ids_to_add: List of player_id's to add to the parent player.
        :param player_ids_to_remove: List of player_id's to remove from the parent player.
        """
        target_player = parent_player.player_id
        # handle the sync leader being removed from itself: either transfer leadership
        # to a remaining member (keeping playback alive) or dissolve the group entirely
        should_stop = False
        if player_ids_to_remove and target_player in player_ids_to_remove:
            remaining_members = [
                m
                for m in parent_player.state.group_members
                if m != target_player
                and m not in player_ids_to_remove
                and (member := self.get_player(m))
                and member.state.available
            ]
            active_queue = self.get_active_queue(parent_player)
            if remaining_members and active_queue and active_queue.state != PlaybackState.IDLE:
                # transfer leadership to a remaining member instead of dissolving
                await self._transfer_ad_hoc_leadership(parent_player, remaining_members)
                return
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

            # also skip if the child is part of this group via its sync leader
            # (e.g. synced to the sync leader of this syncgroup)
            if (
                child_player.state.active_group == target_player
                and child_player_id in parent_player.state.group_members
            ):
                continue

            # handle edge case: child player is synced to a different player
            # automatically ungroup it first and wait for state to propagate
            # but not if the child is already part of this group (via its sync leader)
            if child_player.state.synced_to and target_player not in {
                child_player.state.synced_to,
                child_player.state.active_group,
            }:
                await self._auto_ungroup_if_synced(child_player, f"joining {parent_player.name}")

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
                if child_player_id in parent_player.state.group_members:
                    final_player_ids_to_remove.append(child_player_id)
                    continue
                # also accept the removal if the child player itself reports
                # being synced to this parent - handles race conditions where the
                # parent's group_members state is stale/not yet updated
                child_player = self.get_player(child_player_id)
                if child_player and child_player.state.synced_to == target_player:
                    final_player_ids_to_remove.append(child_player_id)
                    continue

        # Forward command to the appropriate player after all (base) sanity checks
        # GROUP players (sync_group, universal_group) manage their own members internally
        # and don't need protocol translation - call their set_members directly
        if (
            parent_player.type == PlayerType.GROUP
            and PlayerFeature.SET_MEMBERS in parent_player.state.supported_features
        ):
            await parent_player.set_members(
                player_ids_to_add=final_player_ids_to_add,
                player_ids_to_remove=final_player_ids_to_remove,
            )
            return
        # For regular players, handle protocol selection and translation
        await self._handle_set_members_with_protocols(
            parent_player, final_player_ids_to_add, final_player_ids_to_remove
        )

        if should_stop:
            # Stop playback on the player if it is being removed from itself
            await self._handle_cmd_stop(parent_player.player_id)

    async def _handle_set_members_with_protocols(
        self,
        parent_player: Player,
        player_ids_to_add: list[str],
        player_ids_to_remove: list[str],
    ) -> None:
        """
        Handle set_members considering protocol and native members.

        Skips permission checks, locking, and all redirect logic (internal use only).
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
                if PlayerFeature.SET_MEMBERS not in parent_player.state.supported_features:
                    return
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

    async def _transfer_ad_hoc_leadership(
        self, leader: Player, remaining_members: list[str]
    ) -> None:
        """
        Transfer leadership of an ad-hoc sync group to a remaining member.

        Called when the sync leader of an ad-hoc group is unjoined while other
        members remain and playback is active. The queue is moved to a newly
        selected leader, the remaining members are regrouped under it and playback
        resumes at the saved position (accepting a brief audio gap).

        :param leader: The current sync leader being removed from the group.
        :param remaining_members: Available group members (excluding the leader)
            that should keep playing under a new leader.
        """
        active_queue = self.get_active_queue(leader)
        was_playing = active_queue is not None and active_queue.state == PlaybackState.PLAYING
        new_leader_id = self._select_ad_hoc_leader(leader, remaining_members)
        self.logger.info(
            "Transferring leadership of %s to %s (%s remaining member(s))",
            leader.name,
            new_leader_id,
            len(remaining_members),
        )
        # Move the queue to the new leader. transfer_queue frees the new leader from
        # the old leader's group and stops the old leader; the playback position
        # survives because stop() stores it in resume_pos.
        await self.mass.player_queues.transfer_queue(
            leader.player_id, new_leader_id, auto_play=False
        )
        # regroup the other remaining members under the new leader
        other_members = [m for m in remaining_members if m != new_leader_id]
        if other_members:
            await self.cmd_set_members(new_leader_id, player_ids_to_add=other_members)
        if was_playing:
            await self.mass.player_queues.resume(new_leader_id)

    def _select_ad_hoc_leader(self, leader: Player, remaining_members: list[str]) -> str:
        """
        Pick the new leader for an ad-hoc sync group leadership transfer.

        Prefers a remaining member that can currently be reached on the protocol the
        group is playing on, so the other members can be regrouped under it; falls back
        to the first remaining member. The members' own ``can_group_with`` is unusable
        here because it is empty while they are still synced to the old leader.

        :param leader: The current sync leader being removed.
        :param remaining_members: Candidate member player_ids, already filtered for
            availability. Must not be empty.
        """
        active_domain: str | None = None
        if leader.active_output_protocol and leader.active_output_protocol != "native":
            if protocol_player := self.get_player(leader.active_output_protocol):
                active_domain = protocol_player.provider.domain
        if active_domain:
            for member_id in remaining_members:
                member = self.get_player(member_id)
                if member is None:
                    continue
                if active_domain in member.playback_domains:
                    return member_id
        return remaining_members[0]

    def _clear_sleep_timer(self, player: Player) -> None:
        """
        Clear the active sleep timer for the player.

        :param player: Player to clear the timer for.
        """
        self.mass.cancel_timer(self._sleep_timer_task_id(player.player_id))
        if player.sleep_timer_expires_at is not None:
            player.set_sleep_timer_expires_at(None)
            player.update_state()
            self._signal_sleep_timer_updated(player, None)

    async def _handle_sleep_timer_expired(self, player_id: str) -> None:
        """
        Stop playback when a player's sleep timer expires.

        :param player_id: Player ID whose sleep timer expired.
        """
        player = self.get_player(player_id)
        if player is None or player.sleep_timer_expires_at is None:
            return
        player.set_sleep_timer_expires_at(None)
        player.update_state()
        self._signal_sleep_timer_updated(player, None)
        await self.cmd_stop(player_id)

    def _signal_sleep_timer_updated(self, player: Player, expires_at: float | None) -> None:
        """
        Signal a sleep timer change for the player on the event bus.

        :param player: Player whose sleep timer changed.
        :param expires_at: New expiry timestamp, or None when the timer was cleared.
        """
        if player.state.type == PlayerType.PROTOCOL:
            return
        self.mass.signal_event(
            EventType.PLAYER_SLEEP_TIMER_UPDATED,
            object_id=player.player_id,
            data=expires_at,
        )

    @staticmethod
    def _sleep_timer_task_id(player_id: str) -> str:
        """
        Return the scheduled task ID for a player's sleep timer.

        :param player_id: Player ID to build the task ID for.
        """
        return f"player_sleep_timer_{player_id}"

    # Private command handlers (no permission checks)

    async def _handle_cmd_resume(
        self, player_id: str, source: str | None = None, media: PlayerMedia | None = None
    ) -> None:
        """
        Handle resume playback command.

        Skips permission checks and locking (internal use only).
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
            and PlayerFeature.PAUSE in player.state.supported_features
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
        # fallback: just try to resume queue playback
        await self.mass.player_queues.resume(player.player_id)

    async def _handle_cmd_power(
        self, player_id: str, powered: bool, skip_auto_play: bool = False
    ) -> None:
        """
        Handle player power on/off command.

        Skips permission checks and locking (internal use only).

        :param player_id: The player ID to power on/off.
        :param powered: True to power on, False to power off.
        :param skip_auto_play: If True, skip auto-play on power on.
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
        player_was_sync_child = bool(player.state.synced_to or player.state.active_group)
        if (
            (player_was_sync_child or player.group_members)
            and player.type == PlayerType.PLAYER
            and not powered
        ):
            # ungroup player if it is synced (or is a sync leader itself)
            await self.cmd_ungroup(player_id)

        # always stop player at power off
        if (
            not powered
            and not player_was_sync_child
            and player_state.playback_state in (PlaybackState.PLAYING, PlaybackState.PAUSED)
        ):
            # wait for the stop command to process and prevent race conditions
            async with self.wait_for_player_update(player_id, timeout=5):
                await self._handle_cmd_stop(player_id)

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
            if powered:
                await wait_for_power_on(self.logger, player)
        elif player_state.power_control == PLAYER_CONTROL_FAKE:
            # user wants to use fake power control - so we (optimistically) update the state
            # and store the state in the cache
            player.extra_data[ATTR_FAKE_POWER] = powered
            # Group players need to actively form/dissolve their session when the
            # user toggles fake power — otherwise the toggle would only update the
            # cosmetic state without ever capturing or releasing the members.
            if player_state.type == PlayerType.GROUP:
                await player.power(powered)
            player.update_state()  # trigger update of the player state
            if player_state.type != PlayerType.GROUP:
                # see register(): group fake-power is intentionally not persisted
                # because there is no session to restore at boot.
                await self.mass.cache.set(
                    key=player_id,
                    data=powered,
                    provider=self.domain,
                    category=CACHE_CATEGORY_PLAYER_POWER,
                )
        # handle external player control
        elif player_control := self._controls.get(player.state.power_control):
            control_name = player_control.name
            self.logger.debug("Redirecting power command to PlayerControl %s", control_name)
            if not player_control.supports_power:
                raise UnsupportedFeaturedException(
                    f"Player control {control_name} is not available"
                )
            if powered:
                assert player_control.power_on is not None  # for type checking
                await player_control.power_on()
                await wait_for_power_on(self.logger, player, player_control)
            else:
                assert player_control.power_off is not None  # for type checking
                await player_control.power_off()
        # always trigger a state update to update the UI
        player.refresh_state()

        # handle 'auto play on power on' feature
        if (
            not skip_auto_play
            and not player_state.active_group
            and not player_state.synced_to
            and powered
            and player.config.get_value(CONF_AUTO_PLAY)
            and player_state.active_source in (None, player_id)
            and not player.extra_data.get(ATTR_ANNOUNCEMENT_IN_PROGRESS)
        ):
            await self.mass.player_queues.resume(player_id)

    def _resolve_group_volume_player(self, player: Player) -> Player | None:
        """
        Return the player whose group a group volume command applies to.

        Returns None if the given player is not grouped at all. Commands addressed to a
        synced member and to its sync leader resolve to the same player, so they read
        and guard one and the same group.

        :param player: The player the command was addressed to.
        """
        # the group volume lock this resolves to may not share the VOLUME purpose:
        # set_group_volume sets the volume of the members concurrently and a sync leader
        # is a member of its own group, so a group command would wait on its own lock.
        if player.state.type == PlayerType.GROUP or player.state.group_members:
            # dedicated group player or sync leader
            return player
        if player.state.synced_to:
            # a synced player follows its sync leader
            return self.get_player(player.state.synced_to)
        return None

    async def _set_member_volume(self, player_id: str, volume_level: int) -> None:
        """
        Set the volume of a single member as part of a group volume change.

        :param player_id: player_id of the member to handle the command.
        :param volume_level: logical volume level (0..100) to set on the member.
        """
        # record before waiting for the lock, for the same reason as cmd_volume_set
        if member := self.get_player(player_id):
            self._record_volume_target(member, volume_level)
        # take the volume lock of the member itself, so a group volume change and an
        # individual volume command for that member can not overtake one another
        async with self.get_player_lock(player_id, PlayerLockPurpose.VOLUME):
            await self._handle_cmd_volume_set(player_id, volume_level, record_target=False)

    async def _handle_cmd_volume_set(
        self, player_id: str, volume_level: int, *, record_target: bool = True
    ) -> None:
        """
        Handle Player volume set command.

        Skips permission checks and locking (internal use only).

        :param player_id: player_id of the player to handle the command.
        :param volume_level: logical volume level (0..100) to set on the player.
        :param record_target: Set to False when the caller already recorded the level as
            the base for the next volume nudge, before it waited for the volume lock.
        """
        player = self.get_player(player_id, True)
        assert player is not None  # for type checker

        # Clamp logical volume to 0-100
        volume_level = max(0, min(100, volume_level))

        if player.type == PlayerType.GROUP:
            # redirect to special group volume control
            await self.cmd_group_volume(player_id, volume_level)
            return

        # A muted player stays muted: only an explicit unmute lifts it, and the level
        # set here is the one it plays at once that happens. Fake mute is the exception,
        # because it is simulated with the volume itself.
        if self._stays_silent_on_volume_change(player):
            # a locked player stays silent, the volume it holds is the one
            # that gets restored once it is unmuted again
            volume_level = 0
            # the lock may have been earned after the caller recorded the level it asked
            # for, which is then not the level this player ends up at
            record_target = True
        else:
            player.extra_data.pop(ATTR_FAKE_MUTE, None)

        if record_target:
            self._record_volume_target(player, volume_level)

        # Scale logical volume (0-100) to device volume (min_volume-max_volume)
        device_volume = self.scale_volume_to_device(player_id, volume_level)

        await self._notify_source_volume_change(player, volume_level)

        # Handle native volume control support
        if player.volume_control == PLAYER_CONTROL_NATIVE:
            # player supports volume command natively: forward to player
            await player.volume_set(device_volume)
            return
        # Handle fake volume control support
        if player.volume_control == PLAYER_CONTROL_FAKE:
            # user wants to use fake volume control - so we (optimistically) update the state
            # and store the state in the cache. Fake volume uses the logical volume (no scaling).
            player.extra_data[ATTR_FAKE_VOLUME] = volume_level
            player.update_state()
            return
        # player has no volume support at all
        if player.volume_control == PLAYER_CONTROL_NONE:
            raise UnsupportedFeaturedException(
                f"Player {player.state.name} does not support volume control"
            )
        # handle external player control
        if player_control := self._controls.get(player.state.volume_control):
            control_name = player_control.name
            self.logger.debug("Redirecting volume command to PlayerControl %s", control_name)
            if not player_control.supports_volume:
                raise UnsupportedFeaturedException(
                    f"Player control {control_name} is not available"
                )
            assert player_control.volume_set is not None
            # forward the already-scaled device volume; the external control sets the
            # raw device volume and does not apply min/max scaling of its own
            await player_control.volume_set(device_volume)
            return
        if protocol_player := self.get_player(player.state.volume_control):
            # forward the already-scaled device volume: the limits configured on this
            # (user-facing) player are the only ones that apply to the command
            self.logger.debug(
                "Redirecting volume command to protocol player %s",
                protocol_player.provider.manifest.name,
            )
            await protocol_player.volume_set(device_volume)
            return

    @staticmethod
    def _is_in_group(state: PlayerState) -> bool:
        """Check if the player with the given state is currently grouped with other players."""
        # a sync leader has neither synced_to nor active_group set, but it does lead its
        # own group_members, which stays empty for a player that is not grouped at all
        return bool(state.synced_to or state.active_group or state.group_members)

    def _has_active_mute_lock(self, player: Player) -> bool:
        """
        Check if the given player holds a mute lock that still applies to it.

        A lock is only earned inside a group and only holds for as long as the player
        is still grouped, so it can not outlive the group it was earned in.

        :param player: The player to check, which may be a protocol player.
        """
        if player.extra_data.get(ATTR_MUTE_LOCK) and self._is_in_group(player.state):
            return True
        # cmd_volume_mute stores the lock on the parent player, while the volume command
        # may arrive with the protocol player ID (e.g. during group volume changes)
        if player.protocol_parent_id and (parent := self.get_player(player.protocol_parent_id)):
            return bool(parent.extra_data.get(ATTR_MUTE_LOCK)) and self._is_in_group(parent.state)
        return False

    def _stays_silent_on_volume_change(self, player: Player) -> bool:
        """Check if a volume command for the given player lands at 0 to keep it silent."""
        return (
            self._has_active_mute_lock(player)
            and player.mute_control == PLAYER_CONTROL_FAKE
            and bool(player.extra_data.get(ATTR_FAKE_MUTE))
        )

    async def _mute_group_members(self, group_player: Player, muted: bool) -> None:
        """
        Mute or unmute all mute capable members of a player group or synced players.

        :param group_player: The group player or sync leader.
        :param muted: bool if the group should be muted.
        """
        coros = []
        for child_player in self.iter_group_members(
            group_player, only_powered=True, exclude_self=False
        ):
            if child_player.mute_control == PLAYER_CONTROL_NONE:
                # members without a mute control are left alone, just like the
                # group mute state itself is calculated from the capable members only
                continue
            coros.append(self.cmd_volume_mute(child_player.player_id, muted))
        await asyncio.gather(*coros)

    async def _handle_cmd_volume_mute(self, player: Player, mute_control: str, muted: bool) -> None:
        """
        Send the mute command to the given player's mute control.

        Skips permission checks, locking and mute lock bookkeeping (internal use only).

        :param player: the player to handle the command.
        :param mute_control: the already resolved mute control of the player.
        :param muted: bool if player should be muted.
        """
        if mute_control == PLAYER_CONTROL_NATIVE:
            # player supports mute command natively: forward to player
            await player.volume_mute(muted)
            return
        if mute_control == PLAYER_CONTROL_FAKE:
            # user wants to use fake mute control - so we use volume instead
            self.logger.debug(
                "Using volume for muting for player %s",
                player.state.name,
            )
            if muted:
                already_muted = bool(player.extra_data.get(ATTR_FAKE_MUTE))
                if not already_muted:
                    # on a repeated mute command the volume is already 0
                    player.extra_data[ATTR_PREVIOUS_VOLUME] = player.state.volume_level
                await self._handle_cmd_volume_set(player.player_id, 0)
                # set the flag after the volume command, as that clears it
                player.extra_data[ATTR_FAKE_MUTE] = True
                player.update_state()
            else:
                was_muted = bool(player.extra_data.get(ATTR_FAKE_MUTE))
                player.extra_data[ATTR_FAKE_MUTE] = False
                player.update_state()
                if not was_muted:
                    # the volume is the one the user is listening at, restoring
                    # anything here would turn a no-op unmute into a volume change
                    return
                stored_volume: int | None = player.extra_data.pop(ATTR_PREVIOUS_VOLUME, None)
                # the volume was still unknown at mute time, so pick a low volume
                # rather than blasting the speaker at some assumed level
                await self._handle_cmd_volume_set(
                    player.player_id, 1 if stored_volume is None else stored_volume
                )
            return

        # handle external player control
        if player_control := self._controls.get(mute_control):
            control_name = player_control.name
            self.logger.debug("Redirecting mute command to PlayerControl %s", control_name)
            if not player_control.supports_mute:
                raise UnsupportedFeaturedException(
                    f"Player control {control_name} is not available"
                )
            assert player_control.mute_set is not None
            await player_control.mute_set(muted)
            return

        # handle to protocol player as volume_mute control
        if protocol_player := self.get_player(mute_control):
            self.logger.debug(
                "Redirecting mute command to protocol player %s",
                protocol_player.provider.manifest.name,
            )
            await protocol_player.volume_mute(muted)
            return

        # the configured control disappeared after the mute control was resolved
        raise UnsupportedFeaturedException(f"Player {player.state.name} does not support muting")

    async def _handle_play_media(self, player_id: str, media: PlayerMedia) -> None:
        """
        Handle play media command without group redirect.

        Skips permission checks, locking, and all redirect logic (internal use only).

        :param player_id: player_id of the player to handle the command.
        :param media: The Media that needs to be played on the player.
        """
        player = self.get_player(player_id, raise_unavailable=True)
        assert player is not None
        # media that is not the live source itself takes the player away from it. An
        # announcement is the exception: it interrupts the player and hands it straight
        # back, so releasing the source would tear down a session that is about to
        # resume — and one that cannot be re-selected once its plugin has let go.
        if media.media_type not in (MediaType.AUDIO_SOURCE, MediaType.ANNOUNCEMENT):
            await self._release_audio_source(player_id)
        # set active source if media has a source_id (e.g. plugin source or mass queue source)
        if media.source_id:
            player.set_active_mass_source(media.source_id)

        # Determine output protocol to use:
        # While a session is active (playing/paused), keep using the already active
        # protocol so mid-session commands stay on the same output.
        # On a fresh start always (re)select: a leftover active protocol from a
        # previous session must not overrule user preference, a grouped protocol
        # or native playback (and it may point at a player that is gone by now).
        target_player: Player | None = None
        output_protocol: OutputProtocol | None = None
        if (
            player.state.playback_state in (PlaybackState.PLAYING, PlaybackState.PAUSED)
            and player.active_output_protocol
            and player.active_output_protocol != "native"
            and (protocol_player := self.get_player(player.active_output_protocol))
        ):
            # Use the already-set protocol directly
            output_protocol = player.get_linked_protocol(player.active_output_protocol)
            if output_protocol is not None:
                target_player = protocol_player
        if target_player is None:
            target_player, output_protocol = self._select_best_output_protocol(player)

        if target_player.player_id != player.player_id:
            # Playing via linked protocol - update active output protocol
            # output_protocol is guaranteed to be non-None when target_player != player
            assert output_protocol is not None
            self.logger.debug(
                "Starting playback on %s via protocol %s (target=%s), group_members=%s",
                player.state.name,
                output_protocol.name,
                target_player.display_name,
                target_player.state.group_members,
            )
            player.set_active_output_protocol(output_protocol.output_protocol_id)
        elif player.type != PlayerType.GROUP:
            # Native playback - group players don't have output protocols of their own
            # (they delegate to a sync leader / member which manages its own protocol)
            self.logger.debug(
                "Starting playback on %s via native, group_members=%s",
                player.state.name,
                player.state.group_members,
            )
            player.set_active_output_protocol("native")

        # power on the player if needed (skip auto-play since we're about to start playback)
        if not player.state.powered and player.state.power_control != PLAYER_CONTROL_NONE:
            await self._handle_cmd_power(player.player_id, True, skip_auto_play=True)
        await target_player.play_media(media)
        if target_player.player_id != player.player_id:
            # notify the native player that protocol playback started
            assert output_protocol is not None
            await player.on_protocol_playback(output_protocol=output_protocol)

    async def _handle_enqueue_next_media(self, player_id: str, media: PlayerMedia) -> None:
        """
        Handle enqueue next media command without group redirect.

        Skips permission checks, locking, and all redirect logic (internal use only).

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

    async def _notify_source_volume_change(self, player: Player, volume_level: int) -> None:
        """
        Tell the source playing on a player that its volume changed.

        Only the player the source is actually playing on notifies, never one that
        merely hears it as a group member — otherwise a group volume change would
        fire the callback once per child, each with a different value.

        :param player: The player whose volume changed.
        :param volume_level: The new volume, 0-100.
        """
        if (session := self.get_audio_source_session(player.player_id)) is None:
            return
        provider = self.mass.get_provider(session.provider_instance_id)
        if not isinstance(provider, PluginProvider):
            return
        await provider.on_volume_change(session.source_id, volume_level)

    def _resolve_command_target(self, player: Player, source_id: str | None) -> str:
        """
        Return the source (id) a command issued to a player applies to.

        :param player: The player the command was issued to.
        :param source_id: The source the caller aimed the command at, if it named one.
        :return: The id of the player's active source, which is the id of Music
            Assistant's own queue when nothing else is playing on it.
        :raises PlayerCommandFailed: When the caller named a source that is no longer
            the one playing.
        """
        active_source_id = player.state.active_source or player.player_id
        if source_id is not None and source_id != active_source_id:
            msg = f"The source this was meant for is no longer playing on {player.state.name}."
            raise PlayerCommandFailed(msg)
        return active_source_id

    async def _forward_to_external_source(
        self,
        player: Player,
        action: SourceControl,
        value: SourceControlValue = None,
    ) -> bool:
        """
        Hand a control action to the external source playing on a player.

        Covers the external sources Music Assistant provides itself, which own a
        session it can talk to. A source belonging to the player (its line-in, TV
        input, or its own Spotify Connect) has no such session, so this reports that
        it did not take the action and the caller goes on to the player itself.

        The per-action transport flags gate what the source advertises it can do, so
        a client is refused rather than left waiting. Ordering is not gated here: the
        session decides what reordering means for its own content. Most sources do not
        implement it at all, though, so a client should ask the source whether it can
        before offering the control - handing it one that quietly does nothing is
        worse than not offering it.

        :param player: The player the action was issued to.
        :param action: The control action to hand over.
        :param value: The action's argument, where it takes one.
        :return: True when an external source took the action.
        """
        if (active := self._get_active_audio_source(player)) is None:
            return False
        audio_source, provider = active
        supported = {
            SourceControl.PLAY: audio_source.can_play_pause,
            SourceControl.PAUSE: audio_source.can_play_pause,
            SourceControl.SEEK: audio_source.can_seek,
            SourceControl.NEXT: audio_source.can_next_previous,
            SourceControl.PREVIOUS: audio_source.can_next_previous,
        }.get(action, True)
        if not supported:
            msg = (
                f"The active source ({audio_source.name}) on player "
                f"{player.display_name} does not support this action"
            )
            raise PlayerCommandFailed(msg)
        try:
            await provider.on_source_control(audio_source.item_id, action, value)
        except NotImplementedError as err:
            # a source with no control surface at all (vban_receiver) reaches the base
            # implementation; a caller deserves a refusal rather than a server error
            msg = (
                f"The active source ({audio_source.name}) on player "
                f"{player.display_name} can not be controlled"
            )
            raise PlayerCommandFailed(msg) from err
        return True

    async def _release_audio_source(self, player_id: str) -> None:
        """
        Let go of the live source a player was playing, if it had one.

        Tells the owning plugin so an upstream session still pointing at Music
        Assistant is released. A plugin that raises must not stop the player from
        moving on, so failures are logged rather than propagated.

        :param player_id: The player that is done with its source.
        """
        if (session := self._end_audio_source_session(player_id)) is None:
            return
        self.trigger_player_update(player_id)
        provider = self.mass.get_provider(session.provider_instance_id)
        if not isinstance(provider, PluginProvider):
            return
        try:
            await provider.on_source_released(session.source_id, player_id)
        except Exception:
            self.logger.warning(
                "on_source_released raised for provider %s source %s player %s",
                provider.instance_id,
                session.source_id,
                player_id,
                exc_info=True,
            )

    async def _release_unclaimed_audio_source(
        self, player_id: str, session: AudioSourceSession, playback_session_id: str
    ) -> None:
        """
        Release a source whose renderer never requested the stream.

        The play command returned without error, so the late-start release in the
        streams controller never fires: no stream request means no failed stream
        request either. Without this the player would keep publishing a source
        that never started, with its own queue held inactive behind it.

        :param player_id: The player the source was started on.
        :param session: The session that was started for it.
        :param playback_session_id: Playback session active when it was started.
        """
        current = self.get_audio_source_session(player_id)
        if (
            current is not session
            or current.playback_session_id != playback_session_id
            or current.stream_session_id is not None
        ):
            return
        self.logger.info(
            "AudioSource %s was never streamed by player %s, releasing it",
            session.source_id,
            player_id,
        )
        await self.deselect_source(
            player_id,
            provider_instance_id=session.provider_instance_id,
            source_id=session.source_id,
            playback_session_id=playback_session_id,
        )

    async def _resolve_audio_source_uri(
        self, source: str
    ) -> tuple[AudioSource, PluginProvider] | None:
        """
        Resolve a source string to a live AudioSource, if that is what it names.

        :param source: The source string a select names.
        :return: The source and its owning plugin, or None when the string names
            something else (a queue, a player-native source).
        """
        if "://" not in source:
            return None
        try:
            item = await self.mass.music.get_item_by_uri(source)
        except MusicAssistantError as err:
            # not resolvable as media, so it is something else (a queue id, a
            # player-native source) — logged because a provider being unavailable
            # or unauthenticated also lands here
            self.logger.debug("Could not resolve %s as an audio source: %s", source, err)
            return None
        if not isinstance(item, AudioSource):
            return None
        provider = self.mass.get_provider(item.provider)
        if not isinstance(provider, PluginProvider):
            return None
        if ProviderFeature.AUDIO_SOURCE not in provider.supported_features:
            return None
        return item, provider

    async def _start_audio_source(
        self, player: Player, audio_source: AudioSource, provider: PluginProvider
    ) -> None:
        """
        Start a live external source on a player.

        The player's queue is left exactly as it is: it simply stops being the
        active source, so it is still there to resume when the source ends.

        :param player: The player to play the source on.
        :param audio_source: The source that was selected.
        :param provider: The plugin exposing that source.
        """
        # a player outputs one source at a time, so another one already on it has to be
        # handed back first: replacing the session silently would leave its plugin
        # holding an upstream session that still points at this player
        if (current := self.get_audio_source_session(player.player_id)) is not None and (
            current.source_id != audio_source.item_id
            or current.provider_instance_id != provider.instance_id
        ):
            await self._release_audio_source(player.player_id)
        session = self._start_audio_source_session(
            player.player_id, audio_source, provider.instance_id
        )
        try:
            await self._handle_play_media(
                player.player_id,
                PlayerMedia(
                    uri=audio_source.uri or audio_source.item_id,
                    media_type=MediaType.AUDIO_SOURCE,
                    title=audio_source.name,
                    # the session's owner, which its stream url is keyed on
                    source_id=player.player_id,
                    queue_session_id=session.playback_session_id,
                ),
            )
        except Exception:
            # the source never started, so the player must not go on publishing it:
            # a session left behind holds the queue inactive with nothing playing it
            if self.get_audio_source_session(player.player_id) is session:
                await self._release_audio_source(player.player_id)
            raise
        # the play command returning does not mean the renderer ever fetched the
        # stream url: until a stream request claims the session nothing will evict
        # the player the source may be moving from, and nothing else would ever
        # clear a session that is never streamed
        self.mass.call_later(
            AUDIO_SOURCE_CLAIM_TIMEOUT,
            self._release_unclaimed_audio_source,
            player.player_id,
            session,
            session.playback_session_id,
            task_id=f"release_unclaimed_audio_source_{player.player_id}",
        )

    async def _handle_select_source(self, player_id: str, source: str | None) -> None:
        """
        Handle select source command without group redirect.

        Skips permission checks, locking, and all redirect logic (internal use only).

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
                # just try to stop (regardless of state) and let it settle, so the
                # new source does not race the teardown. A player that already
                # reports idle has nothing to tear down and does not wait at all.
                async with self.wait_for_player_update(
                    player_id,
                    attribute_name="playback_state",
                    attribute_value=PlaybackState.IDLE,
                    timeout=5,
                ):
                    await self._handle_cmd_stop(player_id)
        # an audio source uri selects the live source itself, which plays on the
        # player while its queue keeps its own items and goes inactive
        if (resolved := await self._resolve_audio_source_uri(source)) is not None:
            await self._start_audio_source(player, *resolved)
            return
        # anything else takes the player away from a live source it was playing
        await self._release_audio_source(player_id)
        # check if source is a mass queue
        # this can be used to restore the queue after a source switch
        if self.mass.player_queues.get(source):
            player.set_active_mass_source(source)
            return
        # Legacy compatibility: the old plugin-source API used the
        # plugin's instance_id directly as the source string. The refactor
        # moved plugin sources to first-class AudioSource MediaItems played
        # via player_queues.play_media. Translate a legacy plugin-instance-id
        # source into the new flow so old frontends, third-party scripts,
        # and HA automations keep working — but only when the provider
        # exposes EXACTLY ONE AudioSource (it was always a 1:1 mapping under
        # the old API; multi-source providers have to use the explicit URI).
        if (legacy_prov := self.mass.get_provider(source)) and isinstance(
            legacy_prov, PluginProvider
        ):
            if ProviderFeature.AUDIO_SOURCE not in legacy_prov.supported_features:
                raise PlayerCommandFailed(f"Provider {source} does not expose AudioSources")
            sources = await legacy_prov.get_audio_sources()
            if len(sources) == 1:
                self.logger.debug(
                    "Translating legacy select_source(%s) to play_media(%s)",
                    source,
                    sources[0].uri,
                )
                await self.mass.player_queues.play_media(player_id, str(sources[0].uri))
                return
            raise UnsupportedFeaturedException(
                f"Provider {source} exposes {len(sources)} AudioSources; the legacy "
                "select_source(plugin_instance_id) API only supported 1:1 mappings. "
                "Use player_queues.play_media with an explicit AudioSource URI."
            )
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

        Skips permission checks, locking, and all redirect logic (internal use only).

        :param player_id: player_id of the player to handle the command.
        """
        player = self.get_player(player_id, raise_unavailable=True)
        assert player is not None
        protocol_player: Player | None = None
        if player.active_output_protocol and player.active_output_protocol != "native":
            protocol_player = self.get_player(player.active_output_protocol)
        if player.state.playback_state == PlaybackState.IDLE:
            # The player already reports idle but an output protocol is still marked
            # active: the protocol player may never have received a stop at all
            # (e.g. the source stream ended on its own before this stop command
            # arrived). Forward an (idempotent) stop and schedule the protocol clear
            # so no stale session lingers on the device and the next playback
            # (re)selects the output protocol.
            if protocol_player is not None:
                await protocol_player.stop()
                if len(protocol_player.group_members) <= 1:
                    self.schedule_active_output_protocol_clear(player)
            return
        player.mark_stop_called()
        # Delegate to active protocol player if one is active
        target_player = player
        if protocol_player is not None:
            target_player = protocol_player
            if PlayerFeature.POWER in target_player.supported_features:
                # if protocol player supports/requires power,
                # we power it off instead of just stopping (which also stops playback)
                # this is rare as most protocols do not support power control (except for cast)
                await self._handle_cmd_power(target_player.player_id, False)
                return

        # handle command on player(protocol) directly
        await target_player.stop()
        # Only clear active protocol if the protocol player has no remaining group members.
        # If there are still protocol group members, keep the protocol active so that
        # when playback resumes it continues on the same protocol.
        if target_player.player_id == player.player_id or len(target_player.group_members) <= 1:
            self.schedule_active_output_protocol_clear(player)

    async def _handle_cmd_play(self, player_id: str) -> None:
        """
        Handle play command without group redirect.

        Skips permission checks, locking, and all redirect logic (internal use only).

        :param player_id: player_id of the player to handle the command.
        """
        player = self.get_player(player_id, raise_unavailable=True)
        assert player is not None
        if player.state.playback_state == PlaybackState.PLAYING:
            self.logger.info(
                "Ignore PLAY request to player %s: player is already playing", player.state.name
            )
            return
        # If an AudioSource is the active queue item, proxy play to the plugin
        if active := self._get_active_audio_source(player):
            audio_source, plugin_prov = active
            if audio_source.can_play_pause:
                await plugin_prov.on_source_control(audio_source.item_id, SourceControl.PLAY)
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
            # No active protocol target: if the player rendering the audio supports pause and
            # the active (external) source can be paused, unpause it directly instead of
            # restarting the source.
            output_player = player.resolve_output_player()
            if (
                active_source
                and active_source.can_play_pause
                and PlayerFeature.PAUSE in output_player.supported_features
            ):
                await output_player.play()
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

        Skips permission checks, locking, and all redirect logic (internal use only).

        :param player_id: player_id of the player to handle the command.
        """
        player = self.get_player(player_id, raise_unavailable=True)
        assert player is not None
        if player.state.playback_state == PlaybackState.IDLE:
            return
        # If an AudioSource is the active queue item, proxy pause to the plugin
        if active := self._get_active_audio_source(player):
            audio_source, plugin_prov = active
            if audio_source.can_play_pause:
                await plugin_prov.on_source_control(audio_source.item_id, SourceControl.PAUSE)
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
        if target_player := self._get_control_target(
            player, PlayerFeature.PAUSE, require_active=True
        ):
            await target_player.pause()
            return
        # No active protocol target: if the player rendering the audio supports pause and the
        # active (external) source can be paused, forward the command to it instead of stopping
        # it (mirrors the external-source handling in cmd_seek/cmd_next_track).
        output_player = player.resolve_output_player()
        if (
            active_source
            and active_source.can_play_pause
            and PlayerFeature.PAUSE in output_player.supported_features
        ):
            await output_player.pause()
            return
        # player/protocol does not support pause: fall back to stop
        self.logger.debug(
            "Player/protocol %s does not support pause, using STOP instead",
            player.state.name,
        )
        await self._handle_cmd_stop(player.player_id)
