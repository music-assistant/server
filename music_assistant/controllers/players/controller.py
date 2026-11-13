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
from copy import deepcopy
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.auth import Scope
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.constants import (
    PLAYER_CONTROL_NONE,
)
from music_assistant_models.enums import (
    ConfigEntryType,
    EventType,
    IdentifierType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
    ProtocolRole,
    ProviderFeature,
    ProviderType,
)
from music_assistant_models.errors import (
    ActionUnavailable,
    InvalidDataError,
    PlayerCommandFailed,
    PlayerUnavailableError,
    ProviderUnavailableError,
    UnsupportedFeaturedException,
)
from music_assistant_models.player_control import PlayerControl  # noqa: TC002

from music_assistant.constants import (
    ATTR_ACTIVE_SOURCE,
    ATTR_ANNOUNCEMENT_IN_PROGRESS,
    ATTR_AVAILABLE,
    ATTR_ENABLED,
    ATTR_FAKE_POWER,
    ATTR_GROUP_MEMBERS,
    ATTR_GROUP_VOLUME_SNAPSHOT,
    ATTR_MUTE_CONTROL,
    ATTR_POWER_CONTROL,
    ATTR_POWERED,
    ATTR_REACHABLE,
    ATTR_SUPPORTED_FEATURES,
    ATTR_VOLUME_CONTROL,
    CONF_ANNOUNCE_TTS_ENGINE,
    CONF_CACHED_ARP_MAC,
    CONF_CREATED_AT,
    CONF_DEVICE_IDENTIFIERS,
    CONF_LEGACY_IDS,
    CONF_MAX_VOLUME,
    CONF_MIN_VOLUME,
    CONF_PLAYER_DSP,
    CONF_PLAYER_PROTOCOLS,
    CONF_PLAYER_QUEUES,
    CONF_PLAYERS,
    CONF_REPORTED_MAC,
    CONF_SESSION_ONLY,
    CONF_UNLINKED_FROM,
    CONF_VOLUME_STEP,
    VERBOSE_LOG_LEVEL,
)
from music_assistant.controllers.config.constants import AUTOMATIC_VALUE_KEYS
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    get_current_user,
    has_player_access,
    system_auth_context,
)
from music_assistant.helpers.api import api_command
from music_assistant.helpers.colors import get_palette_for_url
from music_assistant.helpers.plugin_engines import create_tts_engine_config_entries
from music_assistant.helpers.util import (
    enrich_device_mac_address,
    is_valid_mac_address,
)
from music_assistant.models.player import Player, PlayerMedia
from music_assistant.models.player_provider import PlayerProvider

from .announcements import AnnouncementsMixin
from .audio_sources import AudioSourceMixin, AudioSourceSession
from .constants import (
    CACHE_CATEGORY_PLAYER_POWER,
    UNGROUP_ON_POWER_OFF_TYPES,
    PlayerLockPurpose,
    SwitchedOffAction,
)
from .grouping import apply_group_change, grouping_snapshot, route_group_change
from .grouping_plan import ChangeKind, GroupChange, elect_ad_hoc_leader, set_members_group
from .helpers import handle_player_command
from .locks import PlayerLocksMixin
from .outputs import OutputsMixin
from .playback import PlaybackMixin
from .power import PowerMixin
from .protocol_linking import ProtocolLinkingMixin
from .sleep_timer import SleepTimerMixin
from .volume import VolumeMixin

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from music_assistant_models.config_entries import (
        CoreConfig,
        PlayerConfig,
    )

    from music_assistant import MusicAssistant
    from music_assistant.helpers.json import SerializableType
    from music_assistant.models.player_protocol import PlayerProtocol

# state keys that carry the current_media playback-position anchor; these only
# change on discrete position events (play/pause/seek/track change/buffer correction)
POSITION_ANCHOR_KEYS = frozenset(
    {
        "current_media.elapsed_time",
        "current_media.elapsed_time_last_updated",
    }
)

