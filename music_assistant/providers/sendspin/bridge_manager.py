"""
Shared lifecycle management for Sendspin bridges.

A Sendspin bridge exposes a player of another protocol (AirPlay, Chromecast,
a local soundcard) as an external Sendspin client, so the device can take part
in Sendspin synchronized playback. The bridge is a derived transport: it can
only exist while the player it rides on exists and is enabled.

SendspinBridgeManagerBase reconciles that lifecycle in one place. Providers
subclass it and only supply policy (should this player have a bridge?) and a
bridge factory; the base class reacts to config changes and provider
(un)loads, tears bridges down when their base player is disabled and recreates
them when it returns.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from contextlib import suppress
from typing import TYPE_CHECKING, Protocol, cast

from music_assistant_models.enums import EventType

from music_assistant.constants import CONF_PLAYERS, CONF_PROTOCOL_PARENT_ID

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiosendspin.server import SendspinServer
    from music_assistant_models.event import MassEvent

    from music_assistant.mass import MusicAssistant
    from music_assistant.models.player import Player
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


class SendspinBridgeManagerBase[BridgeT: SendspinBridge](ABC):
    """
    Base class managing the Sendspin bridges for one player provider.

    Owns the desired-state reconciliation: a bridge exists if and only if the
    provider policy wants one (_should_have_bridge) and the lifecycle allows it
    (base player registered and enabled, bridge client enabled, Sendspin
    provider available). Subclasses provide the policy and the bridge factory.
    """

    def __init__(self, provider: PlayerProvider) -> None:
        """
        Initialize the bridge manager.

        :param provider: The player provider owning the bridged players.
        """
        self.provider = provider
        self.mass: MusicAssistant = provider.mass
        self.logger = provider.logger.getChild("bridge_manager")
        self._bridges: dict[str, BridgeT] = {}
        self._lock = asyncio.Lock()
        # base player_id -> bridge client_id, kept for config-event routing
        # (also for players that currently have no bridge)
        self._client_ids: dict[str, str] = {}
        self._unsubs: list[Callable[[], None]] = [
            self.mass.subscribe(self._on_player_config_updated, EventType.PLAYER_CONFIG_UPDATED),
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

    async def evaluate_bridge(self, player: Player) -> None:
        """
        Reconcile the Sendspin bridge state for a player.

        Creates, recreates or removes the bridge to match the desired state
        derived from configuration and the current player graph. Idempotent
        and safe to call repeatedly.

        :param player: The player to evaluate.
        """
        player_id = player.player_id
        if client_id := self._bridge_client_id(player):
            self._client_ids[player_id] = client_id
        if not self._should_have_bridge(player):
            # Provider policy: this player should not have a bridge at all,
            # so also clean up the bridge client player/config. Only act on
            # bridges we actually own - the computed client_id may collide
            # with another protocol's bridge on the same device (shared MAC).
            if self._has_bridge(player_id):
                await self.remove_bridge(player_id, permanent=True)
            return
        if client_id:
            await self._heal_stale_client_disable(player, client_id)
        if not self._lifecycle_allows_bridge(player):
            if self._has_bridge(player_id):
                await self.remove_bridge(player_id)
            return
        if (bridge := self._bridges.get(player_id)) and self._is_bridge_stale(bridge):
            # The Sendspin provider was reloaded; rebuild against the new server
            await self.remove_bridge(player_id)
        if not self._has_bridge(player_id):
            await self.setup_bridge(player)

    async def setup_bridge(self, player: Player) -> None:
        """
        Set up a Sendspin bridge for the given player.

        No-op when a bridge is already in place or the current state does not
        want/allow one.

        :param player: The player to bridge.
        """
        async with self._lock:
            player_id = player.player_id
            if self._has_bridge(player_id):
                self.logger.debug("Bridge already exists for %s", player.display_name)
                return
            if not (self._should_have_bridge(player) and self._lifecycle_allows_bridge(player)):
                return
            if await self._try_claim_existing(player):
                return
            bridge: BridgeT | None = None
            try:
                bridge = self._create_bridge(player)
                await bridge.start()
            except Exception:
                self.logger.warning("Failed to start Sendspin bridge for %s", player.display_name)
                if bridge is not None:
                    with suppress(Exception):
                        await bridge.stop()
                return
            if not bridge.is_registered:
                return
            self._bridges[player_id] = bridge
            self.logger.info("Sendspin bridge created for %s", player.display_name)

    async def remove_bridge(self, player_id: str, permanent: bool = False) -> None:
        """
        Remove the Sendspin bridge for a player.

        :param player_id: The (base) player ID to remove the bridge for.
        :param permanent: Also remove the bridge client player and its config.
            Use when the player should not have a bridge at all; plain removal
            keeps the config so user settings survive a re-enable.
        """
        async with self._lock:
            bridge = self._bridges.pop(player_id, None)
            if bridge:
                with suppress(Exception):
                    await bridge.stop()
        removed_any = bridge is not None
        if permanent and (client_id := self._client_ids.get(player_id)):
            if self.mass.players.get_player(client_id):
                await self.mass.players.unregister(client_id, permanent=True)
                removed_any = True
            elif self.mass.config.get(f"{CONF_PLAYERS}/{client_id}"):
                self.mass.players.delete_player_config(client_id)
                removed_any = True
        if removed_any:
            self.logger.debug("Sendspin bridge removed for player %s", player_id)

    def get_bridge(self, player_id: str) -> BridgeT | None:
        """
        Get the bridge for a player.

        :param player_id: The (base) player ID to look up.
        """
        return self._bridges.get(player_id)

    async def stop_all(self) -> None:
        """Stop all Sendspin bridges."""
        async with self._lock:
            for bridge in list(self._bridges.values()):
                with suppress(Exception):
                    await bridge.stop()
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
    def _bridge_client_id(self, player: Player) -> str | None:
        """
        Return the Sendspin client_id used to bridge the given player.

        :param player: The player to resolve the bridge client_id for.
        :return: The client_id, or None when the player cannot be bridged.
        """

    @abstractmethod
    def _create_bridge(self, player: Player) -> BridgeT:
        """
        Create a (not yet started) bridge instance for the given player.

        :param player: The player to create a bridge for.
        """

    @abstractmethod
    def _should_have_bridge(self, player: Player) -> bool:
        """
        Return whether provider policy wants a bridge for this player.

        Policy conditions only (device capabilities, blocklists, preferred
        alternative transports); lifecycle conditions such as enabled state
        and Sendspin availability are handled by the base class.

        :param player: The player to evaluate.
        """

    async def _try_claim_existing(self, player: Player) -> bool:
        """
        Claim an already-registered external Sendspin client for this player.

        Default implementation claims nothing; subclasses can override to
        adopt clients that connected on their own (e.g. a JS Cast receiver).

        :param player: The player being bridged.
        :return: True when an existing client was claimed (skips bridge setup).
        """
        return False

    def _has_bridge(self, player_id: str) -> bool:
        """Return whether a bridge is currently in place for the player."""
        return player_id in self._bridges

    def _lifecycle_allows_bridge(self, player: Player) -> bool:
        """Return whether the current lifecycle state allows a bridge for the player."""
        if self.sendspin_server is None:
            return False
        if self.mass.players.get_player(player.player_id) is not player:
            # not (or no longer) the registered player instance
            return False
        if not self._is_player_enabled(player.player_id):
            return False
        if client_id := self._client_ids.get(player.player_id):
            # the bridge client (= the derived Sendspin protocol player) can be
            # disabled by the user independently of the base player
            return self._is_player_enabled(client_id)
        return True

    def _is_bridge_stale(self, bridge: BridgeT) -> bool:
        """Return whether the bridge is bound to a stale (replaced) Sendspin server."""
        return bridge.sendspin_server is not self.sendspin_server

    def _is_player_enabled(self, player_id: str) -> bool:
        """Return the persisted enabled state for a player (default True)."""
        raw_conf = self.mass.config.get(f"{CONF_PLAYERS}/{player_id}")
        if not raw_conf:
            return True
        return bool(raw_conf.get("enabled", True))

    async def _heal_stale_client_disable(self, player: Player, client_id: str) -> None:
        """
        Re-enable a bridge client whose disabled state lost its owning parent.

        :param player: The base player a bridge is wanted for.
        :param client_id: The Sendspin bridge client_id computed for the player.
        """
        raw_conf = self.mass.config.get(f"{CONF_PLAYERS}/{client_id}")
        if not raw_conf or raw_conf.get("enabled", True):
            return
        if self.sendspin_server is None or not self._is_player_enabled(player.player_id):
            return
        # MAC-based client ids outlive the UUID-based parent ids they were
        # disabled under; without the parent there is no UI toggle to undo it.
        values = raw_conf.get("values") or {}
        parent_id = values.get(CONF_PROTOCOL_PARENT_ID)
        if parent_id and self.mass.config.get(f"{CONF_PLAYERS}/{parent_id}"):
            # the parent the disable was made under still exists - respect it
            return
        self.logger.info(
            "Re-enabling Sendspin bridge client %s for %s: former parent no longer exists",
            client_id,
            player.display_name,
        )
        await self.mass.config.save_player_config(client_id, {"enabled": True})

    async def _on_player_config_updated(self, event: MassEvent) -> None:
        """Re-evaluate the bridge of a player affected by a config change."""
        player_id = event.object_id
        if not player_id:
            return
        # a config change on a bridge client maps back to its base player
        for base_player_id, client_id in self._client_ids.items():
            if client_id == player_id:
                player_id = base_player_id
                break
        if player := self.mass.players.get_player(player_id):
            if player.provider is not self.provider:
                return
            await self.evaluate_bridge(player)
        elif self._has_bridge(player_id):
            # base player disabled/unregistered - tear down its bridge
            await self.remove_bridge(player_id)

    async def _on_providers_updated(self, event: MassEvent) -> None:
        """Re-evaluate all bridges when provider availability changes."""
        for player in self.provider.players:
            await self.evaluate_bridge(player)
