"""
MusicAssistant PlayerController.

Handles all logic to control supported players,
whose devices are provided by Player Providers.

Note that the PlayerController has a concept of a 'player', a 'protocol' and a 'playerstate'.
The Player is what users and clients see, one per device, and is owned by this controller.
A protocol is the object a provider attaches to a player for its device: it
incorporates the (unaltered) state the device reports (e.g. volume, state, etc)
and the functions for controlling the device (e.g. play, pause, etc).

The playerstate is the (final) state of the player, derived from what its protocols
report, including any user customizations and transformations that are applied to it.
The playerstate is the object that is exposed to the outside world (via the API).
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import time
import weakref
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.auth import Scope, UserRole
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
    ProtocolRole,
    ProviderFeature,
    ProviderType,
    RepeatMode,
    SleepTimerMode,
    SourceControl,
)
from music_assistant_models.errors import (
    ActionUnavailable,
    AlreadyRegisteredError,
    InsufficientPermissions,
    InvalidCommand,
    InvalidDataError,
    MusicAssistantError,
    PlayerCommandFailed,
    PlayerUnavailableError,
    ProviderUnavailableError,
    ResourceBusyError,
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
    ATTR_IDLE_SINCE,
    ATTR_MUTE_CONTROL,
    ATTR_MUTE_LOCK,
    ATTR_POWER_CONTROL,
    ATTR_POWER_OFF_IN_PROGRESS,
    ATTR_POWER_ON_IN_PROGRESS,
    ATTR_POWERED,
    ATTR_PREVIOUS_VOLUME,
    ATTR_REACHABLE,
    ATTR_SUPPORTED_FEATURES,
    ATTR_VOLUME_CONTROL,
    ATTR_VOLUME_TARGET,
    CONF_ANNOUNCE_TTS_ENGINE,
    CONF_AUTO_PLAY,
    CONF_CACHED_ARP_MAC,
    CONF_ENTRY_MAX_VOLUME,
    CONF_ENTRY_MIN_VOLUME,
    CONF_IDLE_POWER_OFF,
    CONF_LEGACY_IDS,
    CONF_MAX_VOLUME,
    CONF_MIN_VOLUME,
    CONF_MUTE_CONTROL,
    CONF_PLAY_MEDIA_OVERRIDES_GROUP,
    CONF_PLAYER_DSP,
    CONF_PLAYER_QUEUES,
    CONF_PLAYERS,
    CONF_POWER_CONTROL,
    CONF_REPORTED_MAC,
    CONF_SESSION_ONLY,
    CONF_VOLUME_CONTROL,
    CONF_VOLUME_STEP,
    VERBOSE_LOG_LEVEL,
)
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    get_current_user,
    has_player_access,
    set_current_user,
    system_auth_context,
)
from music_assistant.helpers.api import api_command
from music_assistant.helpers.colors import get_palette_for_url
from music_assistant.helpers.config_entries import PLAYBACK_TARGET_TYPES
from music_assistant.helpers.plugin_engines import create_tts_engine_config_entries
from music_assistant.helpers.provider_access import resolve_playback_user
from music_assistant.helpers.util import (
    enrich_device_mac_address,
    is_valid_mac_address,
)
from music_assistant.models.core_controller import CoreController
from music_assistant.models.player import Player, PlayerMedia, PlayerState
from music_assistant.models.player_provider import PlayerProvider
from music_assistant.models.plugin import PluginProvider, SourceControlValue

from .announcements import AnnouncementsMixin
from .audio_sources import AudioSourceMixin, AudioSourceSession
from .constants import (
    PLAYER_LOCK_SLOW_THRESHOLD,
    PLAYER_LOCK_STRICT_TIMEOUT,
    PLAYER_LOCK_TIMEOUT,
    PlayerLockPurpose,
    SwitchedOffAction,
)
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
    from music_assistant.models.player_protocol import PlayerProtocol

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

# How long an MA power off command claims the power off the player reports back. Covers
# the stop the command waits on and a power control that reports the new state some
# time later.
POWER_OFF_IN_PROGRESS_EXPIRY = 30.0

# How long a power on through an external power control waits for a device that was
# switched off at that control to come online.
OFFLINE_POWER_ON_TIMEOUT = 90

# How long a freshly started source session may wait for its first stream request
# before it is considered never started and released.
AUDIO_SOURCE_CLAIM_TIMEOUT = 30

# How long a player must stay powered off before the queue it was playing is ended.
# Home Assistant reports an entity that is (briefly) unavailable or unknown as off, so an
# external power control can report a power off that comes straight back - which must not
# stop the music. Long enough to sit out a reloading integration or a device missing a
# poll, short enough that a real power off is not left streaming.
EXTERNAL_POWER_OFF_STOP_DELAY = 15

# How long after a player provider was loaded (the builtin ones at every start) it gets to find
# its devices again before players/remove_unavailable may remove the players it did not find.
REMOVE_UNAVAILABLE_GRACE = 60.0

# Player types that must be detached from their (sync)group when they power off.
# A stereo pair joins a group and leads a sync session exactly like a single speaker does.
# A GROUP player has no group of its own to leave: its members are released by its own
# power off, and ungrouping one is defined as powering it off - which is what already
# brought it here.
UNGROUP_ON_POWER_OFF_TYPES = {PlayerType.PLAYER, PlayerType.STEREO_PAIR}

# Sentinel used to detect omitted optional arguments where ``None`` is a valid value.
_SENTINEL: Any = object()


class _Attach:
    """
    An attach of a protocol that is in progress.

    The one stored for its protocol id is the current attach of that protocol: a detach
    takes it away, after which the attach stops at its next step.
    """

    __slots__ = ("minted_player_id", "new_player", "new_protocol_record")

    def __init__(self) -> None:
        """Initialize the state of an attach."""
        # id of the player whose config this attach stored for a new device
        self.minted_player_id: str | None = None
        # id of the protocol whose config this attach stored for a player that exists
        self.new_protocol_record: str | None = None
        # the player this attach created, unpublished until the attach completes
        self.new_player: Player | None = None


class PlayerController(AnnouncementsMixin, AudioSourceMixin, ProtocolLinkingMixin, CoreController):
    """Controller holding all logic to control registered players."""

    domain: str = "players"

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize core controller."""
        super().__init__(mass)
        self._players: dict[str, Player] = {}
        # the protocol objects the providers attached, by their id: bound to their
        # player, or held while it is not known yet which player they belong to
        self._protocols: dict[str, PlayerProtocol] = {}
        # the protocol objects the providers hold that are not attached: detached, or not
        # attached as their player is disabled or the user switched the protocol off
        self._unattached: weakref.WeakValueDictionary[str, PlayerProtocol] = (
            weakref.WeakValueDictionary()
        )
        # the attach that is setting up a protocol, by protocol id
        self._attaching: dict[str, _Attach] = {}
        # the ids of the players that are being removed, which no protocol attaches to
        self._removing: set[str] = set()
        # when each player provider was last loaded (monotonic), by instance id
        self._providers_loaded_at: dict[str, float] = {}
        # when each protocol that needs polling was last polled, by protocol id
        self._last_polls: dict[str, float] = {}
        # the player id behind every legacy id of the stored players
        self._legacy_ids: dict[str, str] = {}
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
        # Lock to prevent race conditions while protocols attach
        self._register_lock = asyncio.Lock()
        # the end of the wait of every held protocol whose identifiers may still arrive
        self._pending_protocol_evaluations: dict[str, asyncio.TimerHandle] = {}
        # Live external AudioSource playing on a player, keyed on player_id
        self._source_sessions: dict[str, AudioSourceSession] = {}
        # Subscribers for player state updates (called with player + changed_values)
        self._state_update_subscribers: list[
            Callable[[Player, dict[str, tuple[Any, Any]]], None]
        ] = []

    @contextlib.asynccontextmanager
    async def get_player_lock(
        self,
        player_id: str,
        purpose: PlayerLockPurpose = PlayerLockPurpose.PLAYBACK,
        strict: bool = False,
    ) -> AsyncIterator[None]:
        """
        Acquire a purpose-scoped lock for a player, with re-entrant support.

        Tracks lock ownership per asyncio Task so that nested calls within the same
        task skip re-acquisition (preventing deadlocks), while deferred callbacks
        (call_later / create_task) correctly acquire a fresh lock.

        If the lock can't be acquired within 30s the body runs anyway, to keep
        the player responsive when a previous holder is stuck on a hung command.
        A strict acquisition never runs without the lock: it keeps waiting, up to
        120s per lock, and then raises ResourceBusyError.

        Ordering rule: when a command needs both a group/leader lock and a member
        lock, it must take the group's first. The group players themselves always
        lock their members from under their own lock, so a member-first
        acquisition is an inversion and will deadlock. A command on a member that
        reaches its group uses get_group_and_player_lock for that.

        :param player_id: The player to lock.
        :param purpose: Lock category. Commands with different purposes can run
            concurrently on the same player.
        :param strict: Fail once the strict timeout is over instead of running the
            body without the lock, for work that corrupts shared state when it
            overlaps with the holder (the queue play actions).
        :raises ResourceBusyError: When a strict acquisition timed out.
        """
        lock_key = f"{purpose.value}_{player_id}"
        task = asyncio.current_task()

        if task is not None and lock_key in self._task_held_locks.get(task, set()):
            yield
            return

        lock = self._player_command_locks.setdefault(lock_key, asyncio.Lock())
        acquired = await self._acquire_player_lock(lock, purpose, player_id, strict)
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

    @contextlib.asynccontextmanager
    async def get_group_and_player_lock(
        self, player_id: str, strict: bool = False
    ) -> AsyncIterator[None]:
        """
        Acquire the playback lock of a player, preceded by that of the group holding it.

        For a command on a group member that reaches its (sync)group: releasing the
        member, joining it back or restarting the group all take the group's lock,
        which has to be taken before the member's own (see get_player_lock). A player
        that is not part of a group only takes its own lock.

        :param player_id: The player to lock.
        :param strict: Never run without either lock, see get_player_lock. Each lock has
            its own strict timeout, so a player held by a group waits for both in turn.
        :raises ResourceBusyError: When a strict acquisition timed out.
        """
        async with contextlib.AsyncExitStack() as stack:
            if (player := self.get_player(player_id)) and (
                owner := self._resolve_playback_owner(player)
            ) is not player:
                await stack.enter_async_context(
                    self.get_player_lock(owner.player_id, PlayerLockPurpose.PLAYBACK, strict=strict)
                )
            await stack.enter_async_context(
                self.get_player_lock(player_id, PlayerLockPurpose.PLAYBACK, strict=strict)
            )
            yield

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
        self._build_legacy_id_index()
        self._poll_task = self.mass.create_task(self._poll_players())

    async def post_setup(self) -> None:
        """Bring back the stored players, before any provider attaches a protocol."""
        await self._restore_players()

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
            # how the players are made up, without anything that names them
            "player_protocols": [
                {
                    "type": player.state.type.value,
                    "available": player.state.available,
                    "protocols": [
                        {
                            "domain": protocol.domain,
                            "role": protocol.role.value,
                            "available": protocol.available,
                            "active": protocol.active,
                        }
                        for protocol in player.state.protocols
                    ],
                }
                for player in players
            ],
        }

    async def on_provider_loaded(self, provider: PlayerProvider) -> None:
        """Handle logic when a provider is loaded."""
        self._providers_loaded_at[provider.instance_id] = time.monotonic()

    async def on_provider_unload(self, provider: PlayerProvider) -> None:
        """
        Handle logic when a provider is (about to get) unloaded.

        Its protocols are detached: the players stay, on what their other protocols offer.
        """
        # a discovery that was asked for must not run against the instance that goes
        self.mass.cancel_timer(f"discover_players_{provider.instance_id}")
        for protocol in list(self._protocols.values()):
            if protocol.provider.instance_id == provider.instance_id:
                await self.detach(protocol.protocol_id)

    @property
    def providers(self) -> list[PlayerProvider]:
        """Return all loaded/running MusicProviders."""
        return cast("list[PlayerProvider]", self.mass.get_providers(ProviderType.PLAYER))

    def iter_players(
        self,
        return_unavailable: bool = True,
        return_disabled: bool = False,
        provider_filter: str | None = None,
    ) -> Iterator[Player]:
        """
        Iterate over all players, regardless of who is asking.

        Use this for internal logic - state derivation, bookkeeping and topology
        lookups - which must stay correct no matter which user's command happened
        to trigger it. Use :meth:`all_players` for anything presented to a user.

        :param return_unavailable [bool]: Include unavailable players.
        :param return_disabled [bool]: Include disabled players.
        :param provider_filter [str]: Optional filter by the provider (instance) a player names.
        """
        for player in list(self._players.values()):
            if not (player.state.available or return_unavailable):
                continue
            if not (player.state.enabled or return_disabled):
                continue
            if not player.initialized.is_set():
                continue
            if provider_filter is not None and player.provider_id != provider_filter:
                continue
            yield player

    def iter_protocols(self, provider_filter: str | None = None) -> Iterator[PlayerProtocol]:
        """
        Iterate over the protocol objects the providers attached.

        A protocol that is held until it is known which player it belongs to is
        included; its ``player`` is None.

        :param provider_filter: Only the protocols of this provider instance.
        """
        for protocol in list(self._protocols.values()):
            if provider_filter is None or protocol.provider.instance_id == provider_filter:
                yield protocol

    def all_players(
        self,
        return_unavailable: bool = True,
        return_disabled: bool = False,
        provider_filter: str | None = None,
    ) -> list[Player]:
        """
        Return the players the current user is allowed to see.

        Note that this applies user filters for players (for non admin users),
        which makes it unsuitable for internal logic - use :meth:`iter_players` there.

        :param return_unavailable [bool]: Include unavailable players.
        :param return_disabled [bool]: Include disabled players.
        :param provider_filter [str]: Optional filter by the provider (instance) a player names.

        :return: List of Player objects.
        """
        current_user = get_current_user()
        return [
            player
            for player in self.iter_players(
                return_unavailable=return_unavailable,
                return_disabled=return_disabled,
                provider_filter=provider_filter,
            )
            if has_player_access(current_user, player.player_id, player)
        ]

    @api_command("players/all", required_scope=Scope.PLAYERS_READ)
    def all_player_states(
        self,
        return_unavailable: bool = True,
        return_disabled: bool = False,
        provider_filter: str | None = None,
    ) -> list[PlayerState]:
        """
        Return PlayerState for all players.

        :param return_unavailable [bool]: Include unavailable players.
        :param return_disabled [bool]: Include disabled players.
        :param provider_filter [str]: Optional filter by the provider (instance) a player names.

        :return: List of PlayerState objects.
        """
        return [
            player.state
            for player in self.all_players(
                return_unavailable=return_unavailable,
                return_disabled=return_disabled,
                provider_filter=provider_filter,
            )
        ]

    def get_player(
        self,
        player_id: str,
        raise_unavailable: bool = False,
    ) -> Player | None:
        """
        Return Player by player_id.

        :param player_id [str]: ID of the player, or one of its legacy ids.
        :param raise_unavailable [bool]: Raise if player is unavailable.

        :raises PlayerUnavailableError: If player is unavailable and raise_unavailable is True.
        :return: Player object or None.
        """
        if player := self._players.get(self.resolve_player_id(player_id)):
            if (not player.state.available or not player.state.enabled) and raise_unavailable:
                msg = f"Player {player_id} is not available"
                raise PlayerUnavailableError(msg)
            return player
        if raise_unavailable:
            msg = f"Player {player_id} is not available"
            raise PlayerUnavailableError(msg)
        return None

    def resolve_player_id(self, player_id: str) -> str:
        """
        Return the id of the player that answers to the given id.

        A player keeps answering to its legacy ids, such as the id of a player that was
        merged into it. Any other id is returned as it is, whether a player has it or not.

        :param player_id: A player id or legacy id.
        """
        if player_id in self._players or player_id not in self._legacy_ids:
            return player_id
        # the id of a stored player is never a legacy id
        if player_id in self.mass.config.get(CONF_PLAYERS, {}):
            return player_id
        return self._legacy_ids[player_id]

    def get_protocol(self, protocol_id: str) -> PlayerProtocol | None:
        """
        Return the attached protocol object of any player provider by its id.

        Meant for provider code that needs a protocol object of another provider;
        a provider looks up its own protocol objects through its own `get_protocol`.

        :param protocol_id: ID of the protocol object.
        """
        return self._protocols.get(protocol_id)

    def get_output_protocol(self, protocol_or_player_id: str) -> PlayerProtocol | None:
        """
        Return the protocol that renders the audio a stream is asked for under a protocol id.

        A protocol fetches its stream under its own id. An id that names no attached
        protocol is taken as that of a player: the protocol that renders for it now.
        Code that holds a player id asks the player instead
        (``get_player(player_id).resolve_output_protocol()``): a player from before the
        split shares its id with its own protocol, which this returns also while one of
        its audio protocols renders.

        :param protocol_or_player_id: ID of an attached protocol, else of a player or one
            of the legacy ids of a player.
        """
        if protocol := self._protocols.get(protocol_or_player_id):
            return protocol
        if player := self.get_player(protocol_or_player_id):
            return player.resolve_output_protocol()
        return None

    def get_player_by_protocol(self, protocol_or_player_id: str) -> Player | None:
        """
        Return the player an id names: the one a protocol is attached to, or the player itself.

        For the ids protocols report, such as their group members: protocols of one
        provider name each other by protocol id, a group names its members by player id.

        :param protocol_or_player_id: ID of an attached protocol or of a player, or one
            of the legacy ids of a player.
        """
        protocol = self._protocols.get(protocol_or_player_id)
        if protocol is not None and protocol.player is not None:
            return protocol.player
        return self.get_player(protocol_or_player_id)

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
        if current_user and not has_player_access(
            current_user, player_id, self.get_player(player_id)
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
        if player := self.get_player_by_name(name):
            if current_user and not has_player_access(current_user, player.player_id, player):
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
        :raises InsufficientPermissions: if the current user may not use the player.
        """
        self.check_player_access(player_id)
        player = self._get_player_with_redirect(player_id)
        return player.sleep_timer_expires_at

    @api_command("players/sleep_timer/set", required_scope=Scope.PLAYERS_CONTROL)
    def set_sleep_timer(
        self,
        player_id: str,
        seconds: int | None = None,
        mode: SleepTimerMode = SleepTimerMode.DURATION,
    ) -> float | None:
        """
        Set a sleep timer for the player.

        Returns the (estimated) expiry timestamp, or None while a queue item or chapter timer
        waits for paused playback.

        :param player_id: Player ID to set the timer for.
        :param seconds: Delay in seconds before playback is stopped, for the DURATION mode.
        :param mode: The point at which playback is stopped.
        :raises InsufficientPermissions: if the current user may not use the player.
        """
        self.check_player_access(player_id)
        if mode in (SleepTimerMode.END_OF_ITEM, SleepTimerMode.END_OF_CHAPTER):
            player = self._get_player_with_redirect(player_id)
            # the queue stops itself at that point, shows it on the player and raises when
            # nothing with an end plays
            expires_at = self.mass.player_queues.set_sleep_timer_target(player.player_id, mode)
            self.mass.cancel_timer(self._sleep_timer_task_id(player.player_id))
            return expires_at
        if mode != SleepTimerMode.DURATION:
            msg = f"Unsupported sleep timer mode: {mode}"
            raise InvalidDataError(msg)
        if seconds is None or seconds <= 0:
            msg = "Sleep timer duration must be greater than zero seconds"
            raise InvalidDataError(msg)
        player = self._get_player_with_redirect(player_id)
        try:
            # guard against absurd durations that overflow the float timestamp math
            expires_at = time.time() + seconds
        except OverflowError:
            msg = "Sleep timer duration is too large to schedule"
            raise InvalidDataError(msg) from None
        self.mass.player_queues.drop_sleep_timer_target(player.player_id)
        player.set_sleep_timer_mode(SleepTimerMode.DURATION)
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
        :raises InsufficientPermissions: if the current user may not use the player.
        """
        self.check_player_access(player_id)
        player = self._get_player_with_redirect(player_id)
        self._clear_sleep_timer(player)

    def update_sleep_timer(
        self, player_id: str, mode: SleepTimerMode, expires_at: float | None
    ) -> None:
        """
        Update a sleep timer that stops at a point in the player's queue.

        :param player_id: Player ID (and queue ID) whose sleep timer to update.
        :param mode: The point at which the queue stops.
        :param expires_at: The estimated unix (utc) timestamp of the stop, None while paused.
        """
        player = self.get_player(player_id)
        if player is None:
            return
        prev_expires_at = player.sleep_timer_expires_at
        if player.sleep_timer_mode == mode and (
            (prev_expires_at is None and expires_at is None)
            or (
                prev_expires_at is not None
                and expires_at is not None
                # position reports jitter, only announce a real change
                and abs(expires_at - prev_expires_at) < 1
            )
        ):
            return
        player.set_sleep_timer_mode(mode)
        player.set_sleep_timer_expires_at(expires_at)
        player.update_state()
        self._signal_sleep_timer_updated(player, expires_at)

    def reset_sleep_timer(self, player: Player) -> None:
        """
        Reset the sleep timer state of the player, without touching its queue.

        :param player: Player whose sleep timer ended.
        """
        if player.sleep_timer_mode is None:
            return
        player.set_sleep_timer_expires_at(None)
        player.set_sleep_timer_mode(None)
        player.update_state()
        self._signal_sleep_timer_updated(player, None)

    # Player commands

    @api_command("players/cmd/stop", required_scope=Scope.PLAYERS_CONTROL)
    @handle_player_command(switched_off=SwitchedOffAction.CANCEL_POWER_ON)
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
    @handle_player_command(switched_off=SwitchedOffAction.POWER_ON)
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
            # a player that is not paused resumes its queue, and so does one paused on a
            # stream its queue has since ended; any other paused player is unpaused
            source = player.state.active_source
            if (active_queue := self.mass.player_queues.get(source or player_id)) and (
                player.state.playback_state != PlaybackState.PAUSED
                or self.mass.player_queues.has_lost_paused_stream(active_queue.queue_id)
            ):
                await self.mass.player_queues.resume(active_queue.queue_id)
                return
            # Delegate to internal handler for actual implementation
            await self._handle_cmd_play(player.player_id)

    @api_command("players/cmd/pause", required_scope=Scope.PLAYERS_CONTROL)
    @handle_player_command(switched_off=SwitchedOffAction.CANCEL_POWER_ON)
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
    @handle_player_command(switched_off=SwitchedOffAction.POWER_ON)
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
        active_source = next(
            (x for x in player.state.source_list if x.id == player.state.active_source), None
        )
        if active_source and not active_source.can_seek:
            msg = (
                f"The active source ({active_source.name}) on player "
                f"{player.display_name} does not support seeking"
            )
            raise PlayerCommandFailed(msg)
        if PlayerFeature.SEEK not in player.state.supported_features:
            msg = f"Player {player.display_name} does not support seeking"
            raise UnsupportedFeaturedException(msg)
        # handle command on player directly
        await self._get_transport_protocol(player).seek(position)

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
            await self._get_transport_protocol(player).set_shuffle(shuffle_enabled)
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
            await self._get_transport_protocol(player).set_repeat(repeat_mode)
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
                await self._get_transport_protocol(player).next_track()
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
                await self._get_transport_protocol(player).previous_track()
                return
            msg = "This action is (currently) unavailable for this source."
            raise PlayerCommandFailed(msg)
        # Player does not support next/previous feature
        msg = f"Player {player.state.name} does not support skipping to the previous track."
        raise UnsupportedFeaturedException(msg)

    @api_command("players/cmd/power", required_scope=Scope.PLAYERS_CONTROL)
    @handle_player_command(switched_off=SwitchedOffAction.RUN, accept_unavailable=True)
    async def cmd_power(self, player_id: str, powered: bool) -> None:
        """
        Send POWER command to given player.

        :param player_id: player_id of the player to handle the command.
        :param powered: bool if player should be powered on or off.
        """
        if powered and (player := self.get_player(player_id)) and player.offline_by_power_control:
            # no lock while the switched off device comes online: that takes far longer than
            # other commands wait for the lock, and it has no group to form
            await self._handle_cmd_power(player_id, True)
            return
        # Power is serialized with PLAYBACK because powering on a sync/group player
        # forms the group (and powering off dissolves it) - this must not race with
        # play_media / cmd_resume / cmd_set_members on the same player.
        # A power off also detaches this player from its (sync)group, which takes the
        # group's lock - so that one is taken first (see get_group_and_player_lock).
        lock = (
            self.get_player_lock(player_id, PlayerLockPurpose.PLAYBACK)
            if powered
            else self.get_group_and_player_lock(player_id)
        )
        async with lock:
            await self._handle_cmd_power(player_id, powered)

    @api_command("players/cmd/volume_set", required_scope=Scope.PLAYERS_CONTROL)
    @handle_player_command(switched_off=SwitchedOffAction.IGNORE)
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
    @handle_player_command(switched_off=SwitchedOffAction.IGNORE)
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
    @handle_player_command(switched_off=SwitchedOffAction.IGNORE)
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
    @handle_player_command(switched_off=SwitchedOffAction.IGNORE)
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
    @handle_player_command(switched_off=SwitchedOffAction.IGNORE)
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
    @handle_player_command(switched_off=SwitchedOffAction.IGNORE)
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
    @handle_player_command(switched_off=SwitchedOffAction.IGNORE)
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
    @handle_player_command(lock=PlayerLockPurpose.VOLUME, switched_off=SwitchedOffAction.IGNORE)
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

    @handle_player_command(switched_off=SwitchedOffAction.POWER_ON)
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
        # The release reaches the group (and takes its lock through cmd_set_members
        # for a dynamic member), so it runs before this player's own lock is taken
        # here. A queue action calling in with that lock already held took the
        # group's lock first as well (see get_group_and_player_lock).
        target_player = self.get_player(player_id, True)
        if target_player is not None and (
            target_player.state.synced_to or target_player.state.active_group
        ):
            override = bool(
                self.mass.config.get_player_value(
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

        protocol = player.own_protocol
        if protocol is None or PlayerFeature.SELECT_SOUND_MODE not in protocol.supported_features:
            raise UnsupportedFeaturedException(
                f"Player {player.display_name} does not support sound mode selection"
            )

        prev_sound_mode = protocol.active_sound_mode
        if sound_mode == prev_sound_mode:
            return

        # basic check if sound mode is valid for player
        if not any(x for x in protocol.sound_mode_list if x.id == sound_mode):
            raise PlayerCommandFailed(
                f"{sound_mode} is an invalid sound_mode for player {player.display_name}"
            )

        # forward to player
        await protocol.select_sound_mode(sound_mode)

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

        protocol = player.own_protocol
        if protocol is None or PlayerFeature.OPTIONS not in protocol.supported_features:
            raise UnsupportedFeaturedException(
                f"Player {player.display_name} does not support set_option"
            )

        prev_player_option = next((x for x in protocol.options if x.key == option_key), None)
        if not prev_player_option:
            return
        if prev_player_option.value == option_value:
            return

        if prev_player_option.read_only:
            raise UnsupportedFeaturedException(
                f"Player {player.display_name} option {option_key} is read-only"
            )

        # forward to player
        await protocol.set_option(option_key=option_key, option_value=option_value)

    @api_command("players/cmd/select_source", required_scope=Scope.PLAYERS_CONTROL)
    @handle_player_command(switched_off=SwitchedOffAction.POWER_ON)
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

    async def cmd_set_members(
        self,
        target_player: str,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """
        Join/unjoin given player(s) to/from target player.

        Will add the given player(s) to the target player (sync leader or group player).
        Does not check the current user's player access, see check_grouping_access.

        :param target_player: player_id of the syncgroup leader or group player.
        :param player_ids_to_add: List of player_id's to add to the target player.
        :param player_ids_to_remove: List of player_id's to remove from the target player.

        :raises UnsupportedFeaturedException: if the target player does not support grouping.
        :raises PlayerUnavailableError: if the target player is not available.
        """
        parent_player = self._get_reachable_player(target_player)
        if PlayerFeature.SET_MEMBERS not in parent_player.state.supported_features:
            msg = f"Player {parent_player.display_name} does not support group commands"
            raise UnsupportedFeaturedException(msg)

        # if the target player is a member of an active group player (e.g. a syncgroup),
        # redirect the command to that group player so it can manage the member change
        if group_player := self._get_set_members_group(parent_player):
            self.logger.debug(
                "Redirecting set_members from %s to its group player %s",
                parent_player.display_name,
                group_player.display_name,
            )
            await self.cmd_set_members(
                group_player.player_id, player_ids_to_add, player_ids_to_remove
            )
            return

        if parent_player.state.synced_to:
            # handle edge case: target player is already synced itself to another player
            # automatically ungroup it first and wait for state to propagate
            await self._auto_ungroup_if_synced(parent_player, "setting members")

        # Use lock for playback commands to prevent protocol switches from
        # racing with concurrent play_media / play_index / resume calls.
        async with self.get_player_lock(parent_player.player_id, PlayerLockPurpose.PLAYBACK):
            await self._handle_set_members(parent_player, player_ids_to_add, player_ids_to_remove)

    def check_player_access(self, player_id: str) -> None:
        """
        Check that the current user may use the given player.

        :param player_id: The id of the player to check.
        :raises InsufficientPermissions: if the current user may not use the player.
        """
        if (current_user := get_current_user()) is None:
            return
        player = self.get_player(player_id)
        if not has_player_access(current_user, player_id, player):
            name = player.display_name if player else player_id
            msg = f"{current_user.username} does not have access to player {name}"
            raise InsufficientPermissions(msg)

    def check_grouping_access(
        self,
        target_player: str,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """
        Check that the current user may make the given cmd_set_members request.

        Call this before cmd_set_members for a request that comes from a client.

        Adding players needs access to the target player and to every player added.
        Removing players needs access to the target player or to every player removed, so a
        user can always take a player of their own out of a group. When the target player is
        a member of a group player, removing needs access to that group player as well, or to
        every player removed. A request that adds nothing needs access to the target player
        when it is empty, or when the target player is synced to another player (as it is
        detached from that player first).

        :param target_player: player_id of the syncgroup leader or group player.
        :param player_ids_to_add: List of player_id's to add to the target player.
        :param player_ids_to_remove: List of player_id's to remove from the target player.
        :raises InsufficientPermissions: if the current user may not make the request.
        """
        if (current_user := get_current_user()) is None:
            return
        target = self.get_player(target_player)
        group_player = self._get_set_members_group(target) if target else None
        if player_ids_to_add:
            # a group player the command is passed on to only gains players the user may use
            for player_id in (target_player, *player_ids_to_add):
                self.check_player_access(player_id)
        elif not player_ids_to_remove or (target and target.state.synced_to and not group_player):
            # cmd_set_members detaches a synced target first, unless it passes the
            # command on to the target's group player
            self.check_player_access(target_player)
        if not player_ids_to_remove:
            return
        owners = [target_player, group_player.player_id] if group_player else [target_player]
        if not all(has_player_access(current_user, pid, self.get_player(pid)) for pid in owners):
            for player_id in player_ids_to_remove:
                self.check_player_access(player_id)

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
        :raises InsufficientPermissions: if the user may not use the player or target player.
        """
        self.check_grouping_access(target_player, player_ids_to_add=[player_id])
        await self.cmd_set_members(target_player, player_ids_to_add=[player_id])

    @api_command("players/cmd/group_many", required_scope=Scope.PLAYERS_CONTROL)
    async def cmd_group_many(self, target_player: str, child_player_ids: list[str]) -> None:
        """
        Join given player(s) to target player.

        Will add the given player(s) to the target player (sync leader or group player).
        This is a (deprecated) alias for cmd_set_members.

        :raises InsufficientPermissions: if the user may not use the target player or a child.
        """
        self.check_grouping_access(target_player, player_ids_to_add=child_player_ids)
        await self.cmd_set_members(target_player, player_ids_to_add=child_player_ids)

    @api_command("players/cmd/ungroup", required_scope=Scope.PLAYERS_CONTROL)
    @handle_player_command(switched_off=SwitchedOffAction.IGNORE)
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
            await self._release_group_session(player.player_id)
            return

        if player.state.active_group:
            group = self.get_player(player.state.active_group)
            is_static_member = group is not None and player_id in group.state.static_group_members
            if is_static_member:
                # Static members can't be released individually — release the
                # session of the group as a whole. Access to the member is enough.
                if group is not None:
                    await self._release_group_session(group.player_id)
                return
            # dynamic or non-static member — remove just this player
            await self.cmd_set_members(player.state.active_group, player_ids_to_remove=[player_id])
            return

        if player.state.synced_to:
            # player is a sync member
            parent = self.get_player(player.state.synced_to)
            if parent and parent.available_for_playback:
                await self.cmd_set_members(parent.player_id, player_ids_to_remove=[player_id])
            else:
                await self._detach_member(player)
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
            with suppress(PlayerUnavailableError):
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
        """
        Remove a group player for good.

        :param player_id: ID of the group player to remove.
        """
        if (player := self.get_player(player_id)) and player.state.type != PlayerType.GROUP:
            raise UnsupportedFeaturedException(f"Player {player.state.name} is not a group player")
        await self.remove(player_id)

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
        mass_queue = self.get_active_queue(player)
        # a favorite set from a player belongs to the user the playback is for; a service
        # account (such as the Home Assistant integration) acts for that user as well
        session_user = get_current_user()
        if (
            (session_user is None or session_user.role == UserRole.SERVICE)
            and mass_queue
            and (playback_user := await resolve_playback_user(self.mass, mass_queue.queue_id))
        ):
            set_current_user(playback_user)
        # handle mass player queue active
        if mass_queue:
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

    async def attach(self, protocol: PlayerProtocol) -> None:
        """
        Attach a protocol to its player: its provider found the device, or found it again.

        The player the protocol belongs to is created when the device is new. Nothing is
        attached for a player the user disabled or a protocol the user switched off.
        Attaching the object that is attached already publishes what it reports now;
        another object under the same id takes its place.

        :param protocol: The protocol object to attach.
        """
        if self._teardown_in_progress(protocol):
            return
        protocol_id = protocol.protocol_id
        # the lock ensures a replacement is never swapped in while an attach of the
        # same protocol is still setting it up
        async with self._register_lock:
            # a teardown may have started while this waited for the lock
            if self._teardown_in_progress(protocol):
                return
            if self._protocols.get(protocol_id) is protocol:
                await self._refresh_protocol(protocol)
                return
            await self._run_attach(protocol)
        # Schedule debounced update of all players since can_group_with values may change
        # when a protocol is attached (provider IDs expand to include it)
        self._schedule_update_all_players(2)

    async def detach(self, protocol_id: str) -> None:
        """
        Detach a protocol from its player: its provider lost the device, or unloads.

        The player stays and keeps its queue; it is unavailable while none of its other
        protocols is available. A player that only exists while its device is connected
        goes with its last protocol. Silently ignored for a protocol that is not attached.

        :param protocol_id: Id of the protocol to detach.
        """
        # an attach that is still setting this protocol up stops at its next step
        self._attaching.pop(protocol_id, None)
        if (protocol := self._protocols.get(protocol_id)) is None:
            return
        if player := await self._unbind(protocol):
            self.logger.info(
                "Protocol %s detached from player %s", protocol_id, player.display_name
            )
            await self._settle_player(player)
        if not protocol.provider.unloading:
            # its provider may keep the object, which a removal of the player has to reach
            self._unattached[protocol_id] = protocol
        # Schedule debounced update of all players since can_group_with values may change
        self._schedule_update_all_players()

    async def remove_protocol(self, protocol_id: str) -> None:
        """
        Remove a protocol for good: its provider forgets the device.

        The protocol is detached and its config deleted, and so are the protocols that
        ride on it. An output or control choice of its player that names it falls back to
        its default. The player it belonged to goes with it when no other protocol of it
        is left.

        :param protocol_id: Id of the protocol to remove.
        """
        for derived_id in self.mass.config.get_protocol_ids_derived_from(protocol_id):
            # it can never attach again without the protocol it rides on
            await self.remove_protocol(derived_id)
        protocol = self._protocols.get(protocol_id)
        player_id = self._get_player_id_of_protocol(protocol_id)
        await self.detach(protocol_id)
        self.mass.config.remove_protocol_record(protocol_id)
        if protocol is not None:
            # the provider forgot the device, so what it stored about it is not needed anymore
            self._unattached.pop(protocol_id, None)
        if player_id is None:
            return
        self._replace_protocol_references(player_id, protocol_id, None)
        if player := self._players.get(player_id):
            player.remove_protocol(protocol_id, forget=True)
        if self.mass.config.get_protocol_ids_of_player(player_id) or (player and player.protocols):
            if player:
                player.refresh_state()
                self._store_player_base(player)
            # a protocol that waits for the place of its domain may get it now
            self._schedule_held_protocols()
            return
        await self._remove_player(player_id, rediscover=False)

    @api_command("players/remove", required_scope=Scope.CONFIG_PLAYERS_WRITE)
    async def remove(self, player_id: str) -> None:
        """
        Remove a player for good: the player itself, its protocols, its config and its queue.

        A device its provider still finds comes back as a new player.

        :param player_id: ID of the player to remove.
        """
        await self._remove_player(self.resolve_player_id(player_id), rediscover=True)

    @api_command("players/remove_unavailable", required_scope=Scope.CONFIG_PLAYERS_WRITE)
    async def remove_unavailable(self, dry_run: bool = False) -> list[str]:
        """
        Remove every player that is unavailable while its providers are running.

        A player is left alone while one of its protocols reaches its device (also one
        that still needs setup), while a provider of its protocols is not loaded or was
        loaded shortly ago and still finds its devices, and when it is disabled. Returns
        the ids of the players that were removed, or with dry_run of those that would be.

        :param dry_run: Only return the ids of the players that would be removed, by the
            same rules: nothing is removed and nothing changes.
        :raises ActionUnavailable: When every player to remove was left alone because its
            providers are still finding their devices, shortly after the start or a load.
            Also with dry_run, so a client learns that before it asks the user to confirm.
        """
        now = time.monotonic()
        removed: list[str] = []
        waits: list[float] = []
        for player in list(self._players.values()):
            if (
                player.state.available
                or not player.initialized.is_set()
                or any(protocol.available for protocol in player.protocols.values())
            ):
                continue
            provider_ids = {
                str(record.get("provider"))
                for protocol_id in self.mass.config.get_protocol_ids_of_player(player.player_id)
                if (record := self.mass.config.get_protocol_record(protocol_id))
            }
            if not all(self.mass.get_provider(provider_id) for provider_id in provider_ids):
                continue
            # without a protocol, any provider may find the device of the player again
            loaded_at = max(
                (
                    self._providers_loaded_at.get(provider_id, -math.inf)
                    for provider_id in provider_ids or self._providers_loaded_at
                ),
                default=-math.inf,
            )
            if (wait := loaded_at + REMOVE_UNAVAILABLE_GRACE - now) > 0:
                waits.append(wait)
                continue
            if not dry_run:
                await self._remove_player(player.player_id, rediscover=False)
            removed.append(player.player_id)
        if waits and not removed:
            msg = f"Players are still being found, try again in {math.ceil(max(waits))} seconds"
            raise ActionUnavailable(msg)
        return removed

    def wants_protocol(self, protocol_id: str) -> bool:
        """
        Return whether the server wants a protocol attached now.

        Not when the user disabled the player it belongs to, or switched the protocol
        itself off as an output of that player. A provider that sets up a connection
        for a device before it attaches it can ask this first.

        :param protocol_id: Id of the protocol.
        """
        return self._wants_protocol(
            protocol_id, self._protocols.get(protocol_id) or self._unattached.get(protocol_id)
        )

    def on_unattached_protocol_update(self, protocol: PlayerProtocol) -> None:
        """
        Handle an update of a protocol that waits for the player it belongs to.

        :param protocol: The protocol that reported new values.
        """
        if self._protocols.get(protocol.protocol_id) is not protocol or self.mass.closing:
            return
        # the identifiers that tell which player it belongs to may have arrived
        self.mass.create_task(
            self._evaluate_pending_protocol(protocol.protocol_id, final=False),
            task_id=f"evaluate_protocol_{protocol.protocol_id}",
        )

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

        # a player that was just created is published once its config is loaded
        if not player.initialized.is_set():
            return

        # a debounced update can still run on a player that was removed meanwhile
        if self._players.get(player_id) is not player:
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
                player.display_name,
                ", ".join(changed_values.keys()),
            )

        # signal update to the playerqueue
        self.mass.call_later(
            0.5,
            self.mass.player_queues.on_player_update,
            player,
            changed_values,
            task_id=f"queue_on_player_update_{player.player_id}",
        )

        # Kick async palette extraction on cold cache. On transition prefetch
        # the next queue item too. Skip players that mirror another player's media
        # (grouped/synced members): their current_media - palette
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
        self._restart_idle_count(player, changed_values)
        # only steer into the (relatively expensive) membership cleanup when a field
        # that can require an unsync actually changed - this runs on every state tick
        if changed_values.keys() & {ATTR_AVAILABLE, ATTR_ENABLED, ATTR_POWERED, ATTR_REACHABLE}:
            self._handle_membership_cleanup_on_state_change(player, changed_values)
            self._handle_external_power_off(player, changed_values)

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
        if any(
            key in changed_values
            for key in ("group_members", "synced_to", ATTR_AVAILABLE, ATTR_REACHABLE)
        ):
            self._trigger_provider_players_update(player)

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
                if not child_player.available_for_playback or not child_player.state.enabled:
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
        player_id = config.player_id
        player = self._players.get(player_id)
        if ATTR_ENABLED in changed_keys and not config.enabled:
            if player is not None:
                await self._disable_player(player)
            return
        if ATTR_ENABLED in changed_keys and player is None:
            await self._enable_player(player_id)
            return
        if not player:
            return  # a player that is not there takes its config when it comes back

        # ensure player state gets updated with any updated config
        player.set_config(await self.mass.config.get_player_object_config(player))
        if (own := player.own_protocol) is not None:
            # the settings of its own protocol are part of the config of the player
            own.set_config(await self._get_protocol_config(own))
            await own.on_config_updated()
        player.update_state()
        await self._restart_playback_after_config_change(
            player,
            any(
                v.requires_reload
                for v in config.values.values()
                if f"values/{v.key}" in changed_keys
            ),
        )

    async def on_protocol_config_change(self, protocol_id: str, changed_keys: set[str]) -> None:
        """
        Call (by config manager) when the configuration of one protocol of a player changes.

        :param protocol_id: Id of the protocol.
        :param changed_keys: The keys that changed: ``enabled`` for whether the user
            wants the protocol used, ``values/<key>`` for a setting, ``setup_data/<key>``
            for what a setup flow collected.
        """
        protocol = self._protocols.get(protocol_id)
        if ATTR_ENABLED in changed_keys:
            if not self.wants_protocol(protocol_id):
                # the user switched this output of the player off
                await self.detach(protocol_id)
                # a protocol that waits for the place of its domain may take it now
                self._schedule_held_protocols()
                return
            await self._hold_derived_protocols_without_place(protocol_id)
            if protocol is None or protocol.player is None:
                # its provider attaches it when it looks for its devices again
                stored = self.mass.config.get_protocol_record(protocol_id) or {}
                provider = self.mass.get_provider(str(stored.get("provider")))
                if isinstance(provider, PlayerProvider):
                    provider.schedule_discovery()
                return
        if protocol is None or (player := protocol.player) is None:
            return
        config = await self._get_protocol_config(protocol)
        protocol.set_config(config)
        await protocol.on_config_updated()
        player.update_state()
        await self._restart_playback_after_config_change(
            player,
            any(
                v.requires_reload
                for v in config.values.values()
                if f"values/{v.key}" in changed_keys
            ),
        )

    async def on_player_dsp_change(
        self, player_id: str, *, after_group_change: bool = False
    ) -> None:
        """
        Restart playback so a changed DSP setup is applied.

        :param player_id: The player whose DSP settings (or group shape) changed.
        :param after_group_change: Whether the call follows a group membership change
            instead of an edit of the DSP settings themselves.
        """
        # signal player provider that the config changed
        if not (player := self.get_player(player_id)):
            return
        if player.state.playback_state != PlaybackState.PLAYING:
            return
        if active_queue := self.get_active_queue(player):
            # a group change the provider reported can hand this player a queue it is not
            # rendering (a Sonos that becomes coordinator of a group playing another
            # player's queue falls back to its own, idle queue); resuming that would
            # replace the music with the wrong queue. Only checked after a group change;
            # an edit of the DSP settings needs no such evidence.
            if after_group_change and not self.mass.player_queues.is_playing_queue(
                active_queue.queue_id, player
            ):
                self.logger.debug(
                    "Not restarting %s after DSP change: it is not playing queue %s",
                    player.display_name,
                    active_queue.queue_id,
                )
                return
            self.logger.info("Restarting playback of Player %s after DSP change", player_id)
            # this will restart the queue stream/playback
            self.mass.call_later(0, self._resume_queue_after_dsp_change, active_queue.queue_id)
            return
        if after_group_change and not self._is_ma_managed_source(
            player, player.state.active_source
        ):
            # an external source (a tv input, a connect session) has no stream of ours
            # to rebuild; stopping and starting it would only interrupt it
            self.logger.debug(
                "Not restarting %s after DSP change: it is playing external source %s",
                player.display_name,
                player.state.active_source,
            )
            return
        self.logger.info("Restarting playback of Player %s after DSP change", player_id)
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

    async def _acquire_player_lock(
        self, lock: asyncio.Lock, purpose: PlayerLockPurpose, player_id: str, strict: bool
    ) -> bool:
        """
        Wait for a player lock, logging a wait that gets long.

        :param lock: The lock to acquire.
        :param purpose: The lock's category, named in the log lines and the error.
        :param player_id: The player the lock belongs to.
        :param strict: Keep waiting past the lock timeout and fail at the strict timeout,
            instead of giving up on the lock.
        :return: Whether the lock was acquired. A non-strict wait gives up at the lock
            timeout and returns False, so the caller runs without the lock; a strict
            wait raises ResourceBusyError at the strict timeout instead.
        """
        try:
            async with asyncio.timeout(PLAYER_LOCK_SLOW_THRESHOLD):
                await lock.acquire()
            return True
        except TimeoutError:
            self.logger.debug(
                "Acquiring %s lock for player %s is slow (>%ss)",
                purpose.value,
                player_id,
                PLAYER_LOCK_SLOW_THRESHOLD,
            )
        try:
            async with asyncio.timeout(PLAYER_LOCK_TIMEOUT - PLAYER_LOCK_SLOW_THRESHOLD):
                await lock.acquire()
            return True
        except TimeoutError:
            if not strict:
                self.logger.warning(
                    "Timed out (%ss) acquiring %s lock for player %s — "
                    "previous holder appears stuck; proceeding without lock",
                    PLAYER_LOCK_TIMEOUT,
                    purpose.value,
                    player_id,
                )
                return False
            self.logger.info(
                "Waited %ss for the %s lock of player %s — previous holder is still busy; "
                "waiting on, giving up after %ss in total",
                PLAYER_LOCK_TIMEOUT,
                purpose.value,
                player_id,
                PLAYER_LOCK_STRICT_TIMEOUT,
            )
        try:
            async with asyncio.timeout(PLAYER_LOCK_STRICT_TIMEOUT - PLAYER_LOCK_TIMEOUT):
                await lock.acquire()
            return True
        except TimeoutError:
            name = player.display_name if (player := self.get_player(player_id)) else player_id
            msg = (
                f"Player {name} is still busy with a previous {purpose.value} command "
                f"after {PLAYER_LOCK_STRICT_TIMEOUT}s"
            )
            raise ResourceBusyError(msg) from None

    async def _run_attach(self, protocol: PlayerProtocol, final: bool = False) -> None:
        """
        Attach a protocol object to its player, and undo what was done when that fails.

        :param protocol: The protocol object to attach: a new one, or one that is held.
        :param final: A held protocol that matches no player has waited long enough.
        """
        protocol_id = protocol.protocol_id
        attach = self._attaching[protocol_id] = _Attach()
        try:
            await self._attach(protocol, attach, final)
        except Exception, asyncio.CancelledError:
            # a protocol whose setup failed must not stay attached half way, where
            # it keeps blocking every later attach of the same id. Cancellation
            # counts too: a re-triggered provider discovery aborts the task this
            # runs in.
            await self._rollback_attach(protocol, attach)
            raise
        finally:
            if self._attaching.get(protocol_id) is attach:
                del self._attaching[protocol_id]

    async def _attach(self, protocol: PlayerProtocol, attach: _Attach, final: bool) -> None:
        """
        Find the player of a protocol object and bind it, or hold it until that is known.

        :param protocol: The protocol object to attach.
        :param attach: The state of this attach, see _attach_aborted.
        :param final: A held protocol that matches no player has waited long enough.
        """
        protocol_id = protocol.protocol_id
        player_id: str | None = None
        existing = self._protocols.get(protocol_id)
        held = existing is protocol and protocol.player is None
        if not self._wants_protocol(protocol_id, protocol):
            # switched off, or its player is disabled: it keeps its player and its place. Its
            # provider keeps the object, which a removal of the player still has to reach
            if held:
                self._release_held_protocol(protocol)
            self._unattached[protocol_id] = protocol
            return
        if not held:
            # the attached object goes without anything being published: another object
            # of the protocol takes its place, or it attaches again in another role
            replaced_player = await self._unbind(existing) if existing else None
            if protocol.role != ProtocolRole.VIRTUAL and protocol.type not in (
                PlayerType.GROUP,
                PlayerType.STEREO_PAIR,
            ):
                await self._resolve_mac_addresses(protocol)
                # the await above may have staled the guards of attach()
                if self._attach_aborted(protocol, attach) or self._teardown_in_progress(protocol):
                    return
            if (
                replaced_player is not None
                and replaced_player.player_id in self._players
                and self._can_attach_to(
                    protocol,
                    self._identity_of(protocol),
                    replaced_player.player_id,
                    attached_only=True,
                )
            ):
                player_id = replaced_player.player_id
        if player_id is None:
            player_id = self._resolve_player_id(protocol)
        if player_id is None:
            if self._awaits_identity(protocol) and (not final or protocol.underlying_protocol_id):
                if not held:
                    self._hold_protocol(protocol)
                return
            player_id = attach.minted_player_id = self._mint_player(protocol)
        elif self.mass.config.get_protocol_record(protocol_id) is None:
            attach.new_protocol_record = protocol_id
        self._store_protocol(protocol, player_id)
        if not self._wants_protocol(protocol_id, protocol):
            # no player for a device the user does not want: its provider keeps the
            # object, which a removal of the player still has to reach
            if held:
                self._release_held_protocol(protocol)
            self._unattached[protocol_id] = protocol
            return
        await self._bind(protocol, player_id, attach)

    async def _bind(self, protocol: PlayerProtocol, player_id: str, attach: _Attach) -> None:
        """
        Bind a protocol to its player and publish the result.

        :param protocol: The protocol object to bind.
        :param player_id: Id of the player it belongs to.
        :param attach: The state of this attach, see _attach_aborted.
        """
        protocol_id = protocol.protocol_id
        if (player := self._players.get(player_id)) is None:
            # a new player, or one that only exists while its device is connected
            player = attach.new_player = self._create_player(player_id, protocol)
        self._unattached.pop(protocol_id, None)
        self._release_held_protocol(protocol)
        # The protocol is bound before it is fully ready (config not loaded), so it can
        # be found during the rest of the attach, such as when fetching its config.
        # A player that is new stays unpublished until the end.
        self._protocols[protocol_id] = protocol
        player.add_protocol(protocol, protocol.role)
        protocol.set_player(player)
        protocol.set_config(await self._get_protocol_config(protocol))
        # call hooks after the protocol is bound and its config is set
        if not self._attach_aborted(protocol, attach):
            await protocol.on_config_updated()
        if not self._attach_aborted(protocol, attach):
            await protocol.on_attach(player)
        if self._attach_aborted(protocol, attach):
            # a hook may have finished after the detach that stopped this attach, which
            # also left a player nobody saw yet to it
            await self._run_on_detach(protocol)
            self._discard_incomplete_attach(attach)
            return
        self._store_player_base(player)
        if not player.initialized.is_set():
            await self._publish_player(player)
            if self._attach_aborted(protocol, attach):
                # the detach found the player not published yet, and left it to this attach
                await self._settle_player(player)
                return
        else:
            player.set_config(await self.mass.config.get_player_object_config(player))
            if self._attach_aborted(protocol, attach):
                return
            player.update_state()
        self.logger.info(
            "Protocol %s (%s) attached to player %s/%s",
            protocol_id,
            protocol.role.value,
            player_id,
            player.display_name,
        )
        self._schedule_held_protocols()

    async def _refresh_protocol(self, protocol: PlayerProtocol) -> None:
        """
        Publish what an attached protocol object reports now, its role included.

        :param protocol: The protocol object, which is the one attached under its id.
        """
        protocol_id = protocol.protocol_id
        if (player := protocol.player) is None:
            # it waits for its player: match it again on what it reports now
            self.on_unattached_protocol_update(protocol)
            return
        if protocol.role != player.get_role(protocol_id):
            # a protocol in another role leaves its player and attaches again: to the
            # same player, unless that has no place for it in its new role
            await self._run_attach(protocol)
            if protocol.player is not player:
                await self._settle_player(player)
            return
        self._store_protocol(protocol, player.player_id)
        player.refresh_state()
        self._schedule_held_protocols()

    async def _rollback_attach(self, protocol: PlayerProtocol, attach: _Attach) -> None:
        """
        Undo what a failed attach did, also when a detach took its protocol away already.

        :param protocol: The protocol object that failed to attach.
        :param attach: The state of that attach.
        """
        protocol_id = protocol.protocol_id
        if self._attaching.get(protocol_id) is attach:
            # no detach took the protocol off its player yet
            player = protocol.player
            if self._protocols.get(protocol_id) is protocol:
                del self._protocols[protocol_id]
            self._release_held_protocol(protocol)
            if player is not None:
                player.remove_protocol(protocol_id)
                protocol.set_player(None)
        self._discard_incomplete_attach(attach)
        # protocols claim resources (event subscriptions, connections) that only
        # on_detach releases, and a failing hook may have claimed them after a detach
        await self._run_on_detach(protocol)

    def _discard_incomplete_attach(self, attach: _Attach) -> None:
        """
        Drop what an attach that did not complete created and nobody saw yet.

        That is the player it created, and the config it stored for a new device or for
        a new protocol of a player: a device that is found again starts from scratch.

        :param attach: The state of the attach.
        """
        if (protocol_id := attach.new_protocol_record) and (
            record := self.mass.config.get_protocol_record(protocol_id)
        ):
            self.mass.config.remove_protocol_record(protocol_id)
            if player := self._players.get(str(record.get("player_id"))):
                player.remove_protocol(protocol_id, forget=True)
                if player.initialized.is_set():
                    player.refresh_state()
        player = attach.new_player
        if player is not None and player.initialized.is_set():
            return
        if player is not None and self._players.get(player.player_id) is player:
            del self._players[player.player_id]
        if (player_id := attach.minted_player_id) and player_id not in self._players:
            self.mass.config.remove(f"{CONF_PLAYERS}/{player_id}")
            for protocol_id in self.mass.config.get_protocol_ids_of_player(player_id):
                self.mass.config.remove_protocol_record(protocol_id)

    def _release_held_protocol(self, protocol: PlayerProtocol) -> None:
        """
        Stop holding a protocol that waited for its player.

        :param protocol: The protocol, which may not be held at all.
        """
        protocol_id = protocol.protocol_id
        if handle := self._pending_protocol_evaluations.pop(protocol_id, None):
            handle.cancel()
        if protocol.player is None and self._protocols.get(protocol_id) is protocol:
            del self._protocols[protocol_id]

    async def _get_protocol_config(self, protocol: PlayerProtocol) -> PlayerConfig:
        """
        Return the config of a protocol, switched on or off as it applies to the protocol.

        :param protocol: The protocol object, attached to its player.
        """
        config = await self.mass.config.get_protocol_config(protocol)
        record = self.mass.config.get_protocol_record(protocol.protocol_id) or {}
        if not self._link_applies(protocol, record.get("player_id")):
            # the switch of an output that another player lists is that player's business
            config.enabled = True
        return config

    def _wants_protocol(self, protocol_id: str, protocol: PlayerProtocol | None) -> bool:
        """
        Return whether the server wants a protocol attached now, see wants_protocol.

        :param protocol_id: Id of the protocol.
        :param protocol: The protocol object, None when it is not known.
        """
        if (record := self.mass.config.get_protocol_record(protocol_id)) is None:
            return True
        player_id = record.get("player_id")
        if not self._link_applies(protocol, player_id):
            # the record names another player than the one the protocol stands for
            return True
        if not record.get("enabled", True):
            return False
        if not player_id:
            return True
        return bool(self.mass.config.get(f"{CONF_PLAYERS}/{player_id}/enabled", True))

    async def _hold_derived_protocols_without_place(self, protocol_id: str) -> None:
        """
        Hold again the derived protocols that took the place of a protocol switched on again.

        A protocol that is away keeps the place of its domain for when it returns, once the
        user switches it on again.

        :param protocol_id: Id of the protocol the user switched on again.
        """
        player_id = self._get_player_id_of_protocol(protocol_id)
        if not player_id or (player := self._players.get(player_id)) is None:
            return
        derived = [
            protocol
            for protocol in player.protocols.values()
            if protocol.underlying_protocol_id
            and self._has_other_protocol_of_domain(protocol, player_id, include_away=True)
        ]
        for protocol in derived:
            self._attaching.pop(protocol.protocol_id, None)
            await self._unbind(protocol)
            self._hold_protocol(protocol)
        if derived:
            await self._settle_player(player)

    async def _settle_player(self, player: Player) -> None:
        """
        Publish a player that lost a protocol, or drop it when it only existed with it.

        :param player: The player a protocol was taken off.
        """
        if self._players.get(player.player_id) is not player or not player.initialized.is_set():
            # an attach that is still setting the player up drops it
            return
        if player.session_only and not player.protocols:
            await self._drop_player(player, permanent=False)
            self.logger.info("Player removed: %s", player.display_name)
            return
        player.set_config(await self.mass.config.get_player_object_config(player))
        player.update_state()

    def _attach_aborted(self, protocol: PlayerProtocol, attach: _Attach) -> bool:
        """
        Return True if an attach is not the current one of its protocol anymore.

        :param protocol: The protocol object that is being attached.
        :param attach: The state of the attach.
        """
        # an attach awaits provider I/O while the protocol is already bound, so a detach
        # (e.g. a provider unload or a device disconnect) can drop it in the meantime,
        # after which the attach must stop
        if self._attaching.get(protocol.protocol_id) is attach:
            return False
        self.logger.debug(
            "Attach of protocol %s aborted: it was detached while setting up",
            protocol.protocol_id,
        )
        return True

    async def _unbind(self, protocol: PlayerProtocol) -> Player | None:
        """
        Take a protocol object out of the registry and off its player, without publishing.

        Returns the player it was bound to, if any.

        :param protocol: The protocol object to unbind.
        """
        protocol_id = protocol.protocol_id
        if self._protocols.get(protocol_id) is protocol:
            del self._protocols[protocol_id]
        if handle := self._pending_protocol_evaluations.pop(protocol_id, None):
            handle.cancel()
        self._last_polls.pop(protocol_id, None)
        player = protocol.player
        if player is not None and player.protocols.get(protocol_id) is protocol:
            was_playback_path = player.resolve_output_protocol() is protocol
            player.remove_protocol(protocol_id)
            self._schedule_held_protocols()
            if was_playback_path and player.initialized.is_set():
                # the audio of the player went with this protocol: let the plugin owning
                # a live source release its session, and the queue go idle
                await self._release_audio_source(player.player_id)
                self.mass.player_queues.on_player_detached(player.player_id)
        protocol.set_player(None)
        await self._run_on_detach(protocol)
        return player

    async def _run_on_detach(self, protocol: PlayerProtocol) -> None:
        """
        Let a protocol object release what it claimed, whatever comes of it.

        :param protocol: The protocol object that was taken off its player.
        """
        # teardown is best-effort: a provider that fails to release its protocol must not
        # strand the other protocols of that provider, nor the provider unload itself,
        # nor mask the error of a failed attach
        try:
            await protocol.on_detach()
        except Exception:
            self.logger.exception("Error detaching protocol %s", protocol.protocol_id)

    def _create_player(self, player_id: str, protocol: PlayerProtocol | None = None) -> Player:
        """
        Create the player with the given id, which stays unpublished for now.

        :param player_id: Id of the player.
        :param protocol: The protocol the player is created for, when it has no config.
        """
        stored = self.mass.config.get(f"{CONF_PLAYERS}/{player_id}")
        provider_id = protocol.provider_id if protocol else ""
        config = self.mass.config.get_base_player_config(player_id, provider_id)
        if stored is None and protocol is not None:
            config.default_name = protocol.name
            config.player_type = protocol.type
        session_only = bool(
            (stored.get("values") or {}).get(CONF_SESSION_ONLY)
            if isinstance(stored, dict)
            else protocol is not None and protocol.session_only
        )
        player = Player(self.mass, player_id, config, session_only=session_only)
        player.protocol_ids.update(self.mass.config.get_protocol_ids_of_player(player_id))
        self._players[player_id] = player
        return player

    async def _publish_player(self, player: Player) -> None:
        """
        Publish a player that was created: to clients, and with its queue.

        :param player: The player, which is in the registry but not initialized yet.
        """
        player_id = player.player_id
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
                key=player_id,
                provider=self.domain,
                category=CACHE_CATEGORY_PLAYER_POWER,
                default=False,
            )
            if cached_value is not None:
                player.extra_data[ATTR_FAKE_POWER] = cached_value
        config = await self.mass.config.get_player_object_config(player)
        if self._players.get(player_id) is not player:
            # removed while the awaits above ran
            return
        player.set_config(config)
        player.update_state(signal_event=False)
        # now we're ready to signal the player is added
        player.set_initialized()
        self.logger.info(
            "Player (type %s) added: %s/%s",
            player.state.type.value,
            player_id,
            player.state.name,
        )
        self.mass.signal_event(EventType.PLAYER_ADDED, object_id=player_id, data=player)
        await self.mass.player_queues.on_player_register(player)

    async def _drop_player(self, player: Player, *, permanent: bool) -> None:
        """
        Take a player out of the registry and let clients know that it is gone.

        :param player: The player to drop, whose protocols are detached already.
        :param permanent: Also delete the saved queue of the player.
        """
        player_id = player.player_id
        # a player that is going away is done with any live source it was playing,
        # so let the owning plugin release an upstream session pointing at us
        await self._release_audio_source(player_id)
        if self._players.get(player_id) is player:
            del self._players[player_id]
        # clean up all lock entries for this player
        for prefix in [p.value for p in PlayerLockPurpose]:
            self._player_command_locks.pop(f"{prefix}_{player_id}", None)
        self._clear_sleep_timer(player)
        self.mass.cancel_timer(f"player_update_state_{player_id}")
        self.mass.player_queues.on_player_remove(player_id, permanent=permanent)
        if player.initialized.is_set() and not self.mass.closing:
            self.mass.signal_event(EventType.PLAYER_REMOVED, player_id)

    async def _remove_player(self, player_id: str, *, rediscover: bool) -> None:
        """
        Remove a player for good, with everything that is stored about it.

        :param player_id: Id of the player to remove.
        :param rediscover: Ask the providers of its protocols to look for their devices
            again afterwards, so a device that is still there comes back as a new player.
        """
        player = self._players.get(player_id)
        stored = self.mass.config.get(f"{CONF_PLAYERS}/{player_id}")
        protocol_ids = set(self.mass.config.get_protocol_ids_of_player(player_id))
        if player is not None:
            protocol_ids.update(player.protocols)
        if player is None and stored is None and not protocol_ids:
            return
        self._removing.add(player_id)
        try:
            if player is not None:
                await self._release_player_that_is_removed(player)
            is_group = (
                player.type if player else (stored or {}).get("player_type")
            ) == PlayerType.GROUP
            provider_ids = {
                str(record.get("provider"))
                for protocol_id in protocol_ids
                if (record := self.mass.config.get_protocol_record(protocol_id))
            }
            # the objects the providers hold, also of a protocol that is not attached
            protocols = [
                protocol
                for protocol_id in protocol_ids
                if (
                    protocol := self._protocols.get(protocol_id)
                    or self._unattached.get(protocol_id)
                )
                # one that connected as a player of its own meanwhile belongs to that player
                and protocol.player in (None, player)
            ]
            for protocol in protocols:
                provider_ids.add(protocol.provider_id)
                self._attaching.pop(protocol.protocol_id, None)
                self._unattached.pop(protocol.protocol_id, None)
                await self._unbind(protocol)
            # No group detach is issued for its persisted memberships: the player is out of
            # the registry, so it is filtered out of every group's live member list, and
            # its persisted membership is settled by the config cleanup below.
            if player is not None:
                await self._drop_player(player, permanent=True)
            # a protocol found again during the awaits above belongs to another player now
            self._delete_player_config(
                player_id, set(self.mass.config.get_protocol_ids_of_player(player_id))
            )
        finally:
            self._removing.discard(player_id)
        self.logger.info("Player removed: %s", player.display_name if player else player_id)
        for protocol in protocols:
            if protocol.protocol_id in self._protocols or protocol.protocol_id in self._unattached:
                # its device was found again meanwhile, so its provider still needs it
                continue
            # the provider only cleans up what it keeps about the device itself.
            # Best-effort: the player is gone by now, whatever happens to that
            try:
                await protocol.on_remove()
            except Exception:
                self.logger.exception("Error removing protocol %s", protocol.protocol_id)
        if not rediscover or is_group:
            # a group is no device on the network: with its config gone there is nothing
            # left for its provider to find
            return
        for provider_id in provider_ids:
            if isinstance(provider := self.mass.get_provider(provider_id), PlayerProvider):
                # a device still on the network is found again as a new player
                provider.schedule_discovery()

    def _delete_player_config(self, player_id: str, protocol_ids: set[str]) -> None:
        """
        Permanently delete everything stored about a player.

        That is its own config and those of its protocols, its DSP and queue settings and
        its saved queue, so a device that returns starts out fresh. Any group that lists
        the player as a member loses the member, and so do the user access filters.

        :param player_id: Id of the player to delete the configuration of.
        :param protocol_ids: Ids of the protocols that belong to the player.
        """
        self._update_group_memberships(player_id)
        for protocol_id in protocol_ids:
            self.mass.config.remove_protocol_record(protocol_id)
        for key in (
            f"{CONF_PLAYERS}/{player_id}",
            f"{CONF_PLAYER_DSP}/{player_id}",
            f"{CONF_PLAYER_QUEUES}/{player_id}",
        ):
            self.mass.config.remove(key)
        if player_id not in self._players:
            self.mass.player_queues.purge_saved_queue(player_id)
        self._build_legacy_id_index()
        # a user access filter is an allow-list of player ids, so it must not be left
        # pointing at a player whose config was just wiped
        self.mass.create_task(
            self.mass.webserver.auth.remove_from_user_filters(player_ids=[player_id])
        )

    def _store_player_base(self, player: Player) -> None:
        """
        Store the provider and type a player has now, for when none of its protocols is attached.

        :param player: The player, which may have no stored config at all.
        """
        conf_key = f"{CONF_PLAYERS}/{player.player_id}"
        if not isinstance(stored := self.mass.config.get(conf_key), dict):
            return
        if stored.get("provider") != player.provider_id:
            self.mass.config.set(f"{conf_key}/provider", player.provider_id)
        if stored.get("player_type") != player.type:
            self.mass.config.set(f"{conf_key}/player_type", player.type)

    def _get_player_id_of_protocol(self, protocol_id: str) -> str | None:
        """
        Return the id of the player a protocol belongs to, attached or not.

        :param protocol_id: Id of the protocol.
        """
        if (protocol := self._protocols.get(protocol_id)) and protocol.player:
            return protocol.player.player_id
        if record := self.mass.config.get_protocol_record(protocol_id):
            return cast("str | None", record.get("player_id"))
        return None

    async def _restore_players(self) -> None:
        """Bring back every stored player that is enabled and kept while its device is away."""
        stored_players = self.mass.config.get(CONF_PLAYERS, {})
        for player_id, stored in list(stored_players.items()):
            if not isinstance(stored, dict) or not stored.get("enabled", True):
                continue
            if player_id in self._players:
                continue
            try:
                if (stored.get("values") or {}).get(CONF_SESSION_ONLY):
                    continue
                await self._publish_player(self._create_player(player_id))
            except Exception:
                self.logger.exception("Could not restore player %s", player_id)
                self._players.pop(player_id, None)

    async def _disable_player(self, player: Player) -> None:
        """
        Take a player the user disabled out of use: the player goes, its config stays.

        :param player: The player to disable.
        """
        await self._stop_player_that_goes(player)
        for protocol in list(player.protocols.values()):
            self._attaching.pop(protocol.protocol_id, None)
            await self._unbind(protocol)
            # its provider keeps the object, which a removal of the player still has to reach
            self._unattached[protocol.protocol_id] = protocol
        await self._drop_player(player, permanent=False)
        self.logger.info("Player disabled: %s", player.display_name)
        self._schedule_update_all_players()

    async def _stop_player_that_goes(self, player: Player) -> None:
        """
        Stop what a player plays, or power it off, before it is disabled.

        :param player: The player that goes.
        """
        if not player.state.available:
            return
        # edge case: ensure that the player is powered off if the player gets disabled
        if player.state.power_control != PLAYER_CONTROL_NONE:
            await self._handle_cmd_power(player.player_id, False)
        elif player.state.playback_state != PlaybackState.IDLE:
            await self.cmd_stop(player.player_id)

    async def _release_player_that_is_removed(self, player: Player) -> None:
        """
        End the playback a player owns before it is removed, without powering it off.

        A member of a group or of a sync leader only leaves it, and the group plays on. A
        command that fails is logged: the removal goes on regardless.

        :param player: The player that is removed.
        """
        try:
            if self._resolve_playback_owner(player) is player:
                if (
                    player.available_for_playback
                    and player.state.playback_state != PlaybackState.IDLE
                ):
                    await self._handle_cmd_stop(player.player_id)
                return
            # check for group memberships that need to be updated: a static group refuses
            # to let one member go and plays on, the member goes anyway
            with suppress(
                UnsupportedFeaturedException, PlayerCommandFailed, PlayerUnavailableError
            ):
                if (
                    player.state.active_group
                    and (group_player := self.get_player(player.state.active_group))
                    and (group_protocol := group_player.own_protocol)
                ):
                    # try to remove from the group
                    await group_protocol.set_members(player_ids_to_remove=[player.player_id])
                elif player.state.synced_to:
                    await self._detach_member(player)
        except Exception:
            self.logger.exception(
                "Error ending the playback of %s before its removal", player.display_name
            )

    async def _enable_player(self, player_id: str) -> None:
        """
        Bring back a player the user enabled, and let its providers look for its device.

        :param player_id: Id of the player, whose config is stored as enabled already.
        """
        stored = self.mass.config.get(f"{CONF_PLAYERS}/{player_id}")
        if not isinstance(stored, dict):
            return
        # a player that only exists while its device is connected comes back when it attaches
        if not (stored.get("values") or {}).get(CONF_SESSION_ONLY):
            await self._publish_player(self._create_player(player_id))
        provider_ids = {
            str(record.get("provider"))
            for protocol_id in self.mass.config.get_protocol_ids_of_player(player_id)
            if (record := self.mass.config.get_protocol_record(protocol_id))
        }
        for provider_id in provider_ids:
            if isinstance(provider := self.mass.get_provider(provider_id), PlayerProvider):
                provider.schedule_discovery()

    async def _restart_playback_after_config_change(
        self, player: Player, requires_restart: bool
    ) -> None:
        """
        Restart the queue a player is playing, for a changed setting that needs it.

        :param player: The player whose config changed.
        :param requires_restart: Whether a setting changed that only applies to a new stream.
        """
        if not requires_restart or not player.state.active_source:
            return
        resume_queue = self.mass.player_queues.get(player.state.active_source)
        # if the PlayerQueue was playing, restart playback
        if resume_queue and resume_queue.state == PlaybackState.PLAYING:
            # always stop first to ensure the player uses the new config
            await self.mass.player_queues.stop(resume_queue.queue_id)
            self.mass.call_later(1, self.mass.player_queues.resume, resume_queue.queue_id, False)

    def _get_transport_protocol(self, player: Player) -> PlayerProtocol:
        """
        Return the protocol that takes the transport commands for what the device plays itself.

        That is the audio protocol whose app is shown on the player, else its own protocol.

        :param player: The player the command was issued on.
        :raises PlayerUnavailableError: When the player has neither of them attached.
        """
        if protocol := player.foreign_protocol or player.own_protocol:
            return protocol
        msg = f"Player {player.display_name} has no protocol that can take this command"
        raise PlayerUnavailableError(msg)

    async def _resolve_mac_addresses(self, protocol: PlayerProtocol) -> None:
        """
        Resolve the MAC addresses used to match the protocol to its player.

        :param protocol: The protocol to resolve the MAC address(es) for.
        """
        protocol_id = protocol.protocol_id
        config = self.mass.config
        # Save the original MAC reported by the provider (before ARP enrichment)
        reported_mac = protocol.device_info.identifiers.get(IdentifierType.MAC_ADDRESS)

        # Try to use cached ARP MAC from config for fast matching on restart.
        # This allows matching to work immediately even if ARP is slow/fails.
        cached_arp_mac = config.get_protocol_value(protocol_id, CONF_CACHED_ARP_MAC)
        if isinstance(cached_arp_mac, str) and is_valid_mac_address(cached_arp_mac):
            protocol.device_info.add_identifier(IdentifierType.MAC_ADDRESS, cached_arp_mac)

        # Enrich device MAC address via ARP if needed
        # (handles invalid MACs, locally-administered MACs, and missing MACs)
        await enrich_device_mac_address(protocol.device_info, self.logger)

        # Keep the original reported MAC if it differs from the resolved MAC.
        # This enables multi-MAC matching for devices with multiple interfaces
        # (e.g., WiFi + Ethernet) where ARP resolves one interface but the
        # protocol reports the other.
        current_mac = protocol.device_info.identifiers.get(IdentifierType.MAC_ADDRESS)
        if reported_mac and is_valid_mac_address(reported_mac) and current_mac:
            # Provider's reported MAC matching the resolved MAC is no second address
            differs = reported_mac.upper() != current_mac.upper()
            protocol.set_reported_mac(reported_mac if differs else None)
        elif not reported_mac or not is_valid_mac_address(reported_mac):
            # Restore reported MAC from config on restart only when the provider
            # did not supply a usable MAC address.
            cached_reported_mac = config.get_protocol_value(protocol_id, CONF_REPORTED_MAC)
            if (
                isinstance(cached_reported_mac, str)
                and is_valid_mac_address(cached_reported_mac)
                and not (current_mac and cached_reported_mac.upper() == current_mac.upper())
            ):
                protocol.set_reported_mac(cached_reported_mac)

    def _teardown_in_progress(self, protocol: PlayerProtocol) -> bool:
        """
        Return True if the server or the provider of this protocol is shutting down.

        :param protocol: The protocol that is in the process of being attached.
        """
        return self.mass.closing or protocol.provider.unloading

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

        Grouped/synced members mirror their parent's
        current_media (palette included), so they must not resolve it themselves.

        :param player: The player to check.
        """
        state = player.state
        # a self-referential active_group/synced_to is not a real parent (mirror the
        # != self guard of the state derivation), so it must not skip resolution
        parent_id = state.active_group or state.synced_to
        return bool(parent_id and parent_id != player.player_id)

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
            if (value := self.mass.config.get_player_value(player_id, conf_key))
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
                self.mass.config.get_player_value(
                    player_id, CONF_MIN_VOLUME, CONF_ENTRY_MIN_VOLUME.default_value
                ),
            )
        )
        max_volume = int(
            cast(
                "int",
                self.mass.config.get_player_value(
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
        """Forward a player state update to related players (groups, sync parent)."""
        # TODO: make this fan-out change-aware (skip relatives that derive nothing from
        # the changed fields) once reverse indexes for synced_to/active_group exist.
        # Propagate group or sync-leader updates to child players.
        if player.state.group_members:
            for child_player in self.iter_group_members(player, exclude_self=True):
                for protocol in list(child_player.protocols.values()):
                    if player.type == PlayerType.GROUP:
                        protocol.on_group_updated(player, changed_values)
                    else:
                        protocol.on_sync_parent_updated(player, changed_values)
        # update/signal group player(s) when a member updates. A sync leader is a member of the
        # group player that formed the sync group and gaining members of its own does not change
        # that: a group player mirrors its leader, so it depends on exactly these updates.
        for group_player in self._get_player_groups(player):
            if group_protocol := group_player.own_protocol:
                group_protocol.on_group_member_updated(player, changed_values)
            else:
                self.trigger_player_update(group_player.player_id)

        # update/signal manually sync-parent player when child updates
        if (_sync_parent_id := player.state.synced_to) and (
            _sync_parent := self.get_player(_sync_parent_id)
        ):
            self.trigger_player_update(_sync_parent.player_id)

    def _trigger_provider_players_update(self, player: Player) -> None:
        """
        Trigger an update of the players that share a provider with the given player.

        :param player: The player whose grouping or availability changed.
        """
        for protocol in player.protocols.values():
            for related in self.iter_protocols(protocol.provider.instance_id):
                if related.player is not None and related.player is not player:
                    self.trigger_player_update(related.player.player_id, debounce_delay=2)

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
        # so we drop it from its parent group/leader directly. The same goes for a player
        # that is offline because it is switched off at its power control, which is still
        # reported as available.
        became_inactive = (
            (ATTR_AVAILABLE in changed_values and changed_values[ATTR_AVAILABLE][1] is False)
            or (ATTR_ENABLED in changed_values and changed_values[ATTR_ENABLED][1] is False)
            or player.offline_by_power_control
        )
        if became_inactive and (player.state.active_group or player.state.synced_to):
            self.mass.create_task(self._cleanup_player_memberships(player.player_id))

        # A player whose power was turned off outside of an MA power command (e.g. its
        # linked power control was switched off directly) must be unsynced too. We act
        # only on an explicit on->off transition, leaving players without power control
        # (powered == None) untouched. The player is still reachable here, so we route
        # through cmd_ungroup, which also hands over the playback of a sync leader that
        # plays its own queue.
        if changed_values.get(ATTR_POWERED) != (True, False):
            return
        # An MA power command ungroups the player itself before it flips the power, and a
        # provider may keep listing the followers on a leader for a while after that
        # (Sonos does), so the transition it causes would ungroup a second time. The
        # marker expires: a command whose device never reported off must not claim the
        # next external power off.
        issued_at = player.extra_data.pop(ATTR_POWER_OFF_IN_PROGRESS, None)
        if issued_at is not None and time.monotonic() - issued_at < POWER_OFF_IN_PROGRESS_EXPIRY:
            return
        if player.offline_by_power_control:
            # it went offline along with its power, so it was detached above instead
            return
        if player.state.type in UNGROUP_ON_POWER_OFF_TYPES and (
            player.state.synced_to or player.state.active_group or player.state.group_members
        ):
            self.mass.create_task(self.cmd_ungroup(player.player_id))

    def _handle_external_power_off(
        self, player: Player, changed_values: dict[str, tuple[Any, Any]]
    ) -> None:
        """End the queue of a player whose power was turned off outside of MA."""
        if (
            changed_values.get(ATTR_POWERED) != (True, False)
            or player.state.type not in PLAYBACK_TARGET_TYPES
        ):
            return
        if (
            player.state.synced_to
            or player.state.active_group
            or (player.state.group_members and player.state.type in UNGROUP_ON_POWER_OFF_TYPES)
        ):
            # a player that is a member of a group, or a sync leader handing its
            # leadership on, is detached instead and the group's own power off ends the
            # queue. a group player leads its own members, so its queue is ended here.
            return
        # a device that powers itself off may report its stop in this very update, so
        # judge on the playback state as it was before it - which is also the snapshot
        # an MA power off works from
        prev_playback_state = (
            changed_values["playback_state"][0]
            if "playback_state" in changed_values
            else player.state.playback_state
        )
        if prev_playback_state not in (PlaybackState.PLAYING, PlaybackState.PAUSED):
            return
        self.mass.call_later(
            EXTERNAL_POWER_OFF_STOP_DELAY,
            self._stop_queue_on_external_power_off,
            player.player_id,
            task_id=f"external_power_off_stop_{player.player_id}",
        )

    async def _stop_queue_on_external_power_off(self, player_id: str) -> None:
        """
        End the queue of a player that is still powered off.

        :param player_id: The player whose power was turned off outside of MA.
        """
        # hold the playback lock across the checks: a power on that overlaps the wait
        # holds it while it resumes the queue, and reading the power state before that
        # completes would stop the playback it just started (the lock is re-entrant per
        # task, so the stop below re-acquiring it is a no-op)
        async with self.get_player_lock(player_id, PlayerLockPurpose.PLAYBACK):
            if not (player := self.get_player(player_id)) or player.state.powered is not False:
                # the power came back on while we were waiting it out
                return
            # only the player's own queue: get_active_queue resolves to another player's
            # queue as soon as this one is hearing someone else's audio
            active_queue = self.get_active_queue(player)
            if active_queue is None or active_queue.queue_id != player_id:
                return
            # a player gone unavailable along with its power cannot be told to stop,
            # but the queue teardown that matters here runs regardless
            with suppress(PlayerUnavailableError):
                await self.mass.player_queues._handle_stop(player_id)

    async def _cleanup_player_memberships(self, player_id: str) -> None:
        """Ensure a player is detached from any groups or syncgroups."""
        if not (player := self.get_player(player_id)):
            return
        try:
            if parent_id := (player.state.active_group or player.state.synced_to):
                # the player is part of a (permanent) groupplayer and the user tries to ungroup
                if parent_player := self.get_player(parent_id):
                    await self._handle_set_members(parent_player, player_ids_to_remove=[player_id])
                return
        except (UnsupportedFeaturedException, PlayerCommandFailed, PlayerUnavailableError) as err:
            self.logger.debug(
                "Could not detach player %s from its group: %s", player.display_name, err
            )

    def _get_reachable_player(self, player_id: str) -> Player:
        """
        Return the player, provided it can take a command right now.

        :param player_id: The player to get.
        :raises PlayerUnavailableError: When the player is unavailable, or switched off at
            its external power control.
        """
        player = self.get_player(player_id, True)
        assert player is not None  # for type checking
        if player.offline_by_power_control:
            msg = f"Player {player.display_name} is switched off"
            raise PlayerUnavailableError(msg)
        return player

    def _get_player_with_redirect(self, player_id: str) -> Player:
        """Get player with check if playback related command should be redirected."""
        player = self.get_player(player_id, True)
        assert player is not None  # for type checking
        target = self._resolve_playback_owner(player)
        if target is not player:
            self.logger.info(
                "Player %s is synced or grouped and can not accept playback related "
                "commands itself, redirected the command to %s.",
                player.display_name,
                target.display_name,
            )
        return target

    def _resolve_playback_owner(self, player: Player) -> Player:
        """
        Return the player that owns the given player's playback.

        That is its sync leader, or the group player capturing that leader.
        A player that is not synced or grouped owns its own playback.

        :param player: The player to resolve the playback owner for.
        """
        owner = player
        if owner.state.synced_to and (sync_leader := self.get_player(owner.state.synced_to)):
            owner = sync_leader
        # a captured sync leader hands its playback on to its group, whose lock has
        # to be taken first (see get_group_and_player_lock)
        if owner.state.active_group and (group := self.get_player(owner.state.active_group)):
            owner = group
        return owner

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

        Mirrors ``get_active_queue``: a sync child hears its leader and a group member
        hears its group.

        :param player: The player to resolve the owner for.
        """
        if player.state.synced_to and player.state.synced_to != player.player_id:
            if sync_leader := self.get_player(player.state.synced_to):
                return self._audio_source_owner(sync_leader)
        if player.state.active_group and player.state.active_group != player.player_id:
            if group_player := self.get_player(player.state.active_group):
                return self._audio_source_owner(group_player)
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

    def _build_legacy_id_index(self) -> None:
        """Build the lookup of legacy ids from the stored player configs."""
        legacy_ids: dict[str, str] = {}
        self._legacy_ids = legacy_ids
        stored_configs = self.mass.config.get(CONF_PLAYERS, {})
        if not isinstance(stored_configs, dict):
            return
        for player_id, stored_config in stored_configs.items():
            values = stored_config.get("values") if isinstance(stored_config, dict) else None
            stored_ids = values.get(CONF_LEGACY_IDS) if isinstance(values, dict) else None
            if not isinstance(stored_ids, list):
                continue
            for legacy_id in stored_ids:
                # of several players that hold the same legacy id the first one keeps it
                if isinstance(legacy_id, str):
                    legacy_ids.setdefault(legacy_id, player_id)

    async def _poll_players(self) -> None:
        """Background task that polls the protocols of the players for updates."""
        while True:
            for player in list(self._players.values()):
                self._check_idle_power_off(player)
                # if the player is playing, update elapsed time every tick
                # to ensure the queue has accurate details
                if player.state.playback_state == PlaybackState.PLAYING:
                    self.mass.call_later(
                        0.5,
                        self.mass.player_queues.on_player_update,
                        player,
                        {"corrected_elapsed_time": (None, player.state.corrected_elapsed_time)},
                        task_id=f"queue_on_player_update_{player.player_id}",
                    )
            for protocol in list(self._protocols.values()):
                # Poll protocol;
                if not protocol.needs_poll:
                    continue
                last_poll = self._last_polls.get(protocol.protocol_id, 0.0)
                if (self.mass.loop.time() - last_poll) < protocol.poll_interval:
                    continue
                if self._protocols.get(protocol.protocol_id) is not protocol:
                    # detached while the protocols before it were polled
                    continue
                self._last_polls[protocol.protocol_id] = self.mass.loop.time()
                try:
                    await protocol.poll()
                except PlayerUnavailableError:
                    self.logger.debug("Player %s is unavailable", protocol.display_name)
                except Exception as err:
                    self.logger.warning(
                        "Error while requesting latest state from player %s: %s",
                        protocol.display_name,
                        str(err),
                        exc_info=err if self.logger.isEnabledFor(10) else None,
                    )
                # Yield to event loop to prevent blocking
                await asyncio.sleep(0)
            await asyncio.sleep(1)

    def _check_idle_power_off(self, player: Player) -> None:
        """
        Power off a player once it has been idle for its configured idle power off time.

        :param player: The player to check.
        """
        if not self._get_idle_power_off_minutes(player):
            player.extra_data.pop(ATTR_IDLE_SINCE, None)
            return
        player.extra_data.setdefault(ATTR_IDLE_SINCE, time.monotonic())
        if self._is_idle_power_off_due(player):
            # the task id keeps a power off that waits for the player lock from starting twice
            self.mass.create_task(
                self._power_off_idle_player(player.player_id),
                task_id=f"idle_power_off_{player.player_id}",
            )

    def _restart_idle_count(
        self, player: Player, changed_values: dict[str, tuple[Any, Any]]
    ) -> None:
        """
        Start the idle power off count of a player over when its playback or power changed.

        :param player: The player whose state changed.
        :param changed_values: The changed state values.
        """
        # also catches playback that starts and ends between two checks of the poll loop
        if changed_values.keys() & {"playback_state", ATTR_POWERED}:
            player.extra_data.pop(ATTR_IDLE_SINCE, None)

    async def _power_off_idle_player(self, player_id: str) -> None:
        """
        Power off a player that has been idle for its configured idle power off time.

        :param player_id: The player to power off.
        """
        try:
            # a command that holds the player may play on it, which starts the count over.
            # So never run without the lock, and check again once it is ours.
            async with self.get_group_and_player_lock(player_id, strict=True):
                player = self.get_player(player_id)
                if player is None or not self._is_idle_power_off_due(player):
                    return
                # starts the count over, so a power off that fails is retried a full idle
                # time later
                player.extra_data.pop(ATTR_IDLE_SINCE)
                self.logger.info("Powering off idle player %s", player.display_name)
                await self._handle_cmd_power(player_id, False)
        except ResourceBusyError:
            self.logger.debug("Player %s is busy, its idle power off is tried again", player_id)

    def _is_idle_power_off_due(self, player: Player) -> bool:
        """
        Return if the player has been idle for its configured idle power off time.

        :param player: The player to check.
        """
        minutes = self._get_idle_power_off_minutes(player)
        idle_since = player.extra_data.get(ATTR_IDLE_SINCE)
        return (
            bool(minutes)
            and idle_since is not None
            and time.monotonic() - idle_since >= minutes * 60
        )

    @staticmethod
    def _get_idle_power_off_minutes(player: Player) -> int:
        """
        Return after how many idle minutes the player is to be powered off.

        Returns 0 while that does not apply: the player has no idle power off configured, is
        not online, powered on and stopped, or is about to play an announcement.

        :param player: The player to get the idle power off time for.
        """
        minutes = player.config.get_value(CONF_IDLE_POWER_OFF)
        state = player.state
        if (
            not isinstance(minutes, int)
            or minutes <= 0
            or not state.available
            or state.powered is not True
            or state.playback_state != PlaybackState.IDLE
            or player.extra_data.get(ATTR_ANNOUNCEMENT_IN_PROGRESS)
            # reported as powered on and idle while it is still starting up
            or player.offline_by_power_control
        ):
            return 0
        return minutes

    def _handle_group_dsp_change(
        self, player: Player, prev_group_members: list[str], new_group_members: list[str]
    ) -> None:
        """Handle DSP reload when group membership changes."""
        # reset cached group volume snapshot since membership changed
        player.extra_data.pop(ATTR_GROUP_VOLUME_SNAPSHOT, None)
        if player.state.synced_to and player.state.synced_to != player.player_id:
            # a sync child renders its leader's stream, so its own DSP setting cannot
            # require a restart (a leader that just became a child reports its members
            # as gone, which would otherwise read as the group shrinking)
            return
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
            self.mass.create_task(
                self.mass.players.on_player_dsp_change(player.player_id, after_group_change=True)
            )

    async def _resume_queue_after_dsp_change(self, queue_id: str) -> None:
        """Resume a queue to apply a DSP change, unless it was emptied while waiting for the lock."""
        # a running command (e.g. a leadership transfer) may move the queue away meanwhile
        async with self.get_group_and_player_lock(queue_id, strict=True):
            queue = self.mass.player_queues.get(queue_id)
            if queue is None or not queue.items:
                self.logger.debug("Not restarting queue %s after DSP change: it is empty", queue_id)
                return
            await self.mass.player_queues.resume(queue_id, False)

    def _check_external_source_takeover(self, player: Player) -> None:
        """
        Handle when an external source takes over playback on a player.

        When a player has an active grouped output protocol (e.g., AirPlay group) and
        an external source (e.g., Spotify Connect, TV input) takes over playback,
        we need to clear the active output protocol and ungroup the protocols.

        This prevents the situation where the player appears grouped via protocol
        but is actually playing from a different source.

        :param player: The player whose active_source changed.
        """
        # Not a takeover if the device itself is not actively playing
        own = player.own_protocol
        if own is None or own.playback_state != PlaybackState.PLAYING:
            return

        # Only relevant if we have an active output protocol (not native)
        if not player.active_output_protocol or player.active_output_protocol == "native":
            return

        new_source = player.state.active_source

        # Check if new source is external (not MA-managed)
        if self._is_ma_managed_source(player, new_source):
            return

        # Get the active protocol
        protocol_player = player.protocols.get(player.active_output_protocol)
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

        # Ungroup the protocol (async task)
        self.mass.create_task(protocol_player.ungroup())

    def _is_ma_managed_source(self, player: Player, source: str | None) -> bool:
        """
        Check if a source is managed by Music Assistant.

        MA-managed sources include:
        - None (=autodetect, no source explicitly set by player)
        - The player's own ID (MA queue)
        - Any active queue ID
        - Any live AudioSource session

        :param player: The player to check.
        :param source: The source ID to check.
        :return: True if the source is MA-managed, False if external.
        """
        if source is None:
            return True

        # Player's own ID means MA queue is active
        if source == player.player_id:
            return True

        # A live AudioSource (e.g. Spotify Connect) is streamed by MA itself, so it is
        # MA that put it on the player rather than something taking the player over
        if self.is_live_audio_source(source):
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

        for player in self.iter_players(return_unavailable=True, return_disabled=False):
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
            player.display_name,
            player.state.synced_to or player.state.active_group,
            log_context,
        )
        # Use internal _handle_set_members to avoid deadlocking on the play lock
        # (we're already inside a cmd_set_members chain that holds a play lock).
        synced_to = player.state.synced_to or player.state.active_group
        parent = self.get_player(synced_to) if synced_to else None
        if parent or player.state.synced_to:
            try:
                async with self.wait_for_player_update(player.player_id, timeout=5):
                    if player.state.synced_to:
                        await self._detach_member(player)
                    elif parent:
                        await self._handle_set_members(
                            parent, player_ids_to_remove=[player.player_id]
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.warning(
                    "Failed to auto-ungroup %s from %s, proceeding anyway",
                    player.display_name,
                    synced_to,
                )

    async def _detach_member(self, member: Player) -> None:
        """
        Remove a sync member from the player it is synced to.

        Skips permission checks and locking (internal use only).

        :param member: The sync member to detach.
        """
        parent = self.get_player(member.state.synced_to) if member.state.synced_to else None
        if parent and parent.available_for_playback:
            self.logger.debug(
                "Removing %s from its sync leader %s", member.display_name, parent.display_name
            )
            await self._handle_set_members(parent, player_ids_to_remove=[member.player_id])
            return
        # a sync leader that is gone (or offline) can't release its members, so the
        # member has to leave the group from its own side: natively, or through the
        # audio protocol that holds the membership (e.g. an AirPlay group)
        target = member.own_protocol
        if target is None or not target.synced_to:
            target = next(
                (protocol for protocol in member.audio_protocols if protocol.synced_to), target
            )
        msg = (
            f"{member.display_name} is synced to the unavailable player "
            f"{member.state.synced_to} and could not leave the group on its own"
        )
        if target is None:
            raise PlayerUnavailableError(msg)
        self.logger.debug(
            "Sync leader %s of %s is unavailable, letting %s leave the group on its own",
            member.state.synced_to,
            member.display_name,
            target.protocol_id,
        )
        try:
            await target.ungroup()
        except asyncio.CancelledError:
            raise
        except Exception as err:
            raise PlayerUnavailableError(msg) from err

    async def _dissolve_own_group(self, player: Player, target_name: str) -> bool:
        """
        Dissolve the group a player leads before it joins another group.

        Only a player whose members ride its own stream has a group to give up: it stops
        rendering that stream the moment it joins the other group, which would leave its
        members silent while they still show up as grouped. A group that is still playing
        is refused instead, so the player stays out rather than silencing its members.

        :param player: The player about to join another group.
        :param target_name: Name of the group being joined, for the log messages.
        :return: False when the group is still in place, so the player must not join.
        """
        own = player.own_protocol
        if own is None or not own.native_grouping_requires_own_stream:
            return True
        members = [
            member_id for member_id in player.state.group_members if member_id != player.player_id
        ]
        if not members:
            return True
        if player.state.playback_state in (PlaybackState.PLAYING, PlaybackState.PAUSED):
            # Grouping never offers a rendering leader as a target, so reaching this means
            # the caller worked from a can_group_with snapshot taken before the playback
            # started. Tearing the group down now would cut its members off mid-track.
            self.logger.warning(
                "Player %s is still serving its own group, leaving it out of %s",
                player.display_name,
                target_name,
            )
            return False
        self.logger.info(
            "Player %s leads a group of its own, dissolving it before it joins %s",
            player.display_name,
            target_name,
        )
        # Removing the members rather than the leader keeps this out of the leadership
        # transfer path, and the internal handler avoids deadlocking on the play lock
        # (we're already inside a cmd_set_members chain that holds it).
        try:
            await self._handle_set_members(player, player_ids_to_remove=members)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Joining on top of a group it still leads is the silent-member state this
            # dissolve exists to prevent, and one the player can no longer be talked out of.
            self.logger.warning(
                "Could not dissolve the group of %s, leaving it out of %s",
                player.display_name,
                target_name,
            )
            return False
        return True

    @api_command("players/cmd/set_members", required_scope=Scope.PLAYERS_CONTROL)
    async def _api_cmd_set_members(
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
        :raises InsufficientPermissions: if the user may not use a player whose grouping changes.
        """
        self.check_grouping_access(target_player, player_ids_to_add, player_ids_to_remove)
        await self.cmd_set_members(target_player, player_ids_to_add, player_ids_to_remove)

    def _get_set_members_group(self, player: Player) -> Player | None:
        """
        Return the group player that manages the members of the given player, if any.

        :param player: The target player of a set_members command.
        """
        if (
            player.type != PlayerType.GROUP
            and player.state.active_group
            and (group_player := self._resolve_playback_owner(player)).type == PlayerType.GROUP
            and PlayerFeature.SET_MEMBERS in group_player.state.supported_features
        ):
            return group_player
        return None

    async def _handle_set_members(  # noqa: PLR0915
        self,
        parent_player: Player,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
        *,
        new_content: bool = False,
    ) -> tuple[PlayerProtocol, object] | None:
        """
        Handle the actual set_members logic.

        Skips permission checks and locking (internal use only).

        :param parent_player: The parent player to add/remove members to/from.
        :param player_ids_to_add: List of player_id's to add to the parent player.
        :param player_ids_to_remove: List of player_id's to remove from the parent player.
        :param new_content: Set when the caller is about to publish new content, so the
            protocol owner can clear its stale metadata before the member joins.
        :return: The opened takeover transaction as a ``(owner, token)`` pair, or None
            when there is nothing to finish. On success the caller owns the returned
            transaction and must call ``on_group_content_takeover_finished`` or
            ``on_group_content_takeover_aborted`` on it; a raised call is already aborted.
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
                and member.available_for_playback
            ]
            # a new leader must be able to render audio: a group with only display,
            # visualizer or lighting members left has no playback heir and dissolves
            has_playback_heir = any(
                (member := self.get_player(m)) and member.state.type in PLAYBACK_TARGET_TYPES
                for m in remaining_members
            )
            # only a queue of the leader's own is handed over: the elected leader of a
            # group player plays the group's queue, and that group re-forms around another
            # member itself (its native followers may still linger on it after a dissolve)
            active_queue = self.get_active_queue(parent_player)
            if (
                has_playback_heir
                and active_queue
                and active_queue.queue_id == target_player
                and active_queue.state != PlaybackState.IDLE
            ):
                # transfer leadership to a remaining member instead of dissolving
                await self._transfer_ad_hoc_leadership(parent_player, remaining_members)
                return None
            self.logger.info(
                "Dissolving sync group of player %s as it is being removed from itself",
                parent_player.display_name,
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
                or not child_player.available_for_playback
            ):
                self.logger.warning("Player %s is not available", child_player_id)
                continue

            # check if player can be synced/grouped with the target player
            # state.can_group_with already handles all expansion and translation
            if child_player_id not in parent_player.state.can_group_with:
                self.logger.warning(
                    "Player %s can not be grouped with %s",
                    child_player.display_name,
                    parent_player.display_name,
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
                await self._auto_ungroup_if_synced(
                    child_player, f"joining {parent_player.display_name}"
                )

            # handle edge case: the child leads a native group of its own, which it cannot
            # keep serving from inside this one
            if not await self._dissolve_own_group(child_player, parent_player.state.name):
                continue

            await self._power_on_for_playback(child_player)
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
                # parent's group_members state is stale/not yet updated.
                child_player = self.get_player(child_player_id)
                if child_player and target_player == child_player.state.synced_to:
                    final_player_ids_to_remove.append(child_player_id)
                    continue

        # Forward command to the appropriate player after all (base) sanity checks
        # GROUP players (sync_group, universal_group) manage their own members internally
        # and don't need protocol translation - call their set_members directly
        if (
            parent_player.type == PlayerType.GROUP
            and PlayerFeature.SET_MEMBERS in parent_player.state.supported_features
            and (group_protocol := parent_player.own_protocol)
        ):
            await group_protocol.set_members(
                player_ids_to_add=final_player_ids_to_add,
                player_ids_to_remove=final_player_ids_to_remove,
            )
            return None
        # For regular players, handle protocol selection and translation
        takeover = await self._handle_set_members_with_protocols(
            parent_player,
            final_player_ids_to_add,
            final_player_ids_to_remove,
            new_content=new_content,
        )

        if should_stop:
            try:
                await self._stop_player_or_its_queue(parent_player)
            except BaseException:
                if takeover is not None:
                    takeover_owner, takeover_token = takeover
                    await takeover_owner.on_group_content_takeover_aborted(takeover_token)
                raise
        return takeover

    async def _handle_set_members_with_protocols(
        self,
        parent_player: Player,
        player_ids_to_add: list[str],
        player_ids_to_remove: list[str],
        *,
        new_content: bool = False,
    ) -> tuple[PlayerProtocol, object] | None:
        """
        Handle set_members considering protocol and native members.

        Skips permission checks, locking, and all redirect logic (internal use only).
        Translates player IDs to the IDs of the protocols that carry the group,
        and forwards to the set_members of the right protocol of the parent.

        :param parent_player: The parent player to add/remove members to/from.
        :param player_ids_to_add: List of player IDs to add as members.
        :param player_ids_to_remove: List of player IDs to remove from members.
        :param new_content: Set when the caller is about to publish new content, so the
            protocol owner can clear its stale metadata before the member joins.
        :return: The opened takeover transaction as a ``(owner, token)`` pair, or None
            when there is nothing to finish. On success the caller owns the returned
            transaction and must call ``on_group_content_takeover_finished`` or
            ``on_group_content_takeover_aborted`` on it; a raised call is already aborted.
        """
        # Get parent's active protocol and its domain if available
        parent_protocol_domain = None
        parent_protocol_player: PlayerProtocol | None = None
        if (
            parent_player.active_output_protocol
            and parent_player.active_output_protocol != "native"
        ):
            parent_protocol_player = parent_player.protocols.get(
                parent_player.active_output_protocol
            )
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

        # Forward protocol members to the protocol's set_members
        takeover_owner: PlayerProtocol | None = None
        takeover_token: object | None = None
        if protocol_members_to_add and parent_protocol_player and new_content:
            takeover_owner = parent_protocol_player
            takeover_token = await takeover_owner.on_group_content_takeover()
        try:
            if (protocol_members_to_add or protocol_members_to_remove) and parent_protocol_player:
                await self._forward_protocol_set_members(
                    parent_player,
                    parent_protocol_player,
                    protocol_members_to_add,
                    protocol_members_to_remove,
                )

            # Forward native members to the set_members of the parent's own protocol
            parent_own = parent_player.own_protocol
            if (native_members_to_add or native_members_to_remove) and parent_own is not None:
                filtered_native_add = self._filter_native_members(
                    native_members_to_add, parent_player
                )
                # For removal, allow the audio protocol of a member only if it is actually in
                # the group_members of the parent's own protocol. This handles a native
                # protocol of an audio domain (e.g., native AirPlay) where group_members
                # contains the audio protocols of other players
                filtered_native_remove = [
                    pid
                    for pid in native_members_to_remove
                    if (member := self.get_player_by_protocol(pid))
                    and (
                        pid in parent_own.group_members
                        or pid == self._native_member_id(parent_player, member)
                    )
                ]
                self.logger.debug(
                    "Native grouping on %s: filtered_add=%s, filtered_remove=%s",
                    parent_player.state.name,
                    filtered_native_add,
                    filtered_native_remove,
                )
                if filtered_native_add or filtered_native_remove:
                    if PlayerFeature.SET_MEMBERS in parent_own.supported_features:
                        self.logger.info(
                            "Calling set_members on native player %s with add=%s, remove=%s",
                            parent_player.state.name,
                            filtered_native_add,
                            filtered_native_remove,
                        )
                        await parent_own.set_members(
                            player_ids_to_add=filtered_native_add or None,
                            player_ids_to_remove=filtered_native_remove or None,
                        )
        except BaseException:
            if takeover_token is not None and takeover_owner:
                await takeover_owner.on_group_content_takeover_aborted(takeover_token)
            raise
        if takeover_owner is not None and takeover_token is not None:
            return takeover_owner, takeover_token
        return None

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
            leader.display_name,
            new_leader_id,
            len(remaining_members),
        )
        # Move the queue to the new leader. transfer_queue frees the new leader from
        # the old leader's group and stops the old leader; the playback position
        # survives because stop() stores it in resume_pos.
        # The user's permission was checked on the player they acted on; the new leader
        # is the server's choice, so these steps run as the server.
        with system_auth_context():
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
            availability. Must contain at least one audio-capable member.
        """
        # non-audio members (display/visualizer/lighting) stay in the group as followers
        # but can never inherit the queue
        candidates = [
            m
            for m in remaining_members
            if (member := self.get_player(m)) and member.state.type in PLAYBACK_TARGET_TYPES
        ]
        active_domain: str | None = None
        if leader.active_output_protocol and leader.active_output_protocol != "native":
            if protocol_player := leader.protocols.get(leader.active_output_protocol):
                active_domain = protocol_player.provider.domain
        if active_domain:
            for member_id in candidates:
                member = self.get_player(member_id)
                if member is None:
                    continue
                if active_domain in member.playback_domains:
                    return member_id
        return candidates[0]

    async def _stop_player_or_its_queue(self, player: Player) -> None:
        """
        Stop the player, ending its queue when it is playing one of its own.

        :param player: The player to stop.
        """
        # End the queue itself, exactly as a stop command does: stopping only the device
        # leaves the queue session open, so its preloading keeps pulling audio and a
        # provider streaming a live session (Spotify) stays tethered for another track or
        # two. Restricted to the player's own queue: get_active_queue resolves a protocol
        # player to its parent, and stopping that parent's queue would come straight back
        # here. The permission-free handler, because each caller either acts on behalf of
        # the server or has already checked that the user can address the player.
        if (
            active_queue := self.get_active_queue(player)
        ) and active_queue.queue_id == player.player_id:
            await self.mass.player_queues._handle_stop(player.player_id)
            return
        await self._handle_cmd_stop(player.player_id)

    def _clear_sleep_timer(self, player: Player) -> None:
        """
        Clear the active sleep timer for the player.

        :param player: Player to clear the timer for.
        """
        self.mass.cancel_timer(self._sleep_timer_task_id(player.player_id))
        self.mass.player_queues.drop_sleep_timer_target(player.player_id)
        self.reset_sleep_timer(player)

    async def _handle_sleep_timer_expired(self, player_id: str) -> None:
        """
        Stop playback when a player's sleep timer expires.

        :param player_id: Player ID whose sleep timer expired.
        """
        player = self.get_player(player_id)
        if player is None or player.sleep_timer_expires_at is None:
            return
        self._clear_sleep_timer(player)
        if player.available_for_playback:
            await self.cmd_stop(player_id)

    def _signal_sleep_timer_updated(self, player: Player, expires_at: float | None) -> None:
        """
        Signal a sleep timer change for the player on the event bus.

        :param player: Player whose sleep timer changed.
        :param expires_at: New expiry timestamp, or None when the timer was cleared or waits
            for paused playback (see the player's sleep_timer_mode).
        """
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
        await self._power_on_for_playback(player)
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
            await self._get_transport_protocol(player).play()
            return
        if active_source and not active_source.passive:
            await self.select_source(player_id, active_source.id)
            return
        if media:
            # try to re-play the current media item
            await self._handle_play_media(player.player_id, media)
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
        # power on is the one command a player takes while it is unavailable
        player = self.get_player(player_id, raise_unavailable=not powered)
        if player is None:
            msg = f"Player {player_id} is not available"
            raise PlayerUnavailableError(msg)
        player_state = player.state

        # a power on that still waits for the device is waited for (power on) or called off
        # (power off) below, whatever its control reports by now
        if player_state.powered == powered and ATTR_POWER_ON_IN_PROGRESS not in player.extra_data:
            self.logger.debug(
                "Ignoring power %s command for player %s: already in state %s",
                "ON" if powered else "OFF",
                player_state.name,
                "ON" if player_state.powered else "OFF",
            )
            return  # nothing to do

        if not powered:
            # the power off the player reports back must not ungroup it a second time: a
            # device that powers itself off when stopped reports it before the power
            # command below even reaches it
            player.extra_data[ATTR_POWER_OFF_IN_PROGRESS] = time.monotonic()

        # ungroup player at power off
        player_was_sync_child = bool(player.state.synced_to or player.state.active_group)
        if (
            (player_was_sync_child or player_state.group_members)
            and player.type in UNGROUP_ON_POWER_OFF_TYPES
            and not powered
        ):
            # ungroup player if it is synced (or is a sync leader itself). Only this
            # player leaves: its followers keep their own power state, as leadership
            # transfers to one of them or their group re-forms without it
            await self.cmd_ungroup(player_id)

        # always stop player at power off
        if (
            not powered
            and not player_was_sync_child
            and player_state.playback_state in (PlaybackState.PLAYING, PlaybackState.PAUSED)
        ):
            # wait for the stop command to process and prevent race conditions
            async with self.wait_for_player_update(player_id, timeout=5):
                await self._stop_player_or_its_queue(player)

        # handle actual power command
        if player_state.power_control == PLAYER_CONTROL_NONE:
            self.logger.debug(
                "Player %s does not support power control, ignoring power command",
                player_state.name,
            )
            return
        if player_state.power_control == PLAYER_CONTROL_NATIVE:
            # player supports power command natively: forward to player provider
            if own := player.own_protocol:
                await own.power(powered)
                if powered:
                    await wait_for_power_on(self.logger, player)
        elif player_state.power_control == PLAYER_CONTROL_FAKE:
            # user wants to use fake power control - so we (optimistically) update the state
            # and store the state in the cache
            player.extra_data[ATTR_FAKE_POWER] = powered
            # Group players need to actively form/dissolve their session when the
            # user toggles fake power — otherwise the toggle would only update the
            # cosmetic state without ever capturing or releasing the members.
            if player_state.type == PlayerType.GROUP and (own := player.own_protocol):
                await own.power(powered)
            player.update_state()  # trigger update of the player state
            if player_state.type != PlayerType.GROUP:
                # see _publish_player(): group fake-power is intentionally not persisted
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
            if powered and player.offline_by_power_control:
                if ATTR_POWER_ON_IN_PROGRESS in player.extra_data:
                    # joins a power on under way: the request that started it runs auto play
                    skip_auto_play = True
                player = await self._power_on_switched_off_player(player_id)
            elif powered:
                assert player_control.power_on is not None  # for type checking
                await player_control.power_on()
                await wait_for_power_on(self.logger, player, player_control)
            else:
                # calls off a power on that still waits for the device
                self._cancel_power_on(player)
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

    async def _power_on_for_playback(self, player: Player, skip_auto_play: bool = False) -> None:
        """
        Power on a player that is about to start playback, if it is not on yet.

        :param player: The player that is about to play.
        :param skip_auto_play: Skip the auto play on power on, as the caller starts playback.
        """
        # a device reports its playback a moment after it started, so the idle power off of
        # the player and the members playing along counts from here, or it could switch off
        # playback that was just started
        for member_id in {player.player_id, *player.state.group_members}:
            if member := self.get_player(member_id):
                member.extra_data.pop(ATTR_IDLE_SINCE, None)
        if not player.state.powered and player.state.power_control != PLAYER_CONTROL_NONE:
            await self._handle_cmd_power(player.player_id, True, skip_auto_play=skip_auto_play)

    async def _power_on_switched_off_player(self, player_id: str) -> Player:
        """
        Power on a player that is switched off at its external power control.

        Returns right away for a player that is not switched off. Otherwise it returns once
        the device is online and the protocols that brought it back finished attaching,
        while the player stays available to clients in the meantime. Takes no lock: waiting
        for the device takes far longer than other commands wait for a player lock, so it
        must run before any lock is taken.

        :param player_id: The player to power on.
        :raises InsufficientPermissions: When the current user has no access to the player.
        :raises PlayerUnavailableError: When the device does not come online in time, or
            it is switched off again before it does.
        :return: The player.
        """
        player = self.get_player(player_id, True)
        assert player is not None  # for type checking
        if not player.offline_by_power_control:
            return player
        current_user = get_current_user()
        if current_user and not has_player_access(current_user, player.player_id, player):
            msg = f"{current_user.username} does not have access to player {player.display_name}"
            raise InsufficientPermissions(msg)
        control_id = player.power_control
        player_control = self._controls[control_id]
        # a power on that is already under way is only waited for: switching the control
        # once more could switch a device behind a toggling control off again
        owns_power_on = ATTR_POWER_ON_IN_PROGRESS not in player.extra_data
        if owns_power_on:
            player.extra_data[ATTR_POWER_ON_IN_PROGRESS] = True
        try:
            if owns_power_on:
                assert player_control.power_on is not None  # for type checking
                await player_control.power_on()
            self.logger.debug("Waiting for player %s to come online", player.display_name)
            # a control reports its new state a moment after it was switched, so only an
            # off that follows its on means it was switched off again
            control_reported_on = False
            try:
                async with asyncio.timeout(OFFLINE_POWER_ON_TIMEOUT):
                    while True:
                        # a protocol that is still attaching may not be played to yet
                        if player.available_for_playback and self._attaching.keys().isdisjoint(
                            player.protocols
                        ):
                            # a device can come online before its control reports on, while
                            # what follows (e.g. an announcement) reads the power state
                            if (control := self._controls.get(control_id)) is not None:
                                await wait_for_power_on(self.logger, player, control)
                            return player
                        if ATTR_POWER_ON_IN_PROGRESS not in player.extra_data or (
                            self._players.get(player_id) is not player
                        ):
                            # a power off called it off, or the power on waited for has ended
                            msg = f"Player {player.display_name} is switched off"
                            raise PlayerUnavailableError(msg)
                        if (control := self._controls.get(control_id)) is not None:
                            if control.power_state:
                                control_reported_on = True
                            elif control_reported_on:
                                msg = f"Player {player.display_name} is switched off"
                                raise PlayerUnavailableError(msg)
                        await asyncio.sleep(0.5)
            except TimeoutError:
                msg = f"Player {player.display_name} did not come online after powering it on"
                raise PlayerUnavailableError(msg) from None
        finally:
            if owns_power_on:
                player.extra_data.pop(ATTR_POWER_ON_IN_PROGRESS, None)
                if refreshed := self.get_player(player_id):
                    refreshed.refresh_state()

    def _cancel_power_on(self, player: Player) -> None:
        """
        Call off a power on of the player that still waits for its device to come online.

        The requests waiting for it fail as switched off. Its power control is left as it is.

        :param player: The player whose power on to call off.
        """
        if player.extra_data.pop(ATTR_POWER_ON_IN_PROGRESS, None) is not None:
            self.logger.debug("Called off the power on of player %s", player.display_name)

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
        player = self._get_reachable_player(player_id)

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
        # from now on the player shows the level its volume control reports
        player.release_held_volume(PlayerFeature.VOLUME_SET)

        await self._notify_source_volume_change(player, volume_level)

        # Handle native volume control support
        if player.volume_control == PLAYER_CONTROL_NATIVE and (own := player.own_protocol):
            # player supports volume command natively: forward to player
            await own.volume_set(device_volume)
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
        if protocol_player := player.protocols.get(player.state.volume_control):
            # forward the already-scaled device volume: the limits configured on the
            # player are the only ones that apply to the command
            self.logger.debug(
                "Redirecting volume command to protocol %s",
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

        :param player: The player to check.
        """
        return bool(player.extra_data.get(ATTR_MUTE_LOCK)) and self._is_in_group(player.state)

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
        # from now on the player shows the mute its mute control reports
        player.release_held_volume(PlayerFeature.VOLUME_MUTE)
        if mute_control == PLAYER_CONTROL_NATIVE and (own := player.own_protocol):
            # player supports mute command natively: forward to player
            await own.volume_mute(muted)
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

        # handle a protocol as volume_mute control
        if protocol_player := player.protocols.get(mute_control):
            self.logger.debug(
                "Redirecting mute command to protocol %s",
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
        player = self._get_reachable_player(player_id)
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
        # A group left on a protocol that none of its members need anymore is
        # regrouped first, as it would otherwise keep that grouped protocol selected.
        target_player: PlayerProtocol | None = None
        output_protocol: OutputProtocol | None = None
        is_fresh_start = player.state.playback_state not in (
            PlaybackState.PLAYING,
            PlaybackState.PAUSED,
        )
        if (
            not is_fresh_start
            and player.active_output_protocol
            and player.active_output_protocol != "native"
            and (protocol_player := player.protocols.get(player.active_output_protocol))
        ):
            # Use the already-set protocol directly
            output_protocol = player.get_linked_protocol(player.active_output_protocol)
            if output_protocol is not None:
                target_player = protocol_player
        if target_player is None:
            if is_fresh_start:
                await self._regroup_off_unneeded_protocol(player)
            target_player, output_protocol = self._select_best_output_protocol(player)

        own = player.own_protocol
        if target_player is not own:
            # Playing via an audio protocol - update active output protocol
            # output_protocol is guaranteed to be non-None when the target is not the own protocol
            assert output_protocol is not None
            self.logger.debug(
                "Starting playback on %s via protocol %s (target=%s), group_members=%s",
                player.state.name,
                output_protocol.name,
                target_player.protocol_id,
                target_player.group_members,
            )
            await self._activate_output_protocol(player, output_protocol.output_protocol_id)
        elif player.type != PlayerType.GROUP:
            # Native playback - group players don't have output protocols of their own
            # (they delegate to a sync leader / member which manages its own protocol)
            self.logger.debug(
                "Starting playback on %s via native, group_members=%s",
                player.state.name,
                player.state.group_members,
            )
            await self._activate_output_protocol(player, "native")

        async with own.prepare_play_media() if own else contextlib.nullcontext():
            # skip auto-play since we're about to start playback
            await self._power_on_for_playback(player, skip_auto_play=True)
            await target_player.play_media(media)
            if own is not None and target_player is not own:
                # notify the native protocol that protocol playback started
                assert output_protocol is not None
                await own.on_protocol_playback(output_protocol=output_protocol)

    async def _activate_output_protocol(self, player: Player, output_protocol_id: str) -> None:
        """
        Make an output the active output of a player, starting at the level of the player.

        :param player: The player whose output is (re)selected.
        :param output_protocol_id: Id of the output that takes the audio, "native" for
            the own protocol of the player.
        """
        volume_control = player.volume_control_for_output(output_protocol_id)
        mute_control = player.mute_control_for_output(output_protocol_id)
        # a control that takes over, or any while the player keeps the level or mute it had
        await self._hand_over_volume(
            player,
            volume_control
            if player.holds_volume(PlayerFeature.VOLUME_SET)
            or volume_control != player.volume_control
            else None,
            mute_control
            if player.holds_volume(PlayerFeature.VOLUME_MUTE) or mute_control != player.mute_control
            else None,
        )
        player.release_held_volume()
        player.set_active_output_protocol(output_protocol_id)

    async def _hand_over_volume(
        self, player: Player, volume_control: str | None, mute_control: str | None
    ) -> None:
        """
        Give the protocols that take over the volume and mute control of a player its level and mute.

        The level and mute of a player are its own, whichever of its protocols controls
        them: a protocol that takes over starts out with them, before it renders any audio
        of the player. A protocol that does not take them is logged, and what the hand-over
        is part of goes on.

        :param player: The player whose controls move.
        :param volume_control: The volume control that takes over, None when it stays.
        :param mute_control: The mute control that takes over, None when it stays.
        """
        # read both first: what a protocol reports back is published right away
        volume_level, volume_muted = player.state.volume_level, player.state.volume_muted
        if (
            volume_control
            and volume_level is not None
            and (protocol := player.get_control_protocol(volume_control))
        ):
            try:
                await protocol.volume_set(
                    self.scale_volume_to_device(player.player_id, volume_level)
                )
            except Exception as err:
                self.logger.warning(
                    "Could not give %s the volume of %s: %s",
                    protocol.protocol_id,
                    player.display_name,
                    err,
                )
        if (
            mute_control
            and volume_muted is not None
            and (protocol := player.get_control_protocol(mute_control))
        ):
            try:
                await protocol.volume_mute(volume_muted)
            except Exception as err:
                self.logger.warning(
                    "Could not give %s the mute of %s: %s",
                    protocol.protocol_id,
                    player.display_name,
                    err,
                )

    async def _handle_enqueue_next_media(self, player_id: str, media: PlayerMedia) -> None:
        """
        Handle enqueue next media command without group redirect.

        Skips permission checks, locking, and all redirect logic (internal use only).

        :param player_id: player_id of the player to handle the command.
        :param media: The Media that needs to be enqueued on the player.
        """
        player = self._get_reachable_player(player_id)
        if target_player := self._get_control_target(
            player,
            required_feature=PlayerFeature.ENQUEUE,
            require_active=True,
        ):
            self.logger.debug(
                "Redirecting enqueue command to protocol %s",
                target_player.provider.manifest.name,
            )
            await target_player.enqueue_next_media(media)
            return

        own = player.own_protocol
        if own is None or PlayerFeature.ENQUEUE not in own.supported_features:
            raise UnsupportedFeaturedException(
                f"Player {player.state.name} does not support enqueueing"
            )
        await own.enqueue_next_media(media)

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
        # a queue can be named by a legacy id of its player
        if source_id is not None and self.resolve_player_id(source_id) != active_source_id:
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
        # default to MA queue source, which can also be named by a legacy id of its player
        source = player_id if source is None else self.resolve_player_id(source)
        player = self._get_reachable_player(player_id)
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
        # the selected source plays, not the queue that auto play would resume
        await self._power_on_for_playback(player, skip_auto_play=True)
        # forward to player
        if (own := player.own_protocol) is None:
            msg = f"Player {player.state.name} has no protocol that can select a source"
            raise PlayerUnavailableError(msg)
        await own.select_source(source)

    async def _release_group_session(self, group_id: str) -> None:
        """
        Release the session of a group player: power it off, or stop it without power control.

        Skips permission checks (internal use only).

        :param group_id: player_id of the group player.
        :raises PlayerUnavailableError: When the group player is not available.
        """
        group = self.get_player(group_id, raise_unavailable=True)
        assert group is not None  # for type checking
        if group.state.power_control != PLAYER_CONTROL_NONE:
            await self._handle_cmd_power(group.player_id, False)
        else:
            await self._handle_cmd_stop(group.player_id)

    async def _handle_cmd_stop(self, player_id: str) -> None:
        """
        Handle stop command without any redirects.

        Skips permission checks, locking, and all redirect logic (internal use only).

        :param player_id: player_id of the player to handle the command.
        """
        player = self._get_reachable_player(player_id)
        protocol_player = player.foreign_protocol
        if player.active_output_protocol and player.active_output_protocol != "native":
            protocol_player = player.protocols.get(player.active_output_protocol)
        if player.state.playback_state == PlaybackState.IDLE:
            # The player already reports idle but an output protocol is still marked
            # active: the protocol may never have received a stop at all
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
        # Delegate to the active audio protocol if one is active
        target_player = protocol_player or player.own_protocol
        if target_player is None:
            return
        if (
            protocol_player is not None
            and PlayerFeature.POWER in protocol_player.supported_features
        ):
            # if the audio protocol supports/requires power,
            # we power it off instead of just stopping (which also stops playback)
            # this is rare as most protocols do not support power control (except for cast)
            await protocol_player.power(False)
            return

        # handle command on the protocol directly
        await target_player.stop()
        # Only clear active protocol if the audio protocol has no remaining group members.
        # If there are still protocol group members, keep the protocol active so that
        # when playback resumes it continues on the same protocol.
        if protocol_player is None or len(protocol_player.group_members) <= 1:
            self.schedule_active_output_protocol_clear(player)

    async def _handle_cmd_play(self, player_id: str) -> None:
        """
        Handle play command without group redirect.

        Skips permission checks, locking, and all redirect logic (internal use only).

        :param player_id: player_id of the player to handle the command.
        """
        player = self._get_reachable_player(player_id)
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
            # Delegate to the active audio protocol if one is active
            if target_player := player.foreign_protocol or self._get_control_target(
                player, PlayerFeature.PAUSE, require_active=True
            ):
                await target_player.play()
                return
            # No active protocol target: if the protocol rendering the audio supports pause and
            # the active (external) source can be paused, unpause it directly instead of
            # restarting the source.
            output_player = player.resolve_output_protocol()
            if (
                output_player is not None
                and active_source
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
        await self._power_on_for_playback(player)
        if active_source and not active_source.passive:
            await self._handle_select_source(player_id, active_source.id)
            return
        if media:
            # try to re-play the current media item
            await self._handle_play_media(player_id, media)
            return
        # fallback: just send play command - which will fail if nothing can be played
        await self._get_transport_protocol(player).play()

    async def _handle_cmd_pause(self, player_id: str) -> None:
        """
        Handle pause command without any redirects.

        Skips permission checks, locking, and all redirect logic (internal use only).

        :param player_id: player_id of the player to handle the command.
        """
        player = self._get_reachable_player(player_id)
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
        # Delegate to the active audio protocol if one is active
        if target_player := player.foreign_protocol or self._get_control_target(
            player, PlayerFeature.PAUSE, require_active=True
        ):
            await target_player.pause()
            return
        # No active protocol target: if the protocol rendering the audio supports pause and the
        # active (external) source can be paused, forward the command to it instead of stopping
        # it (mirrors the external-source handling in cmd_seek/cmd_next_track).
        output_player = player.resolve_output_protocol()
        if (
            output_player is not None
            and active_source
            and active_source.can_play_pause
            and PlayerFeature.PAUSE in output_player.supported_features
        ):
            await output_player.pause()
            return
        # player/protocol does not support pause: fall back to stop, which also ends the
        # player's own queue so it releases its audio and the music source right away
        self.logger.debug(
            "Player/protocol %s does not support pause, using STOP instead",
            player.state.name,
        )
        await self._stop_player_or_its_queue(player)
