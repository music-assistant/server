"""
Shared base of the Player Controller and its mixins.

The mixins of the controller all work on the registry declared here: the players and protocols the
controller holds, and the state it keeps for them. This base declares that state and the operations
one part of the controller calls on another, so each mixin type-checks on its own.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.auth import Scope
from music_assistant_models.enums import PlaybackState, PlayerType
from music_assistant_models.errors import (
    AlreadyRegisteredError,
    InsufficientPermissions,
    PlayerUnavailableError,
)
from music_assistant_models.player_control import PlayerControl  # noqa: TC002

from music_assistant.constants import (
    CONF_LEGACY_IDS,
    CONF_MUTE_CONTROL,
    CONF_PLAYERS,
    CONF_POWER_CONTROL,
    CONF_VOLUME_CONTROL,
)
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    get_current_user,
    has_player_access,
)
from music_assistant.helpers.api import api_command
from music_assistant.models.core_controller import CoreController
from music_assistant.models.player import Player, PlayerState

if TYPE_CHECKING:
    import asyncio
    import weakref
    from collections.abc import Callable, Iterator
    from contextlib import AbstractAsyncContextManager

    from music_assistant_models.config_entries import PlayerConfig
    from music_assistant_models.enums import PlayerFeature, ProtocolRole, SourceControl
    from music_assistant_models.media_items import AudioSource
    from music_assistant_models.player import OutputProtocol
    from music_assistant_models.player_queue import PlayerQueue

    from music_assistant.models.player import PlayerMedia
    from music_assistant.models.player_protocol import PlayerProtocol
    from music_assistant.models.plugin import PluginProvider, SourceControlValue

    from .audio_sources import AudioSourceSession
    from .constants import PlayerLockPurpose
    from .lifecycle import _Attach
    from .matching import DeviceIdentity
    from .power import MemberJoin


class _PlayerControllerBase(CoreController):
    """
    The registry of the players controller, and the contract between its parts.

    Holds the state the controller sets up and the lookups of its players, protocols and player
    controls, and declares the operations one part of the controller calls on another.
    """

    # the state of the controller, set up in PlayerController.__init__
    _players: dict[str, Player]
    _protocols: dict[str, PlayerProtocol]
    _unattached: weakref.WeakValueDictionary[str, PlayerProtocol]
    _attaching: dict[str, _Attach]
    _removing: set[str]
    _providers_loaded_at: dict[str, float]
    _last_polls: dict[str, float]
    _legacy_ids: dict[str, str]
    _controls: dict[str, PlayerControl]
    _poll_task: asyncio.Task[None] | None
    _player_command_locks: dict[str, asyncio.Lock]
    _task_held_locks: weakref.WeakKeyDictionary[asyncio.Task[Any], set[str]]
    _lock_holders: weakref.WeakValueDictionary[str, asyncio.Task[Any]]
    _register_lock: asyncio.Lock
    _pending_protocol_evaluations: dict[str, asyncio.TimerHandle]
    _source_sessions: dict[str, AudioSourceSession]
    _member_joins: dict[str, MemberJoin]
    _state_update_subscribers: list[Callable[[Player, dict[str, tuple[Any, Any]]], None]]

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
            if not (player.available or return_unavailable):
                continue
            if not (player.enabled or return_disabled):
                continue
            if not player.initialized.is_set():
                continue
            if provider_filter is not None and player.provider_id != provider_filter:
                continue
            yield player

    def iter_protocols(self, provider_filter: str | None = None) -> Iterator[PlayerProtocol]:
        """
        Iterate over the protocol objects that are attached to a player or held.

        A protocol is held until it is known which player it belongs to; its
        ``player`` is None meanwhile.

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
            if (not player.available or not player.enabled) and raise_unavailable:
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
        Return the attached or held protocol object of any player provider by its id.

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
        (``get_player(player_id).resolve_output_protocol()``): a player id that is not
        minted can be the id of one of its protocols, which this returns also while another
        of its protocols renders.

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

    def get_player_or_protocol_player(self, player_id: str) -> Player | None:
        """
        Return the player an id names as a player id first, else the player of a protocol of that id.

        For an id that is a player id but may be the id of a protocol: the members of a group
        protocol, a player id a plugin stored, the members of a player's state. An id that names
        a player, live or stored (a disabled one), never names another player, also when a
        protocol of that player has the same id: for a stored player without a live object this
        returns None. Only an id no player has names the player of a protocol. An id that is only
        ever a player id is looked up with `get_player`, an id a device protocol reports (its group
        members, its sync leader) with `get_player_by_protocol`.

        :param player_id: ID of a player or one of its legacy ids, else of an attached protocol.
        """
        if player := self.get_player(player_id):
            return player
        if self.resolve_player_id(player_id) in self.mass.config.get(CONF_PLAYERS, {}):
            return None
        return self.get_player_by_protocol(player_id)

    def get_player_id_of_protocol(self, protocol_id: str) -> str | None:
        """
        Return the id of the player a protocol belongs to, attached or not.

        That is the player it is attached to, else the player its stored config names;
        None for a protocol that is neither attached nor stored.

        :param protocol_id: Id of the protocol.
        """
        if (protocol := self._protocols.get(protocol_id)) and protocol.player:
            return protocol.player.player_id
        if record := self.mass.config.get_protocol_record(protocol_id):
            return cast("str | None", record.get("player_id"))
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
            if player.name.strip().lower() == name_normalized:
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
            name = player.name if player else player_id
            msg = f"{current_user.username} does not have access to player {name}"
            raise InsufficientPermissions(msg)

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
                player.power_control,
                player.volume_control,
                player.mute_control,
            ) or (
                include_configured and control_id in self._configured_control_ids(player.player_id)
            ):
                self.mass.loop.call_soon(player.refresh_state)

    def remove_player_control(self, control_id: str) -> None:
        """Remove a player_control from the players controller."""
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
        if player.synced_to and player.synced_to != player.player_id:
            if sync_leader := self.get_player(player.synced_to):
                return self.get_active_queue(sync_leader)
        # handle active group player
        if player.active_group and player.active_group != player.player_id:
            if group_player := self.get_player(player.active_group):
                return self.get_active_queue(group_player)
        # active_source may be filled queue id (or None)
        active_source = player.active_source or player.player_id
        if active_queue := self.mass.player_queues.get(active_source):
            return active_queue
        return None

    def iter_group_members(
        self,
        group_player: Player,
        only_powered: bool = False,
        only_playing: bool = False,
        active_only: bool = False,
        exclude_self: bool = True,
    ) -> Iterator[Player]:
        """
        Iterate over the members of a group player or sync leader that play along right now.

        A member that can not be played to at this instant is left out, also one that is
        switched off at its power control and is still powered on for the group.

        :param group_player: The group player or sync leader.
        :param only_powered: Leave out the members that are powered off.
        :param only_playing: Only the members that play or are paused.
        :param active_only: Only the members this group holds.
        :param exclude_self: Leave out the group player or leader itself.
        """
        for child_id in list(group_player.group_members):
            if child_player := self.get_player(child_id, False):
                if not child_player.available_for_playback or not child_player.enabled:
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

    def _configured_control_ids(self, player_id: str) -> set[str]:
        """Return the player control ids the given player's config selects."""
        return {
            str(value)
            for conf_key in (CONF_POWER_CONTROL, CONF_VOLUME_CONTROL, CONF_MUTE_CONTROL)
            if (value := self.mass.config.get_player_value(player_id, conf_key))
        }

    def _command_player_id(self, player_id: str) -> str:
        """
        Return the current id of the player a command names.

        :param player_id: The id of a player, one of its legacy ids, or the id of a protocol
            attached to it.
        """
        # this should not happen, but in case the id of a protocol is used,
        # auto-resolve it to the player it is attached to
        if (
            player_id not in self._players
            and (protocol := self._protocols.get(player_id))
            and (protocol_parent := protocol.player)
        ):
            return protocol_parent.player_id
        return self.resolve_player_id(player_id)

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
            msg = f"Player {player.name} is switched off"
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
                player.name,
                target.name,
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
        if owner.synced_to and (sync_leader := self.get_player(owner.synced_to)):
            owner = sync_leader
        # a captured sync leader hands its playback on to its group, whose lock has
        # to be taken first (see get_group_and_player_lock)
        if owner.active_group and (group := self.get_player(owner.active_group)):
            owner = group
        return owner

    def _playback_owner_id(self, player_id: str) -> str:
        """
        Return the id of the player that owns the playback of the given player.

        :param player_id: The player to resolve the playback owner for; a player that is
            not known owns its own playback.
        """
        if (player := self.get_player(player_id)) is None:
            return player_id
        return self._resolve_playback_owner(player).player_id

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
            if _player.type != PlayerType.GROUP:
                continue
            if player_id in _player.group_members:
                yield _player

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

    if TYPE_CHECKING:
        # locks
        def get_player_lock(
            self,
            player_id: str,
            purpose: PlayerLockPurpose = PlayerLockPurpose.PLAYBACK,
        ) -> AbstractAsyncContextManager[None]: ...
        def get_group_and_player_lock(
            self, player_id: str
        ) -> AbstractAsyncContextManager[None]: ...
        def _playback_owner_locks(
            self, player_id: str, *, hold_player: bool
        ) -> AbstractAsyncContextManager[None]: ...
        # lifecycle
        async def unregister(self, protocol_id: str) -> None: ...
        async def remove(self, player_id: str) -> None: ...
        def wants_protocol(
            self, protocol_id: str, protocol: PlayerProtocol | None = None
        ) -> bool: ...
        async def _run_attach(self, protocol: PlayerProtocol, final: bool = False) -> None: ...
        def _schedule_protocol_roles(self, player_id: str) -> None: ...
        async def _get_protocol_config(self, protocol: PlayerProtocol) -> PlayerConfig: ...
        async def _hold_derived_protocols_without_place(self, protocol_id: str) -> None: ...
        async def _settle_player(self, player: Player) -> None: ...
        async def _unbind(self, protocol: PlayerProtocol) -> Player | None: ...
        def _create_player(
            self, player_id: str, protocol: PlayerProtocol | None = None
        ) -> Player: ...
        async def _publish_player(self, player: Player) -> None: ...
        async def _drop_player(self, player_id: str, player: Player | None) -> None: ...
        async def _restore_players(self) -> None: ...
        async def _disable_player(self, player: Player) -> None: ...
        async def _enable_player(self, player_id: str) -> None: ...
        # finding the player of a protocol
        def _resolve_player_id(self, protocol: PlayerProtocol) -> str | None: ...
        def _can_attach_to(
            self,
            protocol: PlayerProtocol,
            identity: DeviceIdentity,
            player_id: str,
            attached_only: bool = False,
        ) -> bool: ...
        def _attach_role(self, protocol: PlayerProtocol, player_id: str) -> ProtocolRole: ...
        async def _attach_in_changed_roles(self, player: Player) -> None: ...
        def _link_applies(self, protocol: PlayerProtocol | None, player_id: str | None) -> bool: ...
        def _has_other_protocol_of_domain(
            self, protocol: PlayerProtocol, player_id: str, include_away: bool = False
        ) -> bool: ...
        def _get_protocol_ids(self, player_id: str) -> set[str]: ...
        def _identity_of(self, protocol: PlayerProtocol) -> DeviceIdentity: ...
        def _identity_parts(
            self,
            player_id: str,
            exclude_protocol_id: str | None = None,
            attached_only: bool = False,
        ) -> list[DeviceIdentity]: ...
        def _merged_identity(self, player_id: str) -> DeviceIdentity: ...
        def _awaits_identity(self, protocol: PlayerProtocol) -> bool: ...
        def _hold_protocol(self, protocol: PlayerProtocol) -> None: ...
        async def _evaluate_pending_protocol(self, protocol_id: str, final: bool) -> None: ...
        def _schedule_held_protocols(self) -> None: ...
        def _mint_player(self, protocol: PlayerProtocol) -> str: ...
        def _new_player_id(self) -> str: ...
        def _store_protocol(self, protocol: PlayerProtocol, player_id: str) -> None: ...
        def _replace_protocol_references(
            self, player_id: str, protocol_id: str, replacement_id: str | None
        ) -> None: ...
        def _update_group_memberships(
            self, player_id: str, replacement_id: str | None = None
        ) -> None: ...
        # merging and unlinking
        def _schedule_duplicate_merge(self, player_id: str) -> None: ...
        # state updates
        def trigger_player_update(
            self,
            player_id: str,
            force_update: bool = False,
            debounce_delay: float = ...,
        ) -> None: ...
        def wait_for_player_update(
            self,
            player_id: str,
            attribute_name: str | None = None,
            attribute_value: Any = ...,
            timeout: float = 5.0,
        ) -> AbstractAsyncContextManager[None]: ...
        async def _wait_for_playback_state(
            self,
            player: Player,
            wanted_state: PlaybackState,
            timeout: float,
            minimal_time: float = 0,
        ) -> None: ...
        async def _poll_players(self) -> None: ...
        def _schedule_update_all_players(self) -> None: ...
        # config changes
        def _handle_group_dsp_change(
            self, player: Player, prev_group_members: list[str], new_group_members: list[str]
        ) -> None: ...
        # output selection
        def schedule_active_output_protocol_clear(self, player: Player) -> None: ...
        def _get_transport_protocol(self, player: Player) -> PlayerProtocol: ...
        def _check_external_source_takeover(self, player: Player) -> None: ...
        async def _activate_output_protocol(
            self, player: Player, output_protocol_id: str
        ) -> None: ...
        def _select_best_output_protocol(
            self, player: Player
        ) -> tuple[PlayerProtocol, OutputProtocol | None]: ...
        def _get_grouped_output_protocol(self, player: Player) -> PlayerProtocol | None: ...
        def _get_control_target(
            self,
            player: Player,
            required_feature: PlayerFeature,
            require_active: bool = False,
        ) -> PlayerProtocol | None: ...
        # playback
        async def cmd_stop(self, player_id: str) -> None: ...
        async def cmd_play(self, player_id: str) -> None: ...
        async def _stop_player_or_its_queue(self, player: Player) -> None: ...
        async def _handle_cmd_resume(
            self, player_id: str, source: str | None = None, media: PlayerMedia | None = None
        ) -> None: ...
        async def _handle_play_media(self, player_id: str, media: PlayerMedia) -> None: ...
        async def _handle_cmd_stop(self, player_id: str) -> None: ...
        async def cmd_resume(
            self, player_id: str, source: str | None = None, media: PlayerMedia | None = None
        ) -> None: ...
        # sources
        async def select_source(self, player_id: str, source: str | None) -> None: ...
        def _get_active_audio_source(
            self, player: Player
        ) -> tuple[AudioSource, PluginProvider] | None: ...
        def _is_ma_managed_source(self, player: Player, source: str | None) -> bool: ...
        async def _notify_source_volume_change(self, player: Player, volume_level: int) -> None: ...
        def _resolve_command_target(self, player: Player, source_id: str | None) -> str: ...
        async def _forward_to_external_source(
            self,
            player: Player,
            action: SourceControl,
            value: SourceControlValue = None,
        ) -> bool: ...
        async def _release_audio_source(self, player_id: str) -> None: ...
        async def _handle_select_source(self, player_id: str, source: str | None) -> None: ...
        # power
        def _handle_external_power_off(
            self, player: Player, changed_values: dict[str, tuple[Any, Any]]
        ) -> None: ...
        def _check_idle_power_off(self, player: Player) -> None: ...
        def _restart_idle_count(
            self, player: Player, changed_values: dict[str, tuple[Any, Any]]
        ) -> None: ...
        async def _handle_cmd_power(
            self, player_id: str, powered: bool, skip_auto_play: bool = False
        ) -> None: ...
        async def _power_on_for_playback(
            self, player: Player, skip_auto_play: bool = False
        ) -> None: ...
        async def _power_on_switched_off_player(self, player_id: str) -> Player: ...
        def _power_on_member(
            self, owner: Player, member: Player, added: bool = False
        ) -> asyncio.Future[bool]: ...
        def _cancel_power_on(self, player: Player) -> None: ...
        # volume
        def _enforce_volume_limits(self, player: Player) -> bool: ...
        def _invalidate_group_volume_snapshot(self, player_id: str) -> None: ...
        def _unexpired_volume_target(self, player: Player) -> int | None: ...
        async def _handle_cmd_volume_set(
            self, player_id: str, volume_level: int, *, record_target: bool = True
        ) -> None: ...
        async def _handle_cmd_volume_mute(
            self, player: Player, mute_control: str, muted: bool
        ) -> None: ...
        async def _hand_over_volume(
            self, player: Player, volume_control: str | None, mute_control: str | None
        ) -> None: ...
        # grouping commands
        async def cmd_set_members(
            self,
            target_player: str,
            player_ids_to_add: list[str] | None = None,
            player_ids_to_remove: list[str] | None = None,
        ) -> None: ...
        async def cmd_ungroup(self, player_id: str) -> None: ...
        def _handle_membership_cleanup_on_state_change(
            self, player: Player, changed_values: dict[str, tuple[Any, Any]]
        ) -> None: ...
        async def _detach_member(self, member: Player) -> None: ...
        async def _handle_set_members(
            self,
            parent_player: Player,
            player_ids_to_add: list[str] | None = None,
            player_ids_to_remove: list[str] | None = None,
            *,
            new_content: bool = False,
        ) -> tuple[PlayerProtocol, object] | None: ...
        async def _transfer_ad_hoc_leadership(
            self, leader: Player, remaining_members: list[str]
        ) -> None: ...
        async def _release_group_session(self, group_id: str) -> None: ...
        # sleep timer
        def _clear_sleep_timer(self, player: Player) -> None: ...
        @staticmethod
        def _sleep_timer_task_id(player_id: str) -> str: ...
