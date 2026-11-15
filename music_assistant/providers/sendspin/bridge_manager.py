"""
Shared lifecycle management for Sendspin bridges.

A Sendspin bridge exposes a protocol of another provider (AirPlay, Chromecast)
as an external Sendspin client, so the device can take part in Sendspin
synchronized playback. The bridge is a derived transport: its Sendspin protocol
rides on the protocol it bridges (its underlying protocol) and belongs to the
player of that protocol. It only exists while that protocol is attached to its
player and the user wants its Sendspin output.

SendspinBridgeManagerBase reconciles that lifecycle in one place. Providers
subclass it and only supply policy (should this protocol have a bridge?) and a
bridge factory; the base class reacts to the attach and detach of the protocol
it bridges, to the output being switched on or off and to the Sendspin provider
(un)loading.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from contextlib import suppress
from typing import TYPE_CHECKING, Protocol, cast

from music_assistant_models.enums import EventType

from music_assistant.constants import CONF_PLAYERS, CONF_UNDERLYING_PLAYER_ID

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from aiosendspin.server import SendspinServer
    from music_assistant_models.event import MassEvent

    from music_assistant.mass import MusicAssistant
    from music_assistant.models.player import Player
    from music_assistant.models.player_protocol import PlayerProtocol
    from music_assistant.models.player_provider import PlayerProvider

    from .provider import SendspinProvider


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


async def evaluate_bridge_of(mass: MusicAssistant, protocol_id: str) -> None:
    """
    Let the bridge manager of a protocol bring its Sendspin bridge in line with what is wanted.

    For the Sendspin side, when the Sendspin output of a bridged device is switched on or
    off. For a protocol that is not attached, the bridges of the protocols attached to its
    player are evaluated instead, so another provider can carry the output. Nothing happens
    for a protocol without a bridge manager.

    :param mass: The Music Assistant instance.
    :param protocol_id: Id of the protocol the bridge rides on.
    """
    protocol = mass.players.get_protocol(protocol_id)
    if protocol is not None and protocol.player is not None:
        protocols = [protocol]
    elif (player_id := mass.players.get_player_id_of_protocol(protocol_id)) and (
        player := mass.players.get_player(player_id)
    ):
        protocols = list(player.attached_protocols.values())
    else:
        return
    await _evaluate_bridges(protocols)


class SendspinBridgeManagerBase[BridgeT: SendspinBridge](ABC):
    """
    Base class managing the Sendspin bridges for one player provider.

    Owns the desired-state reconciliation: a bridge exists if and only if the
    provider policy wants one (_should_have_bridge) and the lifecycle allows it
    (bridged protocol attached, its Sendspin output wanted, Sendspin provider
    available). Subclasses provide the policy and the bridge factory. Every id
    is the id of the provider's own protocol that is bridged.
    """

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
        derived from configuration and the current player graph. Idempotent
        and safe to call repeatedly: call it when the protocol is attached and
        when its config changes.

        :param protocol: The protocol to evaluate.
        """
        protocol_id = protocol.protocol_id
        if removal := self._removals.get(protocol_id):
            # the bridge of an earlier attach of the protocol goes first
            await removal
        if client_id := self._bridge_client_id(protocol):
            self._client_ids[protocol_id] = client_id
        if not self._should_have_bridge(protocol):
            # Provider policy: this protocol should not have a bridge at all,
            # so also clean up the bridge client protocol/config. Only act on
            # bridges we actually own - the computed client_id may collide
            # with another protocol's bridge on the same device (shared MAC).
            if self._has_bridge(protocol_id):
                await self.remove_bridge(protocol_id, permanent=True)
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
            if client_id:
                await self._remove_outputs_left_by_other_bridges(protocol, client_id)
            await self.setup_bridge(protocol)

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
            if not (self._should_have_bridge(protocol) and self._lifecycle_allows_bridge(protocol)):
                return
            if await self._try_claim_existing(protocol):
                self._hold_client(protocol)
                self._remove_bridge_on_detach(protocol)
                return
            bridge: BridgeT | None = None
            try:
                bridge = self._create_bridge(protocol)
                await bridge.start()
            except Exception:
                self.logger.warning("Failed to start Sendspin bridge for %s", protocol.display_name)
                if bridge is not None:
                    with suppress(Exception):
                        await bridge.stop()
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
        :param permanent: Also remove the Sendspin protocol of the bridge client and its
            config, when it rides on this protocol. Use when the protocol should not have a
            bridge at all; plain removal keeps the config so user settings survive. When this
            removed something, the bridges of the other protocols of its player are evaluated
            again, so another one can take the Sendspin output of the device.
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
        if removed_any:
            self.logger.debug("Sendspin bridge removed for protocol %s", protocol_id)
            if permanent:
                self._let_other_bridges_take_over(protocol_id)

    def get_bridge(self, protocol_id: str) -> BridgeT | None:
        """
        Get the bridge of a protocol.

        :param protocol_id: The id of the bridged protocol.
        """
        return self._bridges.get(protocol_id)

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
    def _bridge_client_id(self, protocol: PlayerProtocol) -> str | None:
        """
        Return the Sendspin client_id used to bridge the given protocol.

        :param protocol: The protocol to resolve the bridge client_id for.
        :return: The client_id, or None when the protocol cannot be bridged.
        """

    @abstractmethod
    def _create_bridge(self, protocol: PlayerProtocol) -> BridgeT:
        """
        Create a (not yet started) bridge instance for the given protocol.

        :param protocol: The protocol to create a bridge for.
        """

    @abstractmethod
    def _should_have_bridge(self, protocol: PlayerProtocol) -> bool:
        """
        Return whether provider policy wants a bridge for this protocol.

        Policy conditions only (device capabilities, blocklists, preferred
        alternative transports); lifecycle conditions such as enabled state
        and Sendspin availability are handled by the base class.

        :param protocol: The protocol to evaluate.
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

    def _has_bridge(self, protocol_id: str) -> bool:
        """Return whether a bridge is currently in place for the protocol."""
        return protocol_id in self._bridges

    def _hold_client(self, protocol: PlayerProtocol) -> None:
        """
        Record that the bridge of a protocol registered or claimed its client last.

        :param protocol: The bridged protocol.
        """
        if (client_id := self._bridge_client_id(protocol)) and (
            sendspin_provider := self.sendspin_provider
        ):
            sendspin_provider.hold_bridge_client(client_id, protocol.protocol_id)

    def _has_switched_on_output(self, player: Player, protocol_domain: str) -> bool:
        """
        Return whether a player has an output of a protocol that the user has switched on.

        An output that is away counts; one switched off as an output of the player, or whose
        provider is disabled, does not.

        :param player: The player to look at.
        :param protocol_domain: The domain of the protocol of the output, such as "airplay".
        """
        if (output := player.get_output_protocol_by_domain(protocol_domain)) is None:
            return False
        # a native output is listed as "native", not under the id of its protocol
        return output.is_native or self.mass.config.is_protocol_switched_on(
            output.output_protocol_id
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
        Remove the Sendspin protocol of a bridge client and its config, when it is ours.

        Another bridge manager can compute the same client id for the same device (an
        AirPlay id equal to the Cast MAC), so only a client riding on this protocol goes. A
        client the bridge of another protocol holds keeps its output, which is attached again
        riding on that protocol.

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
        if (sendspin_provider := self.sendspin_provider) and sendspin_provider.get_bridge_holder(
            client_id
        ) not in (None, protocol_id):
            sendspin_provider.refresh_bridge_client(client_id)
            return False
        await self.mass.players.remove_protocol(client_id)
        return True

    async def _remove_outputs_left_by_other_bridges(
        self, protocol: PlayerProtocol, client_id: str
    ) -> None:
        """
        Remove the Sendspin outputs other bridges left on the player of a protocol.

        Those ride on a protocol of the player that is switched off or not attached, so they
        carry no Sendspin output now, yet keep the place of the Sendspin output of the device
        from the bridge of the given protocol, which its policy allows to register.

        :param protocol: The protocol whose bridge is about to register.
        :param client_id: The client id that bridge registers under.
        """
        if (player := protocol.player) is None or (sendspin := self.sendspin_provider) is None:
            return
        for other_id in self.mass.config.get_protocol_ids_of_player(player.player_id):
            record = self.mass.config.get_protocol_record(other_id) or {}
            if other_id == client_id or record.get("provider") != sendspin.instance_id:
                continue
            underlying_id = (record.get("values") or {}).get(CONF_UNDERLYING_PLAYER_ID)
            if not isinstance(underlying_id, str):
                continue
            if underlying_id in player.attached_protocols and (
                self.mass.config.is_protocol_switched_on(underlying_id)
            ):
                continue
            await self.mass.players.remove_protocol(other_id)

    def _let_other_bridges_take_over(self, protocol_id: str) -> None:
        """
        Let the bridges of the other protocols of its player take the Sendspin output over.

        Call it once a protocol gave up the Sendspin output of its device for good: a bridge
        of another protocol that waited for it, or failed to register, can take it now.

        :param protocol_id: The id of the protocol whose bridge stepped back.
        """
        self.mass.create_task(self._evaluate_other_bridges(protocol_id), eager_start=False)

    async def _evaluate_other_bridges(self, protocol_id: str) -> None:
        """
        Evaluate the bridges of the other protocols attached to the player of a protocol.

        :param protocol_id: The id of the protocol whose bridge stepped back.
        """
        if not (player_id := self.mass.players.get_player_id_of_protocol(protocol_id)) or not (
            player := self.mass.players.get_player(player_id)
        ):
            return
        await _evaluate_bridges(
            protocol
            for protocol in list(player.attached_protocols.values())
            if protocol.protocol_id != protocol_id
        )

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
        await self.mass.config.save_protocol_config(client_id, {"enabled": True})

    async def _on_providers_updated(self, event: MassEvent) -> None:
        """Re-evaluate all bridges when provider availability changes."""
        for protocol in self.provider.protocols:
            if protocol.player is not None:
                await self.evaluate_bridge(protocol)


async def _evaluate_bridges(protocols: Iterable[PlayerProtocol]) -> None:
    """
    Let the bridge manager of each protocol bring its Sendspin bridge in line.

    :param protocols: The attached protocols; one without a bridge manager is skipped.
    """
    for protocol in protocols:
        manager = getattr(protocol.provider, "bridge_manager", None)
        if isinstance(manager, SendspinBridgeManagerBase):
            await manager.evaluate_bridge(protocol)