# How long an MA power off command claims the power off the player reports back. Covers
# the stop the command waits on and a power control that reports the new state some
# time later.
POWER_OFF_IN_PROGRESS_EXPIRY = 30.0

# How long after a player provider was loaded (the builtin ones at every start) it gets to find
# its devices again before players/remove_unavailable may remove the players it did not find.
REMOVE_UNAVAILABLE_GRACE = 60.0

# How long a triggered player update waits, so the triggers of one change (a related player,
# a config change) lead to a single state calculation.
PLAYER_UPDATE_DEBOUNCE = 0.25

# How long the players wait to recalculate what they can group with after a protocol attached
# or left, or a grouping changed: these come in bursts (a provider that finds its devices, a
# group that forms member by member), which are then recalculated once.
GROUPING_UPDATE_DELAY = 2.0

# How long the queue of a player waits to follow the player's state, so it follows a burst of
# updates (and the poll ticks while the player plays) once.
QUEUE_UPDATE_DEBOUNCE = 0.5

# How long a new active source of a player that plays over a grouped output protocol has to
# stay before it counts as an external source taking the player over: a device can report a
# passing source while a stream starts or ends.
EXTERNAL_SOURCE_TAKEOVER_DELAY = 5

# How long a queue that is restarted for a changed setting waits after its stop before it
# resumes, so the stop has reached the device before the new stream starts.
CONFIG_CHANGE_RESUME_DELAY = 1

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


