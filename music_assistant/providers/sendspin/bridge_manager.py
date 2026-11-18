"""
Shared lifecycle management for Sendspin bridges.

A Sendspin bridge exposes a protocol of another provider (squeezelite, AirPlay,
Chromecast) as an external Sendspin client, so the device can take part in Sendspin
synchronized playback. The bridge is a derived transport: its Sendspin protocol
rides on the protocol it bridges (its underlying protocol) and belongs to the
player of that protocol.

A player has one Sendspin output, whichever of its protocols can be bridged. One of
them carries it: the first in BRIDGE_ORDER that holds it (see select_carrier). The
stored output names its carrier, and a bridge only exists for the carrier, while that
protocol is attached to its player and the user wants the Sendspin output.

SendspinBridgeManagerBase reconciles that in one place. Providers subclass it and
only supply policy (does this protocol qualify for a bridge?) and a bridge factory;
the base class decides which protocol of a player carries its output, hands the
output over when that changes, and reacts to the attach and detach of the protocol
it bridges, to an output being switched on or off and to providers (un)loading.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast

from music_assistant_models.enums import EventType

from music_assistant.constants import (
    CONF_ENABLED,
    CONF_PLAYER_PROTOCOLS,
    CONF_PLAYERS,
    CONF_PROTOCOL_EXPERIMENTAL_NOTE,
    CONF_PROTOCOL_OFF_BY_DEFAULT,
    CONF_UNDERLYING_PLAYER_ID,
)

from .constants import (
    CONF_SENDSPIN_BRIDGE_STATIC_DELAYS,
    CONF_SENDSPIN_CAST_OPT_IN,
    CONF_SENDSPIN_STATIC_DELAY,
)
from .helpers import bridge_client_id_variants

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from aiosendspin.server import SendspinServer
    from music_assistant_models.event import MassEvent

    from music_assistant.mass import MusicAssistant
    from music_assistant.models.player import Player
    from music_assistant.models.player_protocol import PlayerProtocol
    from music_assistant.models.player_provider import PlayerProvider

    from .provider import SendspinProvider

LOGGER = logging.getLogger(__name__)

# The providers whose bridges carry the Sendspin output of a player, the first choice first.
BRIDGE_ORDER: tuple[str, ...] = ("squeezelite", "airplay", "chromecast")

# The players reported to have more than one stored Sendspin output, by those outputs.
_REPORTED_OUTPUTS: set[tuple[str, ...]] = set()


class SendspinBridge(Protocol):
    """Interface a Sendspin bridge implementation must provide."""

    sendspin_server: SendspinServer

    @property
    def is_registered(self) -> bool:
        """Return whether the bridge is registered with Sendspin."""

    async def start(self) -> None:
        """Register the bridge as an external Sendspin client."""

    async def stop(self) -> None:
        """Stop and unregister the bridge."""


@dataclass(frozen=True)
class BridgeConnection:
    """A protocol of a player whose bridge can carry the Sendspin output of that player."""

    protocol_id: str
    domain: str
    # the user has the protocol, and its provider, switched on
    switched_on: bool
    attached: bool
    # its bridge manager wants a bridge for it, which is only known while it is attached
    qualifies: bool = False


def select_carrier(
    connections: Iterable[BridgeConnection], stored_carrier_id: str | None
) -> str | None:
    """
    Return the id of the protocol that carries the Sendspin output of a player.

    That is the first connection in BRIDGE_ORDER that holds the output. A connection the
    user has switched on holds it when it qualifies while it is attached, and when it is
    the one that carried the output before while it is away.

    :param connections: The protocols of the player that can be bridged.
    :param stored_carrier_id: Id of the protocol the stored output rides on, if any.
    :return: The id of the carrier, None when no connection holds the output.
    """
    for connection in sorted(connections, key=_place_in_order):
        if not connection.switched_on:
            continue
        if connection.attached:
            if connection.qualifies:
                return connection.protocol_id
        elif connection.protocol_id == stored_carrier_id:
            return connection.protocol_id
    return None


def get_bridge_output_id(mass: MusicAssistant, player_id: str) -> str | None:
    """
    Return the id of the stored Sendspin output the bridges of a player carry.

    :param mass: The Music Assistant instance.
    :param player_id: Id of the player.
    :return: The client id of the output, None when the player has none stored.
    """
    output_ids = sorted(
        protocol_id
        for protocol_id in mass.config.get_protocol_ids_of_player(player_id)
        if _is_bridge_output(mass.config.get_protocol_record(protocol_id))
    )
    if len(output_ids) < 2:
        return output_ids[0] if output_ids else None
    if (reported := tuple(output_ids)) not in _REPORTED_OUTPUTS:
        _REPORTED_OUTPUTS.add(reported)
        LOGGER.warning(
            "Player %s has more than one Sendspin output stored: %s",
            player_id,
            ", ".join(output_ids),
        )

    def _rank(output_id: str) -> tuple[bool, bool, str]:
        protocol = mass.players.get_protocol(output_id)
        record = mass.config.get_protocol_record(output_id) or {}
        return (
            protocol is None or protocol.player is None,
            not record.get(CONF_ENABLED, True),
            output_id,
        )

    return min(output_ids, key=_rank)


def hand_static_delay_over(mass: MusicAssistant, client_id: str, carrier_id: str) -> None:
    """
    Let a stored Sendspin output ride on another protocol, with the static delay set for it.

    A static delay the user set belongs to the protocol that carries the output: the delay
    set for the protocol before is put aside, and the one set while the new protocol
    carried the output before is back. Nothing happens for an output that is not stored
    or that rides on the protocol already.

    :param mass: The Music Assistant instance.
    :param client_id: The client id of the output.
    :param carrier_id: Id of the protocol the output rides on from now on.
    """
    if (record := mass.config.get_protocol_record(client_id)) is None:
        return
    values = dict(record.get("values") or {})
    if _move_carrier(values, carrier_id):
        mass.config.set(f"{CONF_PLAYER_PROTOCOLS}/{client_id}/values", values)


async def evaluate_bridge_of(mass: MusicAssistant, protocol_id: str) -> None:
    """
    Bring the Sendspin bridges of the player of a protocol in line with what is wanted.

    For the Sendspin side, when the Sendspin output of a bridged device is switched on or
    off. Nothing happens for a protocol that belongs to no player.

    :param mass: The Music Assistant instance.
    :param protocol_id: Id of the protocol the bridge rides on.
    """
    protocol = mass.players.get_protocol(protocol_id)
    if protocol is not None and protocol.player is not None:
        await _evaluate_player(mass, protocol.player)
    elif player_id := mass.players.get_player_id_of_protocol(protocol_id):
        await evaluate_bridges_of_player(mass, player_id)


async def evaluate_bridges_of_player(mass: MusicAssistant, player_id: str) -> None:
    """
    Bring the Sendspin bridges of a player in line with what is wanted.

    Nothing happens for a player that is not there.

    :param mass: The Music Assistant instance.
    :param player_id: Id of the player.
    """
    if (player := mass.players.get_player(player_id)) is not None:
        await _evaluate_player(mass, player)


class SendspinBridgeManagerBase[BridgeT: SendspinBridge](ABC):
    """
    Base class managing the Sendspin bridges for one player provider.

    Owns the desired-state reconciliation: a bridge exists if and only if its protocol
    carries the Sendspin output of its player and the lifecycle allows it (bridged
    protocol attached, the Sendspin output wanted, Sendspin provider available).
    Subclasses provide the policy and the bridge factory. Every id is the id of the
    provider's own protocol that is bridged.
    """

    # Translation key of the warning shown with a Sendspin output a bridge of this provider
    # carries. Such a bridge is experimental: the output is switched off by default until
    # the user switches it on once while a bridge of this provider carries it.
    experimental_note: str | None = None

    # Player state fields a change of which evaluates the bridges of the player again.
    _policy_state_fields: frozenset[str] = frozenset({"output_protocols"})

    def __init__(self, provider: PlayerProvider) -> None:
        """
        Initialize the bridge manager.

        :param provider: The player provider owning the bridged protocols.
        """
        self.provider = provider
        self.mass: MusicAssistant = provider.mass
        self.logger = provider.logger.getChild("bridge_manager")
        self._bridges: dict[str, BridgeT] = {}
        self._lock = asyncio.Lock()
        # bridged protocol id -> bridge client id, also for protocols without a bridge now
        self._client_ids: dict[str, str] = {}
        # bridged protocol id -> removal of its bridge after the protocol was detached
        self._removals: dict[str, asyncio.Task[None]] = {}
        self._unsubs: list[Callable[[], None]] = [
            self.mass.subscribe(self._on_providers_updated, EventType.PROVIDERS_UPDATED),
            self.mass.players.subscribe_player_state_update(self._on_player_state_updated),
        ]

    @property
    def sendspin_provider(self) -> SendspinProvider | None:
        """Get the Sendspin provider if available."""
        return cast("SendspinProvider | None", self.mass.get_provider("sendspin"))

    @property
    def sendspin_server(self) -> SendspinServer | None:
        """Get the Sendspin server if available."""
        if provider := self.sendspin_provider:
            return provider.server_api
        return None

    async def evaluate_bridge(self, protocol: PlayerProtocol) -> None:
        """
        Reconcile the Sendspin bridge of a protocol.

        Creates, recreates or removes the bridge to match the desired state
        derived from configuration and the current player graph, together with the
        bridges of the other protocols of its player. Idempotent and safe to call
        repeatedly: call it when the protocol is attached and when its config changes.
        Never await it from the attach of a protocol.

        :param protocol: The protocol to evaluate.
        """
        if (player := protocol.player) is not None and (
            self.provider.get_protocol(protocol.protocol_id) is protocol
        ):
            await _evaluate_player(self.mass, player)
            return
        if removal := self._removals.get(protocol.protocol_id):
            await removal
        if self._has_bridge(protocol.protocol_id):
            await self.remove_bridge(protocol.protocol_id)

    async def setup_bridge(self, protocol: PlayerProtocol) -> None:
        """
        Set up a Sendspin bridge for the given protocol.

        No-op when a bridge is already in place or the current state does not
        want/allow one.

        :param protocol: The protocol to bridge.
        """
        async with self._lock:
            protocol_id = protocol.protocol_id
            if self._has_bridge(protocol_id):
                self.logger.debug("Bridge already exists for %s", protocol.display_name)
                return
            if client_id := self.bridge_client_id(protocol):
                self._client_ids[protocol_id] = client_id
            if not (self.qualifies(protocol) and self._lifecycle_allows_bridge(protocol)):
                return
            if await self._try_claim_existing(protocol):
                self._hold_client(protocol)
                self._remove_bridge_on_detach(protocol)
                return
            if client_id and (taken_id := self._taken_by_another_player(protocol, client_id)):
                # a bridge of another player registered it meanwhile
                self.logger.debug(
                    "Not bridging %s: another player has %s", protocol.display_name, taken_id
                )
                self._evaluate_on_release(protocol, taken_id)
                return
            bridge: BridgeT | None = None
            try:
                bridge = self._create_bridge(protocol)
                await bridge.start()
            except Exception:
                self.logger.warning("Failed to start Sendspin bridge for %s", protocol.display_name)
                if bridge is not None:
                    if bridge.is_registered:
                        # it registered its client before it failed, and takes it along
                        self._hold_client(protocol, takes_output=False)
                    with suppress(Exception):
                        await bridge.stop()
                    self._release_client(protocol)
                return
            if not bridge.is_registered:
                return
            self._hold_client(protocol)
            self._bridges[protocol_id] = bridge
            self._remove_bridge_on_detach(protocol)
            self.logger.info("Sendspin bridge created for %s", protocol.display_name)

    async def remove_bridge(self, protocol_id: str, permanent: bool = False) -> None:
        """
        Remove the Sendspin bridge of a protocol.

        :param protocol_id: The id of the bridged protocol.
        :param permanent: Also remove the Sendspin output of its player with its config,
            when it rides on this protocol. Use when the protocol is removed for good;
            plain removal keeps the config so user settings survive.
        """
        async with self._lock:
            bridge = self._bridges.pop(protocol_id, None)
            if bridge:
                with suppress(Exception):
                    await bridge.stop()
        removed_any = bridge is not None
        client_id = self._client_ids.get(protocol_id)
        if permanent and client_id:
            removed_any = await self._remove_client_protocol(protocol_id, client_id) or removed_any
        if client_id and (sendspin_provider := self.sendspin_provider):
            sendspin_provider.release_bridge_client(client_id, protocol_id)
            if permanent:
                # its player may be gone for good, with the output another player waited for
                sendspin_provider.wake_bridge_waiters(client_id)
        if removed_any:
            self.logger.debug("Sendspin bridge removed for protocol %s", protocol_id)

    def get_bridge(self, protocol_id: str) -> BridgeT | None:
        """
        Get the bridge of a protocol.

        :param protocol_id: The id of the bridged protocol.
        """
        return self._bridges.get(protocol_id)

    def bridge_client_id(self, protocol: PlayerProtocol) -> str | None:
        """
        Return the Sendspin client id the bridge of a protocol registers under.

        That is the id of the Sendspin output its player has, else an id of the device of
        the protocol.

        :param protocol: The protocol to bridge.
        :return: The client id, None when the protocol cannot be bridged.
        """
        if client_id := self._client_ids.get(protocol.protocol_id):
            return client_id
        player = protocol.player
        return self._resolve_client_id(
            protocol, get_bridge_output_id(self.mass, player.player_id) if player else None
        )

    async def stop_all(self) -> None:
        """Stop all Sendspin bridges."""
        async with self._lock:
            for bridge in list(self._bridges.values()):
                with suppress(Exception):
                    await bridge.stop()
            if sendspin_provider := self.sendspin_provider:
                for protocol_id in self._bridges:
                    if client_id := self._client_ids.get(protocol_id):
                        sendspin_provider.release_bridge_client(client_id, protocol_id)
            self._bridges.clear()
        self.logger.debug("All Sendspin bridges stopped")

    async def close(self) -> None:
        """Stop all bridges and unsubscribe event listeners."""
        for unsub in self._unsubs:
            with suppress(Exception):
                unsub()
        self._unsubs.clear()
        await self.stop_all()

    @abstractmethod
    def qualifies(self, protocol: PlayerProtocol) -> bool:
        """
        Return whether provider policy wants a bridge for this protocol.

        Policy conditions of the protocol itself only (device capabilities, blocklists);
        which protocol of a player carries its Sendspin output and lifecycle conditions
        such as enabled state and Sendspin availability are handled by the base class.

        :param protocol: The protocol to evaluate.
        """

    @abstractmethod
    def _own_client_id(self, protocol: PlayerProtocol) -> str | None:
        """
        Return the Sendspin client_id a bridge of this provider gives a device.

        :param protocol: The protocol to resolve the client_id for.
        :return: The client_id, or None when the protocol cannot be bridged.
        """

    @abstractmethod
    def _create_bridge(self, protocol: PlayerProtocol) -> BridgeT:
        """
        Create a (not yet started) bridge instance for the given protocol.

        :param protocol: The protocol to create a bridge for.
        """

    async def _try_claim_existing(self, protocol: PlayerProtocol) -> bool:
        """
        Claim an already-registered external Sendspin client for this protocol.

        Default implementation claims nothing; subclasses can override to
        adopt clients that connected on their own (e.g. a JS Cast receiver).

        :param protocol: The protocol being bridged.
        :return: True when an existing client was claimed (skips bridge setup).
        """
        return False

    async def _reconcile_bridge(self, protocol: PlayerProtocol, carries: bool) -> None:
        """
        Create, recreate or remove the bridge of an attached protocol.

        The caller holds the bridge lock of the player of the protocol.

        :param protocol: The protocol, which is attached to its player.
        :param carries: Whether the protocol carries the Sendspin output of its player.
        """
        protocol_id = protocol.protocol_id
        if removal := self._removals.get(protocol_id):
            # the bridge of an earlier attach of the protocol goes first
            await removal
        client_id = self._client_ids.get(protocol_id)
        if not carries:
            if self._has_bridge(protocol_id):
                # Only the output of a player without another connection goes along with
                # the bridge of a protocol that must not have one; a plain removal keeps
                # the config, for the connection that takes the output over.
                await self.remove_bridge(
                    protocol_id,
                    permanent=not self.qualifies(protocol) and self._is_only_connection(protocol),
                )
            return
        if client_id:
            await self._heal_stale_client_disable(protocol, client_id)
        if not self._lifecycle_allows_bridge(protocol):
            if self._has_bridge(protocol_id):
                await self.remove_bridge(protocol_id)
            return
        if (bridge := self._bridges.get(protocol_id)) and self._is_bridge_stale(bridge):
            # The Sendspin provider was reloaded; rebuild against the new server
            await self.remove_bridge(protocol_id)
        if not self._has_bridge(protocol_id):
            await self.setup_bridge(protocol)

    def _resolve_client_id(self, protocol: PlayerProtocol, output_id: str | None) -> str | None:
        """
        Return the Sendspin client id the bridge of a protocol would register under.

        :param protocol: The protocol to bridge.
        :param output_id: The client id of the Sendspin output of its player, stored or
            registered by a bridge, if it has one.
        """
        if output_id is not None:
            return output_id
        if (client_id := self._own_client_id(protocol)) is None:
            return None
        if self._taken_by_another_player(protocol, client_id):
            return None
        return client_id

    def _has_bridge(self, protocol_id: str) -> bool:
        """Return whether a bridge is currently in place for the protocol."""
        return protocol_id in self._bridges

    def _hold_client(self, protocol: PlayerProtocol, *, takes_output: bool = True) -> None:
        """
        Record that the bridge of a protocol registered or claimed its client last.

        :param protocol: The bridged protocol.
        :param takes_output: Whether the Sendspin output of the client moves over to this
            protocol when it rode on another one.
        """
        if (client_id := self.bridge_client_id(protocol)) and (
            sendspin_provider := self.sendspin_provider
        ):
            sendspin_provider.hold_bridge_client(
                client_id, protocol.protocol_id, takes_output=takes_output
            )

    def _release_client(self, protocol: PlayerProtocol) -> None:
        """
        Forget that the bridge of a protocol holds its client, once that bridge is gone.

        :param protocol: The bridged protocol.
        """
        if (client_id := self.bridge_client_id(protocol)) and (
            sendspin_provider := self.sendspin_provider
        ):
            sendspin_provider.release_bridge_client(client_id, protocol.protocol_id)

    def _taken_by_another_player(self, protocol: PlayerProtocol, client_id: str) -> str | None:
        """
        Return the form of a client id that another player has, if one has it.

        That is the id of the client a bridge of another player holds, in either form of
        the MAC address of the device, and for a player without a stored Sendspin output of
        its own also the id of the stored Sendspin output of another player.

        :param protocol: The protocol to bridge.
        :param client_id: The client id its bridge would register under.
        """
        if (player := protocol.player) is None:
            return None
        own_protocol_ids = {
            *player.attached_protocols,
            *self.mass.config.get_protocol_ids_of_player(player.player_id),
        }
        sendspin_provider = self.sendspin_provider
        # of two players that each have an output of the device stored, the one whose
        # bridge registers first has it, as long as that bridge holds it
        has_own_output = get_bridge_output_id(self.mass, player.player_id) is not None
        for variant in bridge_client_id_variants(client_id):
            record = self.mass.config.get_protocol_record(variant)
            owner_id = record.get("player_id") if isinstance(record, dict) else None
            if (
                not has_own_output
                and isinstance(owner_id, str)
                and owner_id != player.player_id
                and self.mass.config.get(f"{CONF_PLAYERS}/{owner_id}")
            ):
                return variant
            holder = sendspin_provider.get_bridge_holder(variant) if sendspin_provider else None
            if holder is not None and holder not in own_protocol_ids:
                return variant
        return None

    def _wait_for_owner(self, protocol: PlayerProtocol) -> None:
        """
        Evaluate the bridge of a protocol again once another player lets go of its client id.

        Nothing happens when no other player has it.

        :param protocol: The protocol to bridge, which is attached to its player.
        """
        if (client_id := self._own_client_id(protocol)) and (
            taken_id := self._taken_by_another_player(protocol, client_id)
        ):
            self._evaluate_on_release(protocol, taken_id)

    def _evaluate_on_release(self, protocol: PlayerProtocol, client_id: str) -> None:
        """
        Evaluate the bridge of a protocol again once another player lets go of a client id.

        That is when the bridge that holds it lets go, or the protocol it rides on is removed
        for good. Nothing happens when the protocol was detached before.

        :param protocol: The protocol to bridge, which is attached to its player.
        :param client_id: The client id another player has.
        """
        if (sendspin_provider := self.sendspin_provider) is None:
            return
        protocol_id = protocol.protocol_id

        def _stop_waiting() -> None:
            if current_provider := self.sendspin_provider:
                current_provider.stop_waiting_for_bridge_client(client_id, protocol_id)

        def _on_release() -> None:
            with suppress(ValueError):
                protocol._on_detach_callbacks.remove(_stop_waiting)
            if self.provider.get_protocol(protocol_id) is protocol:
                self.mass.create_task(self.evaluate_bridge(protocol))

        if sendspin_provider.wait_for_bridge_client(client_id, protocol_id, _on_release):
            protocol._on_detach_callbacks.append(_stop_waiting)

    def _is_only_connection(self, protocol: PlayerProtocol) -> bool:
        """
        Return whether no other protocol of the player of a protocol can be bridged.

        :param protocol: The protocol, which is attached to its player.
        """
        if (player := protocol.player) is None:
            return True
        return all(
            connection.protocol_id == protocol.protocol_id
            for connection in _get_connections(self.mass, player)
        )

    def _lifecycle_allows_bridge(self, protocol: PlayerProtocol) -> bool:
        """Return whether the current lifecycle state allows a bridge for the protocol."""
        if self.sendspin_server is None:
            return False
        if self.provider.get_protocol(protocol.protocol_id) is not protocol:
            # not (or no longer) the attached protocol object
            return False
        if protocol.player is None:
            # a bridge belongs to the player of the protocol it rides on
            return False
        if client_id := self._client_ids.get(protocol.protocol_id):
            carrier_id = self.mass.config.get_protocol_value(client_id, CONF_UNDERLYING_PLAYER_ID)
            if isinstance(carrier_id, str) and carrier_id != protocol.protocol_id:
                # the output rides on another protocol of its player
                return False
            # the Sendspin output of the bridge can be switched off by the user
            # independently of the protocol it rides on
            return self.mass.players.wants_protocol(client_id)
        return True

    def _is_bridge_stale(self, bridge: BridgeT) -> bool:
        """Return whether the bridge is bound to a stale (replaced) Sendspin server."""
        return bridge.sendspin_server is not self.sendspin_server

    def _remove_bridge_on_detach(self, protocol: PlayerProtocol) -> None:
        """
        Stop the bridge of a protocol when that protocol is detached from its player.

        :param protocol: The bridged protocol, which is attached.
        """
        protocol_id = protocol.protocol_id

        def _forget(task: asyncio.Task[None]) -> None:
            if self._removals.get(protocol_id) is task:
                del self._removals[protocol_id]

        def _on_detach() -> None:
            removal = self.mass.create_task(self.remove_bridge(protocol_id))
            self._removals[protocol_id] = removal
            removal.add_done_callback(_forget)

        # the callbacks run once, at the next detach
        protocol._on_detach_callbacks.append(_on_detach)

    async def _remove_client_protocol(self, protocol_id: str, client_id: str) -> bool:
        """
        Remove the Sendspin output of a bridge client and its config, when it rides on a protocol.

        :param protocol_id: The id of the bridged protocol.
        :param client_id: The client id of its bridge.
        :return: Whether something was removed.
        """
        if (client := self.mass.players.get_protocol(client_id)) is not None:
            underlying_id = client.underlying_protocol_id
        elif record := self.mass.config.get_protocol_record(client_id):
            underlying_id = (record.get("values") or {}).get(CONF_UNDERLYING_PLAYER_ID)
        else:
            return False
        if underlying_id != protocol_id:
            return False
        await self.mass.players.remove_protocol(client_id)
        return True

    async def _heal_stale_client_disable(self, protocol: PlayerProtocol, client_id: str) -> None:
        """
        Re-enable a bridge client whose disabled state lost the player it was made for.

        :param protocol: The protocol a bridge is wanted for.
        :param client_id: The Sendspin bridge client_id computed for the protocol.
        """
        record = self.mass.config.get_protocol_record(client_id)
        if not record or record.get("enabled", True):
            return
        if self.sendspin_server is None or protocol.player is None:
            return
        # MAC-based client ids outlive the UUID-based protocol ids they were disabled
        # under; without that player there is no UI toggle to undo it.
        player_id = record.get("player_id")
        if player_id and self.mass.config.get(f"{CONF_PLAYERS}/{player_id}"):
            # the player the disable was made under still exists - respect it
            return
        self.logger.info(
            "Re-enabling Sendspin bridge client %s for %s: its former player no longer exists",
            client_id,
            protocol.display_name,
        )
        if (record.get("values") or {}).get(CONF_PROTOCOL_OFF_BY_DEFAULT):
            # an experimental carrier offers it to the user again, rather than take it as an opt-in
            self.mass.config.set_protocol_value(client_id, CONF_PROTOCOL_EXPERIMENTAL_NOTE, None)
        await self.mass.config.save_protocol_config(client_id, {"enabled": True})

    async def _on_providers_updated(self, event: MassEvent) -> None:
        """Re-evaluate all bridges when provider availability changes."""
        for protocol in self.provider.protocols:
            if protocol.player is not None:
                await self.evaluate_bridge(protocol)

    def _on_player_state_updated(
        self, updated_player: Player, changed_values: dict[str, tuple[Any, Any]]
    ) -> None:
        """Evaluate the bridges of a player again after a change its bridge policy depends on."""
        if not changed_values.keys() & self._policy_state_fields:
            return
        for protocol in list(updated_player.attached_protocols.values()):
            if protocol.provider is self.provider:
                self.mass.create_task(self.evaluate_bridge(protocol))


def _place_in_order(connection: BridgeConnection) -> int:
    """Return the place of a connection in BRIDGE_ORDER, after the last for an unlisted one."""
    if connection.domain in BRIDGE_ORDER:
        return BRIDGE_ORDER.index(connection.domain)
    return len(BRIDGE_ORDER)


def _is_bridge_output(record: Any) -> bool:
    """
    Return whether a stored protocol record is the Sendspin output of a bridged player.

    :param record: The stored record of a protocol, if there is one.
    """
    if not isinstance(record, dict):
        return False
    values = record.get("values")
    return (
        str(record.get("provider") or "").split("--", maxsplit=1)[0] == "sendspin"
        and isinstance(values, dict)
        and isinstance(values.get(CONF_UNDERLYING_PLAYER_ID), str)
    )


def _get_managed_protocols(
    player: Player,
) -> list[tuple[PlayerProtocol, SendspinBridgeManagerBase[Any]]]:
    """
    Return the attached protocols of a player that have a bridge manager, with that manager.

    :param player: The player.
    """
    return [
        (protocol, manager)
        for protocol in list(player.attached_protocols.values())
        if isinstance(
            manager := getattr(protocol.provider, "bridge_manager", None),
            SendspinBridgeManagerBase,
        )
    ]


def _get_connections(mass: MusicAssistant, player: Player) -> list[BridgeConnection]:
    """
    Return the protocols of a player whose bridge can carry its Sendspin output.

    :param mass: The Music Assistant instance.
    :param player: The player.
    """
    connections: dict[str, BridgeConnection] = {}
    for protocol_id in mass.config.get_protocol_ids_of_player(player.player_id):
        record = mass.config.get_protocol_record(protocol_id)
        if not isinstance(record, dict):
            continue
        domain = str(record.get("provider") or "").split("--", maxsplit=1)[0]
        if domain in BRIDGE_ORDER:
            connections[protocol_id] = BridgeConnection(
                protocol_id,
                domain,
                switched_on=mass.config.is_protocol_switched_on(protocol_id),
                attached=False,
            )
    for protocol, manager in _get_managed_protocols(player):
        connections[protocol.protocol_id] = BridgeConnection(
            protocol.protocol_id,
            protocol.provider.domain,
            switched_on=mass.config.is_protocol_switched_on(protocol.protocol_id),
            attached=True,
            qualifies=manager.qualifies(protocol)
            and manager._client_ids.get(protocol.protocol_id) is not None,
        )
    return list(connections.values())


def _move_carrier(values: dict[str, Any], carrier_id: str) -> bool:
    """
    Let the values of a stored Sendspin output name another carrier, with its static delay.

    :param values: The values of the output, changed in place.
    :param carrier_id: Id of the protocol the output rides on from now on.
    :return: Whether the output rode on another protocol before.
    """
    previous_id = values.get(CONF_UNDERLYING_PLAYER_ID)
    if not isinstance(previous_id, str) or previous_id == carrier_id:
        return False
    set_aside = dict(values.pop(CONF_SENDSPIN_BRIDGE_STATIC_DELAYS, None) or {})
    if (delay := values.pop(CONF_SENDSPIN_STATIC_DELAY, None)) is not None:
        set_aside[previous_id] = delay
    if (delay := set_aside.pop(carrier_id, None)) is not None:
        values[CONF_SENDSPIN_STATIC_DELAY] = delay
    if set_aside:
        values[CONF_SENDSPIN_BRIDGE_STATIC_DELAYS] = set_aside
    values[CONF_UNDERLYING_PLAYER_ID] = carrier_id
    return True


def _settle_output(
    mass: MusicAssistant,
    client_id: str,
    carrier: PlayerProtocol,
    manager: SendspinBridgeManagerBase[Any],
) -> bool:
    """
    Let the stored Sendspin output of a player follow the protocol that carries it.

    One step without a suspension, so the output never names a carrier with the settings of
    another one: the carrier, the static delay set for it, the warning of an experimental
    carrier and whether the output is switched on. An experimental carrier leaves an output
    it was not offered for yet switched off, until the user switches it on once; another
    carrier switches on what was only switched off as such a default.

    :param mass: The Music Assistant instance.
    :param client_id: The client id of the output.
    :param carrier: The attached protocol that carries the output.
    :param manager: The bridge manager of that protocol.
    :return: Whether the output was switched off.
    """
    if (record := mass.config.get_protocol_record(client_id)) is None:
        return False
    stored_values = record.get("values") or {}
    values = dict(stored_values)
    was_enabled = enabled = bool(record.get(CONF_ENABLED, True))
    if moved := _move_carrier(values, carrier.protocol_id):
        LOGGER.info(
            "The Sendspin output %s rides on %s from now on", client_id, carrier.display_name
        )
    if (
        enabled
        and values.get(CONF_PROTOCOL_EXPERIMENTAL_NOTE)
        and not values.get(CONF_SENDSPIN_CAST_OPT_IN)
    ):
        # switched on with the warning shown, also while its experimental carrier was away
        values[CONF_SENDSPIN_CAST_OPT_IN] = True
    if note := manager.experimental_note:
        if values.get(CONF_PROTOCOL_EXPERIMENTAL_NOTE) != note:
            values[CONF_PROTOCOL_EXPERIMENTAL_NOTE] = note
            if enabled and not values.get(CONF_SENDSPIN_CAST_OPT_IN):
                enabled = False
                values[CONF_PROTOCOL_OFF_BY_DEFAULT] = True
    else:
        values.pop(CONF_PROTOCOL_EXPERIMENTAL_NOTE, None)
        if values.pop(CONF_PROTOCOL_OFF_BY_DEFAULT, None):
            enabled = True
    if values != stored_values:
        mass.config.set(f"{CONF_PLAYER_PROTOCOLS}/{client_id}/values", values)
    if enabled != was_enabled:
        mass.config.set_protocol_value(client_id, CONF_ENABLED, enabled)
    if moved and (player := carrier.player) is not None:
        # the outputs of the player name the carrier also of an output that is not attached
        player.update_state(force_update=True)
    return was_enabled and not enabled


def _settle_carrier(
    mass: MusicAssistant,
    player: Player,
    protocols: list[tuple[PlayerProtocol, SendspinBridgeManagerBase[Any]]],
) -> tuple[str | None, str | None]:
    """
    Decide which protocol of a player carries its Sendspin output, and let the output follow it.

    Without a suspension, so no bridge of the player registers in between.

    :param mass: The Music Assistant instance.
    :param player: The player.
    :param protocols: Its attached protocols that have a bridge manager, with that manager.
    :return: The id of the protocol that carries the output, None when none does, and the
        client id of the output when its carrier switched it off.
    """
    stored_id = get_bridge_output_id(mass, player.player_id)
    # an output a bridge registered is not stored before its protocol is attached
    output_id = stored_id or next(
        (
            client_id
            for protocol, manager in protocols
            if manager._has_bridge(protocol.protocol_id)
            and (client_id := manager._client_ids.get(protocol.protocol_id))
        ),
        None,
    )
    for protocol, manager in protocols:
        if client_id := manager._resolve_client_id(protocol, output_id):
            manager._client_ids[protocol.protocol_id] = client_id
        else:
            manager._client_ids.pop(protocol.protocol_id, None)
            manager._wait_for_owner(protocol)
    stored_carrier_id = (
        mass.config.get_protocol_value(stored_id, CONF_UNDERLYING_PLAYER_ID) if stored_id else None
    )
    carrier_id = select_carrier(
        _get_connections(mass, player),
        stored_carrier_id if isinstance(stored_carrier_id, str) else None,
    )
    switched_off_id: str | None = None
    for protocol, manager in protocols:
        # also an output of its id that belongs to no player anymore, which it takes over
        if (
            protocol.protocol_id == carrier_id
            and (client_id := manager._client_ids.get(protocol.protocol_id))
            and _settle_output(mass, client_id, protocol, manager)
        ):
            switched_off_id = client_id
    return carrier_id, switched_off_id


async def _await_removals(mass: MusicAssistant, player: Player) -> None:
    """
    Let the bridges of the protocols that were detached from a player finish stopping.

    :param mass: The Music Assistant instance.
    :param player: The player.
    """
    for protocol_id in mass.config.get_protocol_ids_of_player(player.player_id):
        if protocol_id in player.attached_protocols:
            continue
        record = mass.config.get_protocol_record(protocol_id)
        provider = mass.get_provider(str(record.get("provider"))) if record else None
        manager = getattr(provider, "bridge_manager", None)
        if isinstance(manager, SendspinBridgeManagerBase) and (
            removal := manager._removals.get(protocol_id)
        ):
            await removal


async def _evaluate_player(mass: MusicAssistant, player: Player) -> None:
    """
    Bring the Sendspin bridges of a player in line with what is wanted.

    :param mass: The Music Assistant instance.
    :param player: The player.
    """
    if not (protocols := _get_managed_protocols(player)):
        return
    sendspin = cast("SendspinProvider | None", mass.get_provider("sendspin"))
    if sendspin is None:
        # nothing to register a bridge with
        for protocol, manager in protocols:
            await manager._reconcile_bridge(protocol, carries=False)
        return
    async with sendspin.bridge_lock(player.player_id):
        await _await_removals(mass, player)
        carrier_id, switched_off_id = _settle_carrier(mass, player, protocols)
        if switched_off_id is not None:
            # also when no bridge holds its client, as with a receiver that connected by itself
            await mass.players.unregister(switched_off_id)
        # the bridge that carried the output stops before the one that carries it starts
        for protocol, manager in sorted(
            protocols, key=lambda managed: managed[0].protocol_id == carrier_id
        ):
            await manager._reconcile_bridge(protocol, carries=protocol.protocol_id == carrier_id)
