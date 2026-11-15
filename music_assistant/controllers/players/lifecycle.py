"""
Player Lifecycle Mixin for the Player Controller.

Attaches and detaches the protocols of a player, creates, publishes and drops players,
and removes, enables and disables them.

This module provides the PlayerLifecycleMixin class which is inherited by
PlayerController to add the lifecycle of players and protocols.
"""

from __future__ import annotations

import asyncio
import math
import time
from contextlib import suppress
from typing import TYPE_CHECKING

from music_assistant_models.auth import Scope
from music_assistant_models.constants import PLAYER_CONTROL_NONE
from music_assistant_models.enums import (
    EventType,
    IdentifierType,
    PlaybackState,
    PlayerType,
    ProtocolRole,
)
from music_assistant_models.errors import (
    ActionUnavailable,
    PlayerCommandFailed,
    PlayerUnavailableError,
    UnsupportedFeaturedException,
)

from music_assistant.constants import (
    ATTR_FAKE_POWER,
    CONF_CACHED_ARP_MAC,
    CONF_PLAYER_DSP,
    CONF_PLAYER_QUEUES,
    CONF_PLAYERS,
    CONF_REPORTED_MAC,
    CONF_SESSION_ONLY,
)
from music_assistant.helpers.api import api_command
from music_assistant.helpers.util import enrich_device_mac_address, is_valid_mac_address
from music_assistant.models.player import Player
from music_assistant.models.player_provider import PlayerProvider

from .base import _PlayerControllerBase
from .constants import CACHE_CATEGORY_PLAYER_POWER, PlayerLockPurpose
from .grouping import apply_group_change
from .grouping_plan import ChangeKind, GroupChange

if TYPE_CHECKING:
    from music_assistant_models.config_entries import PlayerConfig

    from music_assistant.models.player_protocol import PlayerProtocol


# How long after a player provider was loaded (the builtin ones at every start) it gets to find
# its devices again before players/remove_unavailable may remove the players it did not find.
REMOVE_UNAVAILABLE_GRACE = 60.0


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