class PlayerController(
    AnnouncementsMixin,
    AudioSourceMixin,
    ProtocolLinkingMixin,
    PlayerLocksMixin,
    SleepTimerMixin,
    VolumeMixin,
    PowerMixin,
    OutputsMixin,
    PlaybackMixin,
):
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
        # the task that holds each player lock, by lock key, named when a wait times out
        self._lock_holders: weakref.WeakValueDictionary[str, asyncio.Task[Any]] = (
            weakref.WeakValueDictionary()
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
        # refused, passed on to the group player that holds the target, or the target is
        # released from the player it is synced to first
        if await route_group_change(
            self,
            GroupChange(
                kind=ChangeKind.ROUTE,
                target_id=parent_player.player_id,
                add=_member_ids(player_ids_to_add),
                remove=_member_ids(player_ids_to_remove),
            ),
        ):
            return

        # Use lock for playback commands to prevent protocol switches from
        # racing with concurrent play_media / play_index / resume calls.
        # A target whose group leaves member changes to it (a static group) is changed under
        # that group's lock too: moving its output to another protocol resumes the group.
        async with self.get_group_and_player_lock(parent_player.player_id):
            await self._handle_set_members(parent_player, player_ids_to_add, player_ids_to_remove)

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
        group_id = set_members_group(grouping_snapshot(self), target_player) if target else None
        if player_ids_to_add:
            # a group player the command is passed on to only gains players the user may use
            for player_id in (target_player, *player_ids_to_add):
                self.check_player_access(player_id)
        elif not player_ids_to_remove or (target and target.state.synced_to and not group_id):
            # cmd_set_members detaches a synced target first, unless it passes the
            # command on to the target's group player
            self.check_player_access(target_player)
        if not player_ids_to_remove:
            return
        owners = [target_player, group_id] if group_id else [target_player]
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
        await apply_group_change(self, GroupChange(kind=ChangeKind.UNGROUP, target_id=player_id))

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
        self._schedule_update_all_players()

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
        player_id = self.get_player_id_of_protocol(protocol_id)
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
                # a protocol it ranked above stands for the device again
                self._schedule_protocol_roles(player_id)
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

    @api_command("players/merge", required_scope=Scope.CONFIG_PLAYERS_WRITE)
    async def merge(self, player_id: str, target_player: str) -> None:
        """
        Merge a player into another one: the two turn out to be one device.

        The player goes and its outputs move to the target player, which answers to its
        id from then on. A name or setting only the player that goes has is kept, its
        queue is dropped.

        :param player_id: ID of the player that goes.
        :param target_player: ID of the player it is merged into, which stays.
        """
        await self.merge_players(target_player, player_id)

    async def merge_players(self, keep_player_id: str, absorb_player_id: str) -> None:
        """
        Merge a player into another one: the two turn out to be one device.

        The absorbed player goes and its protocols move to the kept player. Its id, and
        the ids it answered to, become legacy ids of the kept player, so what still names
        it reaches the kept player. A name or setting only the absorbed player has is
        carried over; where both have one, that of the kept player stays. The queue of
        the absorbed player is dropped, and stopped first when it plays.

        :param keep_player_id: Id of the player that stays, or one of its legacy ids.
        :param absorb_player_id: Id of the player that goes, or one of its legacy ids.
        :raises InvalidDataError: When a player does not exist, or both ids name one player.
        :raises InvalidCommand: When the two can not be one player: both have a protocol
            that stands for the device, or a protocol of the same domain, their identifiers
            tell them apart, or one of them is a group or only exists while connected.
        """
        keep_id = self.resolve_player_id(keep_player_id)
        absorb_id = self.resolve_player_id(absorb_player_id)
        self._check_merge(keep_id, absorb_id)
        if absorbed := self._players.get(absorb_id):
            await self._stop_player_that_is_merged(absorbed)
        async with self._register_lock:
            # either player may have changed while the playback stopped
            self._check_merge(keep_id, absorb_id)
            await self._merge_players(keep_id, absorb_id)

    @api_command("players/unlink_protocol", required_scope=Scope.CONFIG_PLAYERS_WRITE)
    async def unlink_protocol(self, protocol_id: str) -> str:
        """
        Take a protocol off its player and give it a player of its own.

        For a protocol that ended up on the player of another device. It stays on its new
        player, also after a restart, and is not merged back by itself; the protocols that
        ride on it move with it. Returns the id of the new player.

        :param protocol_id: ID of the protocol.
        :raises InvalidDataError: When no protocol has the id.
        :raises InvalidCommand: When the protocol can not stand on its own: it rides on another
            protocol, it is the protocol of a group, it only exists while connected, or it is
            the only protocol of its player.
        """
        async with self._register_lock:
            player_id = self._check_unlink(protocol_id)
            return await self._unlink_protocol(protocol_id, player_id)

    def schedule_protocol_roles_of_provider(self, provider_id: str) -> None:
        """
        Attach the protocols again whose role changed now that a provider is enabled or disabled.

        A protocol of a provider the user disabled ranks above no other protocol of its player,
        one of a provider the user enabled again does; that holds whether the provider is
        loaded or not.

        :param provider_id: Instance id of the provider.
        """
        if self.mass.closing:
            return
        for protocol_id in self.mass.config.get_protocol_ids_of_provider(provider_id):
            record = self.mass.config.get_protocol_record(protocol_id) or {}
            if (player_id := record.get("player_id")) in self._players:
                self._schedule_protocol_roles(str(player_id))

    def wants_protocol(self, protocol_id: str, protocol: PlayerProtocol | None = None) -> bool:
        """
        Return whether the server wants a protocol attached now.

        Not when the user disabled the player it belongs to, or switched the protocol
        itself off as an output of that player. A provider that sets up a connection
        for a device before it attaches it can ask this first.

        :param protocol_id: Id of the protocol.
        :param protocol: The protocol object about to be attached, when the provider has one:
            what it is decides whether its stored config applies to it (a protocol that only
            exists while it is connected is no output of the player of a device).
        """
        return self._wants_protocol(
            protocol_id,
            protocol or self._protocols.get(protocol_id) or self._unattached.get(protocol_id),
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
        self,
        player_id: str,
        force_update: bool = False,
        debounce_delay: float = PLAYER_UPDATE_DEBOUNCE,
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
            QUEUE_UPDATE_DEBOUNCE,
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
                EXTERNAL_SOURCE_TAKEOVER_DELAY,
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
            player_id = self.get_player_id_of_protocol(protocol_id)
            if not self.wants_protocol(protocol_id):
                # the user switched this output of the player off
                await self.detach(protocol_id)
                # a protocol that waits for the place of its domain may take it now
                self._schedule_held_protocols()
                if player_id:
                    # and one it ranked above stands for the device again
                    self._schedule_protocol_roles(player_id)
                return
            if player_id:
                # a protocol switched on again ranks above others again
                self._schedule_protocol_roles(player_id)
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

    def __iter__(self) -> Iterator[Player]:
        """Iterate over all players."""
        return iter(self._players.values())

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
        else:
            # a protocol it ranks above stands for the device no more
            await self._attach_in_changed_roles(player)
            if self._attach_aborted(protocol, attach):
                return
        self._unattached.pop(protocol_id, None)
        self._release_held_protocol(protocol)
        # The protocol is bound before it is fully ready (config not loaded), so it can
        # be found during the rest of the attach, such as when fetching its config.
        # A player that is new stays unpublished until the end.
        self._protocols[protocol_id] = protocol
        role = self._attach_role(protocol, player_id)
        player.add_protocol(protocol, role)
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
            role.value,
            player_id,
            player.display_name,
        )
        self._schedule_held_protocols()
        self._schedule_duplicate_merge(player_id)

    def _schedule_protocol_roles(self, player_id: str) -> None:
        """
        Attach the protocols of a player again whose role changed with its other protocols.

        Runs once the attach lock is free.

        :param player_id: Id of the player.
        """
        self.mass.create_task(
            self._settle_protocol_roles(player_id), task_id=f"protocol_roles_{player_id}"
        )

    async def _settle_protocol_roles(self, player_id: str) -> None:
        """
        Attach the protocols of a player again whose role changed with its other protocols.

        :param player_id: Id of the player.
        """
        async with self._register_lock:
            if player := self._players.get(player_id):
                await self._attach_in_changed_roles(player)

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
        if self._attach_role(protocol, player.player_id) != player.get_role(protocol_id):
            # a protocol in another role leaves its player and attaches again: to the
            # same player, unless that has no place for it in its new role
            await self._run_attach(protocol)
            if protocol.player is not player:
                await self._settle_player(player)
            return
        self._store_protocol(protocol, player.player_id)
        player.refresh_state()
        self._schedule_held_protocols()
        self._schedule_duplicate_merge(player.player_id)

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
                # a protocol the dropped one ranked above stands for the device again
                self._schedule_protocol_roles(player.player_id)
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
        player_id = self.get_player_id_of_protocol(protocol_id)
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
        :param permanent: The player is removed for good: also delete its saved queue, and
            say so in the event (a player dropped for now comes back with the same id).
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
        # once the player is gone its id can be a legacy id of another player, whose queue
        # this update must not reach
        self.mass.cancel_timer(f"queue_on_player_update_{player_id}")
        self.mass.player_queues.on_player_remove(player_id, permanent=permanent)
        if player.initialized.is_set() and not self.mass.closing:
            self.mass.signal_event(
                EventType.PLAYER_REMOVED, player_id, data={"permanent": permanent}
            )

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

    async def _stop_player_that_is_merged(self, player: Player) -> None:
        """
        Stop the queue a player plays before it is merged into another one, best effort.

        Its queue goes with it. A member of a group or of a sync leader is not stopped: its
        membership moves with its protocols.

        :param player: The player that is merged into another one.
        """
        player_id = player.player_id
        if (
            self._resolve_playback_owner(player) is not player
            or player.state.active_source != player_id
            or player.state.playback_state == PlaybackState.IDLE
        ):
            return
        try:
            await self.mass.player_queues.stop(player_id)
        except Exception:
            self.logger.exception("Error stopping %s before it is merged", player.display_name)

    async def _merge_players(self, keep_id: str, absorb_id: str) -> None:
        """
        Merge a player into another one, right after the attach rules allowed it.

        Called under the attach lock. What is stored changes before the first await and is
        saved before anything else of the absorbed player is deleted; the user filters follow
        once its removal went out, so a user limited to it still learns that it went. A
        protocol that fails to attach again, or that its provider detached meanwhile,
        attaches to the kept player when its provider finds it again.

        :param keep_id: Id of the player that stays.
        :param absorb_id: Id of the player that goes.
        """
        protocol_ids = set(self.mass.config.get_protocol_ids_of_player(absorb_id))
        if absorbed := self._players.get(absorb_id):
            protocol_ids.update(absorbed.protocol_ids)
        self.logger.info(
            "Merging player %s into %s",
            self._stored_player_name(absorb_id),
            self._stored_player_name(keep_id),
        )
        self._merge_player_config(keep_id, absorb_id, protocol_ids)
        await self.mass.config.async_save()
        absorbed = self._players.get(absorb_id)
        moving = list(absorbed.protocols.values()) if absorbed is not None else []
        # the output that plays for the absorbed player, e.g. in a group, stays the active output
        # of the kept one, so the group keeps its member
        playing_through = (
            absorbed.resolve_output_protocol()
            if absorbed is not None and absorbed.state.playback_state != PlaybackState.IDLE
            else None
        )
        # the providers of the protocols that are not attached look for their devices again
        provider_ids = {
            str(record.get("provider"))
            for protocol_id in protocol_ids - {protocol.protocol_id for protocol in moving}
            if (record := self.mass.config.get_protocol_record(protocol_id))
        }
        # a moving protocol is attaching: a detach by its provider meanwhile takes this away
        moves = {protocol.protocol_id: _Attach() for protocol in moving}
        self._attaching.update(moves)
        for protocol in moving:
            if protocol.player is absorbed:
                await self._unbind(protocol)
        if absorbed is not None:
            await self._drop_player(absorbed, permanent=True)
        else:
            self.mass.player_queues.purge_saved_queue(absorb_id)
        try:
            await self.mass.webserver.auth.replace_player_in_user_filters(
                absorb_id, keep_id, removed_player_ids=[absorb_id]
            )
        except Exception:
            self.logger.exception("Could not move the user filters of %s", absorb_id)
        for protocol in moving:
            if self._attaching.get(protocol.protocol_id) is not moves[protocol.protocol_id]:
                # its provider detached it while it moved, and keeps the object
                if not protocol.provider.unloading:
                    self._unattached[protocol.protocol_id] = protocol
                continue
            try:
                await self._run_attach(protocol)
            except Exception:
                self.logger.exception(
                    "Protocol %s did not attach to %s after the merge",
                    protocol.protocol_id,
                    self._stored_player_name(keep_id),
                )
        if keeper := self._players.get(keep_id):
            # a protocol that moved here while away may rank above one that is attached
            await self._attach_in_changed_roles(keeper)
        if (keeper := self._players.get(keep_id)) and keeper.initialized.is_set():
            # the player caches its legacy ids with its config
            keeper.set_config(await self.mass.config.get_player_object_config(keeper))
            keeper.update_state()
            if (
                playing_through is not None
                and keeper.protocols.get(playing_through.protocol_id) is playing_through
                and keeper.active_output_protocol is None
            ):
                keeper.set_active_output_protocol(playing_through.protocol_id)
        for provider_id in provider_ids:
            if isinstance(provider := self.mass.get_provider(provider_id), PlayerProvider):
                provider.schedule_discovery()
        self._schedule_held_protocols()
        self._schedule_update_all_players()

    def _merge_player_config(self, keep_id: str, absorb_id: str, protocol_ids: set[str]) -> None:
        """
        Move what is stored about a player to the player it is merged into.

        Its protocols, its group memberships and its ids follow; its name, its values and
        its DSP and queue settings where the kept player has none. Its own config is
        deleted last.

        :param keep_id: Id of the player that stays.
        :param absorb_id: Id of the player that goes.
        :param protocol_ids: Ids of the protocols of the player that goes.
        """
        config = self.mass.config
        keep_key = f"{CONF_PLAYERS}/{keep_id}"
        kept = config.get(keep_key) or {}
        absorbed = config.get(f"{CONF_PLAYERS}/{absorb_id}") or {}
        kept_values = kept.get("values") or {}
        absorbed_values = absorbed.get("values") or {}
        if absorbed.get("name") and not kept.get("name"):
            config.set(f"{keep_key}/name", absorbed["name"])
        for key, value in absorbed_values.items():
            if key not in kept_values and key not in AUTOMATIC_VALUE_KEYS:
                config.set(f"{keep_key}/values/{key}", deepcopy(value))
        stored_identifiers = absorbed_values.get(CONF_DEVICE_IDENTIFIERS)
        if isinstance(stored_identifiers, dict) and stored_identifiers:
            config.set(
                f"{keep_key}/values/{CONF_DEVICE_IDENTIFIERS}",
                {**stored_identifiers, **(kept_values.get(CONF_DEVICE_IDENTIFIERS) or {})},
            )
        legacy_ids = [
            *(kept_values.get(CONF_LEGACY_IDS) or []),
            absorb_id,
            *(absorbed_values.get(CONF_LEGACY_IDS) or []),
        ]
        config.set(
            f"{keep_key}/values/{CONF_LEGACY_IDS}",
            list(
                dict.fromkeys(
                    legacy_id
                    for legacy_id in legacy_ids
                    if isinstance(legacy_id, str) and legacy_id != keep_id
                )
            ),
        )
        merged = {keep_id, absorb_id}
        for protocol_id in protocol_ids | set(config.get_protocol_ids_of_player(keep_id)):
            if (record := config.get_protocol_record(protocol_id)) is None:
                continue
            if record.get("player_id") != keep_id:
                config.set_protocol_value(protocol_id, "player_id", keep_id)
            unlinked_from = (record.get("values") or {}).get(CONF_UNLINKED_FROM)
            if isinstance(unlinked_from, str) and self.resolve_player_id(unlinked_from) in merged:
                # the user puts the protocol back together with the player it was taken off
                config.remove(f"{CONF_PLAYER_PROTOCOLS}/{protocol_id}/values/{CONF_UNLINKED_FROM}")
        if keeper := self._players.get(keep_id):
            keeper.protocol_ids.update(protocol_ids)
        absorbed_dsp = config.get(f"{CONF_PLAYER_DSP}/{absorb_id}")
        if absorbed_dsp and not config.get(f"{CONF_PLAYER_DSP}/{keep_id}"):
            config.set(f"{CONF_PLAYER_DSP}/{keep_id}", deepcopy(absorbed_dsp))
        absorbed_queue = config.get(f"{CONF_PLAYER_QUEUES}/{absorb_id}")
        if isinstance(absorbed_queue, dict) and absorbed_queue.get("values"):
            kept_queue = deepcopy(config.get(f"{CONF_PLAYER_QUEUES}/{keep_id}") or {})
            kept_queue["queue_id"] = keep_id
            kept_queue["values"] = {
                **deepcopy(absorbed_queue["values"]),
                **(kept_queue.get("values") or {}),
            }
            config.set(f"{CONF_PLAYER_QUEUES}/{keep_id}", kept_queue)
        self._update_group_memberships(absorb_id, keep_id)
        for key in (
            f"{CONF_PLAYERS}/{absorb_id}",
            f"{CONF_PLAYER_DSP}/{absorb_id}",
            f"{CONF_PLAYER_QUEUES}/{absorb_id}",
        ):
            config.remove(key)
        self._build_legacy_id_index()

    async def _unlink_protocol(self, protocol_id: str, player_id: str) -> str:
        """
        Move a protocol, with the protocols riding on it, to a new player of its own.

        :param protocol_id: Id of the protocol, which the unlink rules allow to move.
        :param player_id: Id of the player it is taken off.
        """
        config = self.mass.config
        protocol = self._protocols.get(protocol_id)
        record = config.get_protocol_record(protocol_id) or {}
        moved = [protocol_id, *config.get_protocol_ids_derived_from(protocol_id)]
        new_id = self._new_player_id()
        config.create_player_record(
            new_id,
            provider=str(record.get("provider")),
            player_type=protocol.type if protocol else PlayerType.PLAYER,
            default_name=(protocol.name if protocol else None) or record.get("default_name"),
            values={CONF_CREATED_AT: time.time_ns()},
        )
        for moved_id in moved:
            config.set_protocol_value(moved_id, "player_id", new_id)
            self._replace_protocol_references(player_id, moved_id, None)
        config.set_protocol_value(protocol_id, CONF_UNLINKED_FROM, player_id)
        if player := self._players.get(player_id):
            player.protocol_ids.difference_update(moved)
        self.logger.info(
            "Protocol %s taken off player %s", protocol_id, self._stored_player_name(player_id)
        )
        attached = [
            attached_protocol
            for moved_id in moved
            if (attached_protocol := self._protocols.get(moved_id))
            and attached_protocol.player is not None
        ]
        for attached_protocol in attached:
            self._attaching.pop(attached_protocol.protocol_id, None)
            await self._unbind(attached_protocol)
        for attached_protocol in attached:
            try:
                await self._run_attach(attached_protocol)
            except Exception:
                self.logger.exception(
                    "Protocol %s did not attach to its new player", attached_protocol.protocol_id
                )
        if new_id not in self._players:
            # its device is away, or was not attached as its player was disabled: the
            # player waits for it like a stored one at startup, and its provider looks again
            await self._publish_player(self._create_player(new_id))
            if isinstance(
                provider := self.mass.get_provider(str(record.get("provider"))), PlayerProvider
            ):
                provider.schedule_discovery()
        if player is not None:
            # a protocol the moved one ranked above stands for the device again
            await self._attach_in_changed_roles(player)
            await self._settle_player(player)
        self._schedule_update_all_players()
        return new_id

    def _schedule_duplicate_merge(self, player_id: str) -> None:
        """
        Look for unused duplicates of a player, now that one of its protocols attached.

        :param player_id: Id of the player.
        """
        self.mass.create_task(
            self._merge_duplicates(player_id), task_id=f"merge_duplicates_{player_id}"
        )

    async def _merge_duplicates(self, player_id: str) -> None:
        """
        Merge the players that turn out to stand for the device of a player, while unused.

        Of two such players the younger one goes into the older one, and only while nothing
        about it is the user's own yet (see _is_unused); otherwise both stay, for the user to
        merge by hand.

        :param player_id: Id of the player.
        """
        async with self._register_lock:
            while not self.mass.closing and player_id in self._players:
                for other_id in self._duplicates_of(player_id):
                    keep_id, absorb_id = sorted((player_id, other_id), key=self._merge_rank)
                    if (absorbed := self._players.get(absorb_id)) and self._is_unused(absorbed):
                        break
                else:
                    return
                self.logger.info(
                    "Player %s turns out to be the device of %s",
                    self._stored_player_name(absorb_id),
                    self._stored_player_name(keep_id),
                )
                await self._merge_players(keep_id, absorb_id)
                player_id = keep_id

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
                await apply_group_change(
                    self,
                    GroupChange(kind=ChangeKind.LEAVE_FOR_REMOVAL, target_id=player.player_id),
                    player,
                )
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
            self.mass.call_later(
                CONFIG_CHANGE_RESUME_DELAY,
                self.mass.player_queues.resume,
                resume_queue.queue_id,
                False,
            )

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
                    self.trigger_player_update(
                        related.player.player_id, debounce_delay=GROUPING_UPDATE_DELAY
                    )

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
        issued_at = player.power_off_issued_at
        player.set_power_off_issued_at(None)
        if issued_at is not None and time.monotonic() - issued_at < POWER_OFF_IN_PROGRESS_EXPIRY:
            return
        if player.offline_by_power_control:
            # it went offline along with its power, so it was detached above instead
            return
        if player.state.type in UNGROUP_ON_POWER_OFF_TYPES and (
            player.state.synced_to or player.state.active_group or player.state.group_members
        ):
            self.mass.create_task(self.cmd_ungroup(player.player_id))

    async def _cleanup_player_memberships(self, player_id: str) -> None:
        """Ensure a player is detached from any groups or syncgroups."""
        await apply_group_change(self, GroupChange(kind=ChangeKind.LEAVE, target_id=player_id))

    # Protocol linking methods are provided by ProtocolLinkingMixin (protocol_linking.py)

    async def _poll_players(self) -> None:
        """Background task that polls the protocols of the players for updates."""
        while True:
            for player in list(self._players.values()):
                self._check_idle_power_off(player)
                # if the player is playing, update elapsed time every tick
                # to ensure the queue has accurate details
                if player.state.playback_state == PlaybackState.PLAYING:
                    self.mass.call_later(
                        QUEUE_UPDATE_DEBOUNCE,
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
        async with self.get_group_and_player_lock(queue_id):
            queue = self.mass.player_queues.get(queue_id)
            if queue is None or not queue.items:
                self.logger.debug("Not restarting queue %s after DSP change: it is empty", queue_id)
                return
            await self.mass.player_queues.resume(queue_id, False)

    def _schedule_update_all_players(self) -> None:
        """
        Schedule a debounced update of all players' state.

        Used when a new player is registered to ensure all existing players
        update their dynamic properties (like can_group_with) that may have changed.
        """
        if self.mass.closing:
            return

        for player in self.iter_players(return_unavailable=True, return_disabled=False):
            self.trigger_player_update(player.player_id, debounce_delay=GROUPING_UPDATE_DELAY)

    async def _detach_member(self, member: Player) -> None:
        """
        Remove a sync member from the player it is synced to.

        Skips permission checks and locking (internal use only).

        :param member: The sync member to detach.
        """
        await apply_group_change(
            self, GroupChange(kind=ChangeKind.DETACH, target_id=member.player_id), member
        )

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

    async def _handle_set_members(
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
        return await apply_group_change(
            self,
            GroupChange(
                kind=ChangeKind.MEMBERS,
                target_id=parent_player.player_id,
                add=_member_ids(player_ids_to_add),
                remove=_member_ids(player_ids_to_remove),
                new_content=new_content,
            ),
            parent_player,
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
        new_leader_id = elect_ad_hoc_leader(
            grouping_snapshot(self), leader.player_id, remaining_members
        )
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

    async def _release_group_session(self, group_id: str) -> None:
        """
        Release the session of a group player: power it off, or stop it without power control.

        Skips permission checks (internal use only).

        :param group_id: player_id of the group player.
        :raises PlayerUnavailableError: When the group player is not available.
        """
        group = self.get_player(group_id, raise_unavailable=True)
        assert group is not None  # for type checking
        async with self.get_player_lock(group.player_id, PlayerLockPurpose.PLAYBACK):
            if group.state.power_control != PLAYER_CONTROL_NONE:
                await self._handle_cmd_power(group.player_id, False)
            else:
                await self._handle_cmd_stop(group.player_id)


def _member_ids(player_ids: list[str] | None) -> tuple[str, ...] | None:
    """
    Return the players a member change names, keeping whether they were given at all.

    :param player_ids: The player ids a command was given, None when not given.
    """
    return tuple(player_ids) if player_ids is not None else None