class PlayerLifecycleMixin(_PlayerControllerBase):
    """Mixin class providing the lifecycle of players and protocols for PlayerController."""

    async def register(self, protocol: PlayerProtocol) -> None:
        """
        Register a protocol: its provider found the device, or found it again.

        The controller attaches the protocol to the player it belongs to, which it creates
        for a new device, or holds it until it knows which player that is. It leaves it
        unattached for a player the user disabled or a protocol the user switched off.
        Registering an object that is attached already publishes what it reports now;
        another object under the same id takes its place. The protocol is told through on_attach
        and on_detach when it is actually attached to a player or detached from it, so do
        not assume it is attached when this returns.

        :param protocol: The protocol object to register.
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

    async def unregister(self, protocol_id: str) -> None:
        """
        Unregister a protocol: its provider lost the device, or unloads.

        The controller detaches the protocol from its player, which tells it through
        on_detach, and calls off a registration of it that is still in progress. The
        player stays and keeps its queue; it is unavailable while none of its other
        protocols is available, unless it is switched off at its external power control.
        A player that only exists while its device is connected goes with its last
        protocol, unless it is kept: the user changed its config, a plugin keeps it (see
        keep_player), or its queue has items. Silently ignored for a protocol the
        controller does not know.

        :param protocol_id: Id of the protocol to unregister.
        """
        # an attach that is still setting this protocol up stops at its next step
        self._attaching.pop(protocol_id, None)
        if (protocol := self._protocols.get(protocol_id)) is None:
            return
        if (
            (player := protocol.player) is not None
            and player.session_only
            and player.attached_protocols.keys() == {protocol_id}
            and (queue := self.mass.player_queues.get(player.player_id)) is not None
            and queue.items
        ):
            # a player that was played on is kept like one the user changed, before its
            # last protocol goes and takes its config with it
            self.keep_player(player.player_id)
        if player := await self._unbind(protocol):
            self.logger.info("Protocol %s detached from player %s", protocol_id, player.name)
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
        await self.unregister(protocol_id)
        self.mass.config.remove_protocol_record(protocol_id)
        if protocol is not None:
            # the provider forgot the device, so what it stored about it is not needed anymore
            self._unattached.pop(protocol_id, None)
        if player_id is None:
            return
        self._replace_protocol_references(player_id, protocol_id, None)
        if player := self._players.get(player_id):
            player.remove_protocol(protocol_id, forget=True)
        if self.mass.config.get_protocol_ids_of_player(player_id) or (
            player and player.attached_protocols
        ):
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
                player.available
                or not player.initialized.is_set()
                or any(protocol.available for protocol in player.attached_protocols.values())
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

    def keep_player(self, player_id: str) -> None:
        """
        Keep a player that only exists while its device is connected, also while it is away.

        Call it when something was set up for the player that its user expects to find again,
        such as a DJ on its queue: it is stored from then on, like the player of a device.
        Nothing happens for a player that is stored already, or that is not there.

        :param player_id: Id of the player.
        """
        player_id = self.resolve_player_id(player_id)
        if self.mass.config.get(f"{CONF_PLAYERS}/{player_id}") is not None:
            return
        if player_id not in self._players:
            return
        self.mass.config.store_session_player(player_id)
        self.logger.debug("Player %s is kept from now on", player_id)

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
        for a device before it registers it can ask this first.

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
                # the await above may have staled the guards of register()
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
            player.name,
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
            for protocol in player.attached_protocols.values()
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
        if (
            player.session_only
            and not player.attached_protocols
            and self.mass.config.get(f"{CONF_PLAYERS}/{player.player_id}") is None
        ):
            await self._drop_player(player.player_id, player)
            self.logger.info("Player removed: %s", player.name)
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
        if player is not None and player.attached_protocols.get(protocol_id) is protocol:
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
        if player.reported_type != PlayerType.GROUP:
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
            player.type.value,
            player_id,
            player.name,
        )
        self.mass.signal_event(EventType.PLAYER_ADDED, object_id=player_id, data=player)
        await self.mass.player_queues.on_player_register(player)

    async def _drop_player(self, player_id: str, player: Player | None) -> None:
        """
        Take a player away for good, with its saved queue, and let clients know it is gone.

        Every permanent removal of a player that is live or has a stored record goes here.

        :param player_id: Id of the player.
        :param player: The live player, whose protocols are detached already; None for a
            player that only has a stored record, such as a disabled one.
        """
        if player is not None:
            await self._unregister_player(player)
            self.mass.player_queues.on_player_remove(player_id, permanent=True)
        else:
            self.mass.player_queues.purge_saved_queue(player_id)
        # a live player nobody was told about yet goes unannounced
        if (player is None or player.initialized.is_set()) and not self.mass.closing:
            self.mass.signal_event(EventType.PLAYER_REMOVED, player_id)

    async def _unregister_player(self, player: Player) -> None:
        """
        Take a player out of the registry, with its locks, timers and live source.

        :param player: The player, whose protocols are detached already.
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
            protocol_ids.update(player.attached_protocols)
        if player is None and stored is None and not protocol_ids:
            return
        self._removing.add(player_id)
        try:
            if player is not None:
                await self._release_player_that_is_removed(player)
            is_group = (
                player.reported_type if player else (stored or {}).get("player_type")
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
            if player is not None or stored is not None:
                await self._drop_player(player_id, player)
            # a protocol found again during the awaits above belongs to another player now
            self._delete_player_config(
                player_id, set(self.mass.config.get_protocol_ids_of_player(player_id))
            )
        finally:
            self._removing.discard(player_id)
        self.logger.info("Player removed: %s", player.name if player else player_id)
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

        That is its own config and those of its protocols and its DSP and queue settings,
        so a device that returns starts out fresh. Any group that lists
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
        self._build_legacy_id_index()
        # a user access filter is an allow-list of player ids, so it must not be left
        # pointing at a player whose config was just wiped
        self.mass.create_task(
            self.mass.webserver.auth.remove_from_user_filters(player_ids=[player_id])
        )

    def _store_player_base(self, player: Player) -> None:
        """
        Store what a player has from its protocols now, for when none of them is attached.

        That is its provider and type, and what store_protocol_defaults() keeps.

        :param player: The player, which may have no stored config at all.
        """
        conf_key = f"{CONF_PLAYERS}/{player.player_id}"
        if not isinstance(stored := self.mass.config.get(conf_key), dict):
            return
        if stored.get("provider") != player.provider_id:
            self.mass.config.set(f"{conf_key}/provider", player.provider_id)
        if stored.get("player_type") != player.reported_type:
            self.mass.config.set(f"{conf_key}/player_type", player.reported_type)
        player.store_protocol_defaults()

    async def _restore_players(self) -> None:
        """Bring back every stored player that is enabled."""
        stored_players = self.mass.config.get(CONF_PLAYERS, {})
        for player_id, stored in list(stored_players.items()):
            if not isinstance(stored, dict) or not stored.get("enabled", True):
                continue
            if player_id in self._players:
                continue
            try:
                await self._publish_player(self._create_player(player_id))
            except Exception:
                self.logger.exception("Could not restore player %s", player_id)
                self._players.pop(player_id, None)

    async def _disable_player(self, player: Player) -> None:
        """
        Take a player the user disabled out of use.

        It leaves the players with a last update that it is unavailable; its config and its
        saved queue stay.

        :param player: The player to disable.
        """
        await self._stop_player_that_goes(player)
        for protocol in list(player.attached_protocols.values()):
            self._attaching.pop(protocol.protocol_id, None)
            await self._unbind(protocol)
            # its provider keeps the object, which a removal of the player still has to reach
            self._unattached[protocol.protocol_id] = protocol
        await self._unregister_player(player)
        self.mass.player_queues.on_player_remove(player.player_id, permanent=False)
        if player.initialized.is_set() and not self.mass.closing:
            # its last update carries the disabled config, which also makes it unavailable
            player.set_config(await self.mass.config.get_player_object_config(player))
            player.update_state(signal_event=False)
            self.mass.signal_event(
                EventType.PLAYER_UPDATED, object_id=player.player_id, data=player
            )
        self.logger.info("Player disabled: %s", player.name)
        self._schedule_update_all_players()

    async def _stop_player_that_goes(self, player: Player) -> None:
        """
        Stop what a player plays, or power it off, before it is disabled.

        :param player: The player that goes.
        """
        if not player.available:
            return
        # edge case: ensure that the player is powered off if the player gets disabled
        if player.power_control != PLAYER_CONTROL_NONE:
            await self._handle_cmd_power(player.player_id, False)
        elif player.playback_state != PlaybackState.IDLE:
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
                if player.available_for_playback and player.playback_state != PlaybackState.IDLE:
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
            self.logger.exception("Error ending the playback of %s before its removal", player.name)

    async def _enable_player(self, player_id: str) -> None:
        """
        Bring back a player the user enabled, and let its providers look for its device.

        :param player_id: Id of the player, whose config is stored as enabled already.
        """
        stored = self.mass.config.get(f"{CONF_PLAYERS}/{player_id}")
        if not isinstance(stored, dict):
            return
        await self._publish_player(self._create_player(player_id))
        provider_ids = {
            str(record.get("provider"))
            for protocol_id in self.mass.config.get_protocol_ids_of_player(player_id)
            if (record := self.mass.config.get_protocol_record(protocol_id))
        }
        for provider_id in provider_ids:
            if isinstance(provider := self.mass.get_provider(provider_id), PlayerProvider):
                provider.schedule_discovery()

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
