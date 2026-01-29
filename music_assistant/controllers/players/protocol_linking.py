"""
Protocol Linking Mixin for the Player Controller.

Handles all logic for linking protocol players (AirPlay, Chromecast, DLNA) to
native players or wrapping them in Universal Players.

This module provides the ProtocolLinkingMixin class which is inherited by
PlayerController to add protocol linking capabilities.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, cast

from music_assistant_models.enums import IdentifierType, PlayerFeature, PlayerType, ProviderType
from music_assistant_models.errors import PlayerCommandFailed
from music_assistant_models.player import OutputProtocol

from music_assistant.constants import (
    CONF_LINKED_PROTOCOL_PLAYER_IDS,
    CONF_PLAYERS,
    CONF_PREFERRED_OUTPUT_PROTOCOL,
    CONF_PROTOCOL_PARENT_ID,
)
from music_assistant.helpers.util import is_locally_administered_mac, resolve_real_mac_address
from music_assistant.models.player import PROTOCOL_PRIORITY, DeviceInfo, Player
from music_assistant.providers.universal_player import UniversalPlayer, UniversalPlayerProvider
from music_assistant.providers.universal_player.constants import UNIVERSAL_PLAYER_PREFIX

if TYPE_CHECKING:
    from collections.abc import Coroutine
    from typing import Any

    from music_assistant import MusicAssistant


class ProtocolLinkingMixin:
    """
    Mixin class providing protocol linking functionality for PlayerController.

    Handles the complex logic of:
    - Matching protocol players to native players via device identifiers
    - Creating Universal Players for devices without native support
    - Managing protocol links and their lifecycle
    - Selecting the best output protocol for playback

    This mixin expects to be mixed with a class that provides:
    - mass: MusicAssistant instance
    - _players: dict of registered players
    - _pending_protocol_evaluations: dict of pending protocol evaluations
    - _universal_player_locks: dict of locks for universal player creation
    - logger: logging.Logger instance
    - all(): method to get all players
    - get(): method to get a player by ID
    - unregister(): method to unregister a player
    """

    # Type hints for attributes provided by the class this mixin is used with
    if TYPE_CHECKING:
        mass: MusicAssistant
        _players: dict[str, Player]
        _pending_protocol_evaluations: dict[str, asyncio.TimerHandle]
        _universal_player_locks: dict[str, asyncio.Lock]
        logger: logging.Logger

        def all(  # noqa: D102
            self,
            return_unavailable: bool = True,
            return_disabled: bool = False,
            provider_filter: str | None = None,
            return_sync_groups: bool = True,
            return_protocol_players: bool = False,
        ) -> list[Player]: ...

        def get(self, player_id: str) -> Player | None: ...  # noqa: D102

        def unregister(  # noqa: D102
            self, player_id: str, permanent: bool = False
        ) -> Coroutine[Any, Any, None]: ...

    def _is_protocol_player(self, player: Player) -> bool:
        """
        Check if a player is a generic protocol player without native support.

        Protocol players have PlayerType.PROTOCOL set by their provider, indicating
        they are generic streaming endpoints (e.g., AirPlay receiver, Chromecast device)
        without vendor-specific native support in Music Assistant.
        """
        return player.type == PlayerType.PROTOCOL

    async def _enrich_player_identifiers(self, player: Player) -> None:
        """
        Enrich player identifiers with real MAC address if needed.

        Some devices report different virtual/locally administered MAC addresses per protocol
        (AirPlay, DLNA, Chromecast may all have different MACs for the same device).
        This also applies to native players that may report virtual MACs.
        This method tries to resolve the actual hardware MAC via ARP and adds it as an
        additional identifier to enable proper matching between protocols and native players.
        """
        identifiers = player.device_info.identifiers
        reported_mac = identifiers.get(IdentifierType.MAC_ADDRESS)
        ip_address = identifiers.get(IdentifierType.IP_ADDRESS)

        # Skip if no IP available (can't do ARP lookup)
        if not ip_address:
            return

        # Skip if MAC already looks like a real one (not locally administered)
        if reported_mac and not is_locally_administered_mac(reported_mac):
            return

        # Try to resolve real MAC via ARP
        real_mac = await resolve_real_mac_address(reported_mac, ip_address)
        if real_mac and real_mac.upper() != (reported_mac or "").upper():
            # Add the real MAC as an additional identifier
            # Keep the original MAC too (for protocol-specific matching)
            player.device_info.add_identifier(IdentifierType.MAC_ADDRESS, real_mac)
            self.logger.debug(
                "Resolved real MAC for %s: %s -> %s",
                player.display_name,
                reported_mac,
                real_mac,
            )

    def _evaluate_protocol_links(self, player: Player) -> None:
        """
        Evaluate and establish protocol links for a player.

        Called when a player is registered to:
        1. If it's from a protocol provider - try to link to a native player.
        2. If it's a native player - try to link any existing protocol players.
        """
        if player.type == PlayerType.PROTOCOL:
            # Protocol player: try to find a native parent
            self._try_link_protocol_to_native(player)
        else:
            # Native player: try to find protocol players to link
            self._try_link_protocols_to_native(player)

    def _try_link_protocol_to_native(self, protocol_player: Player) -> None:
        """Try to link a protocol player to a native player."""
        protocol_domain = protocol_player.provider.domain

        # Look for a matching native player
        # Protocol players should only link to:
        # 1. True native players (Sonos, etc.)
        # 2. Universal players
        # NOT to other protocol players (they get merged via universal_player)
        for native_player in self.all(return_protocol_players=False):
            if native_player.player_id == protocol_player.player_id:
                continue
            # Skip all protocol players - they should be handled via universal_player
            if native_player.type == PlayerType.PROTOCOL:
                continue

            # For universal players, check if this protocol player is in its stored list
            if native_player.provider.domain == "universal_player":
                if isinstance(native_player, UniversalPlayer):
                    if protocol_player.player_id in native_player._protocol_player_ids:
                        self._add_protocol_link(native_player, protocol_player, protocol_domain)
                        # Copy identifiers from protocol player to universal player
                        # This is important for restored universal players which start
                        # with empty identifiers
                        for conn_type, value in protocol_player.device_info.identifiers.items():
                            native_player.device_info.add_identifier(conn_type, value)
                        # Update model/manufacturer if universal player has generic values
                        self._update_universal_device_info(native_player, protocol_player)
                        # Persist updated data to config (async via task)
                        self._save_universal_player_data(native_player)
                        protocol_player.update_state()
                        native_player.update_state()
                        return
                continue

            # Check cached protocol IDs first for fast matching on restart
            cached_ids = self._get_cached_protocol_ids(native_player.player_id)
            if protocol_player.player_id in cached_ids:
                self._add_protocol_link(native_player, protocol_player, protocol_domain)
                protocol_player.update_state()
                native_player.update_state()
                return

            # Fallback to identifier matching
            if self._identifiers_match(native_player, protocol_player, protocol_domain):
                self._add_protocol_link(native_player, protocol_player, protocol_domain)
                protocol_player.update_state()
                native_player.update_state()
                return

        # No native player found - schedule delayed evaluation to allow other protocols to register
        if not protocol_player._attr_protocol_parent_id:
            self._schedule_protocol_evaluation(protocol_player)

    def _schedule_protocol_evaluation(self, protocol_player: Player) -> None:
        """
        Schedule a delayed protocol evaluation.

        Delays evaluation to allow other protocol players and native players to register.
        Uses a longer delay (30s) if this protocol player was previously linked to a native
        player that hasn't registered yet, giving native providers time to start up.
        """
        player_id = protocol_player.player_id

        # Cancel any existing pending evaluation for this player
        if player_id in self._pending_protocol_evaluations:
            self._pending_protocol_evaluations[player_id].cancel()

        # Check if this protocol player has a cached parent (was previously linked)
        cached_parent_id = self._get_cached_protocol_parent_id(player_id)
        if cached_parent_id and not self.get(cached_parent_id):
            # Previously linked to a native player that hasn't registered yet
            # Use longer delay to give native providers time to start up
            delay = 30.0
            self.logger.debug(
                "Protocol player %s waiting for cached parent %s (30s delay)",
                player_id,
                cached_parent_id,
            )
        else:
            # Standard delay for protocol player discovery
            # Allows time for other protocols and native players to register
            delay = 10.0

        # Schedule evaluation after the delay
        handle = self.mass.loop.call_later(
            delay,
            lambda: self.mass.create_task(self._delayed_protocol_evaluation(player_id)),
        )
        self._pending_protocol_evaluations[player_id] = handle

    async def _delayed_protocol_evaluation(self, player_id: str) -> None:
        """
        Perform delayed protocol evaluation.

        Called after a delay to allow all protocol players for a device to register.
        Decides whether to create a universal player, join an existing one, or
        promote a single protocol player directly.
        """
        self._pending_protocol_evaluations.pop(player_id, None)

        protocol_player = self.get(player_id)
        if not protocol_player or protocol_player._attr_protocol_parent_id:
            return

        protocol_domain = protocol_player.provider.domain

        # Check if there's an existing universal player we should join
        if existing_universal := self._find_matching_universal_player(protocol_player):
            await self._add_protocol_to_existing_universal(
                existing_universal, protocol_player, protocol_domain
            )
            return

        # Find all protocol players that match this device's identifiers
        matching_protocols = self._find_matching_protocol_players(protocol_player)

        # Create or update UniversalPlayer for all protocol players
        await self._create_or_update_universal_player(matching_protocols)

    def _find_matching_protocol_players(self, protocol_player: Player) -> list[Player]:
        """
        Find all protocol players that match the same device as the given player.

        Searches through all registered protocol players to find ones that share
        identifiers (MAC, IP, UUID) with the given player, indicating they represent
        the same physical device.
        """
        matching = [protocol_player]

        for other_player in self.all(return_protocol_players=True):
            if other_player.player_id == protocol_player.player_id:
                continue
            if other_player.type != PlayerType.PROTOCOL:
                continue
            if other_player._attr_protocol_parent_id:
                continue
            if self._identifiers_match(protocol_player, other_player):
                matching.append(other_player)

        return matching

    def _find_matching_universal_player(self, protocol_player: Player) -> Player | None:
        """Find an existing universal player that matches this protocol player."""
        for player in self._players.values():
            if player.provider.domain != "universal_player":
                continue
            if self._identifiers_match(protocol_player, player, ""):
                return player
        return None

    async def _add_protocol_to_existing_universal(
        self, universal_player: Player, protocol_player: Player, protocol_domain: str
    ) -> None:
        """Add a protocol player to an existing universal player."""
        self._add_protocol_link(universal_player, protocol_player, protocol_domain)

        if isinstance(universal_player, UniversalPlayer):
            universal_player.add_protocol_player(protocol_player.player_id)
            for conn_type, value in protocol_player.device_info.identifiers.items():
                universal_player.device_info.add_identifier(conn_type, value)
            # Update model/manufacturer if universal player has generic values
            self._update_universal_device_info(universal_player, protocol_player)

            # Persist all player data (protocol IDs, identifiers, device info) to config
            for provider in self.mass.get_providers(ProviderType.PLAYER):
                if provider.domain == "universal_player":
                    await cast("UniversalPlayerProvider", provider)._save_player_data(
                        universal_player.player_id, universal_player
                    )
                    break

        protocol_player.update_state()
        universal_player.update_state()

    def _get_device_key_from_players(self, protocol_players: list[Player]) -> str | None:
        """
        Generate a device key from protocol players' identifiers.

        Prefers MAC address (most stable), falls back to UUID, then player_id.
        IP address is not used as it can change with DHCP and cause incorrect matches.
        """
        uuid_key: str | None = None
        for player in protocol_players:
            identifiers = player.device_info.identifiers
            # Prefer MAC address (most reliable)
            if mac := identifiers.get(IdentifierType.MAC_ADDRESS):
                return mac.replace(":", "").replace("-", "").lower()
            # Fall back to UUID (reliable for DLNA, Chromecast)
            if not uuid_key and (uuid := identifiers.get(IdentifierType.UUID)):
                # Normalize UUID: remove special characters, lowercase
                uuid_key = uuid.replace("-", "").replace(":", "").replace("_", "").lower()
        if uuid_key:
            return uuid_key
        # Last resort: use player_id as device key for protocol players without identifiers
        # (e.g., Sendspin players that don't expose IP/MAC)
        if protocol_players:
            return protocol_players[0].player_id.replace(":", "").replace("-", "").lower()
        return None

    def _aggregate_device_info(self, protocol_players: list[Player]) -> DeviceInfo:
        """Aggregate device info from protocol players."""
        first_player = protocol_players[0]
        device_info = DeviceInfo(
            model=first_player.device_info.model,
            manufacturer=first_player.device_info.manufacturer,
        )
        # Merge identifiers from all protocol players
        for player in protocol_players:
            for conn_type, value in player.device_info.identifiers.items():
                device_info.add_identifier(conn_type, value)
        return device_info

    def _update_universal_device_info(
        self, universal_player: UniversalPlayer, protocol_player: Player
    ) -> None:
        """
        Update universal player's device info from protocol player if needed.

        When a universal player is restored from config, it has generic device info
        (model="Universal Player", manufacturer="Music Assistant"). This method
        updates those values from a protocol player that has real device info.
        """
        # Check if universal player has generic device info (from restore)
        device_info = universal_player.device_info
        protocol_info = protocol_player.device_info

        # Update model if universal player has generic value
        if device_info.model in (None, "Universal Player") and protocol_info.model:
            device_info.model = protocol_info.model

        # Update manufacturer if universal player has generic value
        if device_info.manufacturer in (None, "Music Assistant") and protocol_info.manufacturer:
            device_info.manufacturer = protocol_info.manufacturer

    def _save_universal_player_data(self, universal_player: UniversalPlayer) -> None:
        """
        Save universal player data to config via background task.

        This is a helper to persist player data from synchronous code.
        """

        async def _do_save() -> None:
            for provider in self.mass.get_providers(ProviderType.PLAYER):
                if provider.domain == "universal_player":
                    await cast("UniversalPlayerProvider", provider)._save_player_data(
                        universal_player.player_id, universal_player
                    )
                    break

        self.mass.create_task(_do_save())

    def _get_clean_player_name(self, protocol_players: list[Player]) -> str:
        """
        Get the best display name from protocol players.

        Prefers names from protocols that typically provide user-friendly names
        (Chromecast, DLNA, AirPlay) over those that may use technical identifiers
        (Squeezelite, SendSpin). Filters out names that look like MAC addresses,
        UUIDs, or player IDs.
        """
        # Protocol priority for name selection (higher priority = better names typically)
        # Chromecast and DLNA usually have good user-configured names
        # AirPlay also provides sensible names
        # Squeezelite and SendSpin may use MAC addresses or technical IDs
        name_priority = {
            "chromecast": 1,
            "airplay": 2,
            "dlna": 3,
            "squeezelite": 4,
            "sendspin": 5,
        }

        def is_valid_name(name: str) -> bool:
            """Check if a name looks like a real user-friendly name, not a technical ID."""
            if not name or len(name) < 2:
                return False
            name_lower = name.lower().replace(":", "").replace("-", "").replace("_", "")
            # Filter out names that look like MAC addresses (12 hex chars)
            if len(name_lower) == 12 and all(c in "0123456789abcdef" for c in name_lower):
                return False
            # Filter out names that look like UUIDs
            if len(name_lower) >= 32 and all(c in "0123456789abcdef" for c in name_lower[:32]):
                return False
            # Filter out names that start with common player ID prefixes
            return not name_lower.startswith(
                ("ap_", "cc_", "dlna_", "sq_", "sendspin_", "universal_")
            )

        # Sort players by protocol priority, then find the first valid name
        sorted_players = sorted(
            protocol_players,
            key=lambda p: name_priority.get(p.provider.domain, 10),
        )

        for player in sorted_players:
            player_name = player.display_name
            if is_valid_name(player_name):
                return player_name

        # Fallback to first player's name if no valid name found
        return protocol_players[0].display_name

    def _link_protocols_to_universal(
        self, universal_player: Player, protocol_players: list[Player]
    ) -> None:
        """Link protocol players to a universal player, cleaning up existing links."""
        for player in protocol_players:
            # Clean up if linked to another player
            if player._attr_protocol_parent_id:
                if parent := self.get(player._attr_protocol_parent_id):
                    self._remove_protocol_link(parent, player.player_id)
                player._attr_protocol_parent_id = None
            # Link to universal player
            self._add_protocol_link(universal_player, player, player.provider.domain)
            player.update_state()

    async def _create_or_update_universal_player(self, protocol_players: list[Player]) -> None:
        """
        Create or update a UniversalPlayer for a set of protocol players.

        Uses a per-device lock to prevent race conditions when multiple protocols
        for the same device register simultaneously.
        """
        # Get the universal_player provider
        universal_provider: UniversalPlayerProvider | None = None
        for provider in self.mass.get_providers(ProviderType.PLAYER):
            if provider.domain == "universal_player":
                universal_provider = cast("UniversalPlayerProvider", provider)
                break

        if not universal_provider:
            return

        device_key = self._get_device_key_from_players(protocol_players)
        if not device_key:
            return

        universal_player_id = f"{UNIVERSAL_PLAYER_PREFIX}{device_key}"

        # Use a per-device lock to prevent race conditions
        if device_key not in self._universal_player_locks:
            self._universal_player_locks[device_key] = asyncio.Lock()

        async with self._universal_player_locks[device_key]:
            # Re-check - another task may have already handled these players
            protocol_players = [p for p in protocol_players if not p._attr_protocol_parent_id]
            if not protocol_players:
                return

            # Add to existing universal player
            if existing := self.get(universal_player_id):
                for player in protocol_players:
                    if not player._attr_protocol_parent_id:
                        self._add_protocol_link(existing, player, player.provider.domain)
                        if isinstance(existing, UniversalPlayer):
                            await universal_provider.add_protocol_to_universal_player(
                                universal_player_id, player.player_id
                            )
                        player.update_state()
                existing.update_state()
                return

            # Create new universal player
            device_info = self._aggregate_device_info(protocol_players)
            name = self._get_clean_player_name(protocol_players)
            protocol_player_ids = [p.player_id for p in protocol_players]

            universal_player = await universal_provider.create_universal_player(
                device_key=device_key,
                name=name,
                device_info=device_info,
                protocol_player_ids=protocol_player_ids,
            )

            self._link_protocols_to_universal(universal_player, protocol_players)
            universal_player.update_state()

    def _try_link_protocols_to_native(self, native_player: Player) -> None:
        """Try to link protocol players to a native player."""
        # First, check if there's a universal player for this device that should be replaced
        self._check_replace_universal_player(native_player)

        # Look for protocol players that should be linked
        for protocol_player in self.all(return_protocol_players=True):
            if protocol_player.type != PlayerType.PROTOCOL:
                continue
            if protocol_player._attr_protocol_parent_id:
                # Already linked to a parent (could be this native player after replacement)
                continue

            protocol_domain = protocol_player.provider.domain
            if self._identifiers_match(native_player, protocol_player, protocol_domain):
                self._add_protocol_link(native_player, protocol_player, protocol_domain)
                protocol_player.update_state()
                native_player.update_state()

    def _check_replace_universal_player(self, native_player: Player) -> None:
        """Check if a universal player should be replaced by this native player."""
        # Skip if native_player is itself a universal player (prevent self-replacement)
        if native_player.provider.domain == "universal_player":
            return

        # Look for universal players that match this native player
        for player in list(self._players.values()):
            if player.provider.domain != "universal_player":
                continue
            if not self._identifiers_match(native_player, player, ""):
                continue

            # Transfer all protocol links from universal player to native player
            for linked in list(player._attr_linked_protocols):
                if protocol_player := self.get(linked.output_protocol_id):
                    protocol_player._attr_protocol_parent_id = None
                    domain = linked.protocol_domain or protocol_player.provider.domain
                    self._add_protocol_link(native_player, protocol_player, domain)
                    protocol_player.update_state()

            player._attr_linked_protocols.clear()
            native_player.update_state()

            # Remove the now-obsolete universal player
            self.mass.create_task(self.unregister(player.player_id, permanent=True))

    def _add_protocol_link(
        self, native_player: Player, protocol_player: Player, protocol_domain: str
    ) -> None:
        """Add a protocol link from native player to protocol player."""
        # Remove any existing link for the same protocol domain
        native_player._attr_linked_protocols = [
            link
            for link in native_player._attr_linked_protocols
            if link.protocol_domain != protocol_domain
        ]

        # Get priority for this protocol
        priority = PROTOCOL_PRIORITY.get(protocol_domain, 100)

        # Add the new link
        native_player._attr_linked_protocols.append(
            OutputProtocol(
                output_protocol_id=protocol_player.player_id,
                name=protocol_player.provider.name,
                protocol_domain=protocol_domain,
                priority=priority,
            )
        )

        # Set protocol player's parent
        protocol_player._attr_protocol_parent_id = native_player.player_id

        # Persist linked protocol IDs to config for fast restart
        # (only for non-universal players, as universal players handle this themselves)
        if native_player.provider.domain != "universal_player":
            self._save_linked_protocol_ids(native_player)
            # Also save the parent ID on the protocol player for reverse lookup on restart
            self._save_protocol_parent_id(protocol_player.player_id, native_player.player_id)

    def _remove_protocol_link(
        self, native_player: Player, protocol_player_id: str, permanent: bool = False
    ) -> None:
        """
        Remove a protocol link.

        :param native_player: The parent player to remove the link from.
        :param protocol_player_id: The protocol player ID to unlink.
        :param permanent: If True, also removes the protocol ID from the cached list.
            Use this when the protocol player config is being deleted. If False,
            the protocol ID remains in the cache so it can be shown as disabled
            and re-enabled later.
        """
        native_player._attr_linked_protocols = [
            link
            for link in native_player._attr_linked_protocols
            if link.output_protocol_id != protocol_player_id
        ]

        # Clear parent reference on protocol player if it still exists
        if protocol_player := self.get(protocol_player_id):
            if protocol_player._attr_protocol_parent_id == native_player.player_id:
                protocol_player._attr_protocol_parent_id = None

        # Update persisted linked protocol IDs and clear cached parent
        if native_player.provider.domain != "universal_player":
            if permanent:
                # Permanently remove from cache (player config is being deleted)
                self._remove_protocol_id_from_cache(native_player.player_id, protocol_player_id)
            # Note: we don't call _save_linked_protocol_ids here anymore for non-permanent
            # removals because the merge approach will preserve the ID in the cache
            self._clear_protocol_parent_id(protocol_player_id)

    def _save_linked_protocol_ids(self, native_player: Player) -> None:
        """
        Save linked protocol IDs to config for persistence across restarts.

        This method merges active protocol IDs with existing cached IDs to preserve
        disabled protocol players in the cache. This allows disabled protocols to be
        shown in the UI so they can be re-enabled.
        """
        conf_key = (
            f"{CONF_PLAYERS}/{native_player.player_id}/values/{CONF_LINKED_PROTOCOL_PLAYER_IDS}"
        )
        # Get existing cached IDs to preserve disabled protocols
        existing_ids: list[str] = self.mass.config.get(conf_key, [])
        # Get currently active protocol IDs
        active_ids = {link.output_protocol_id for link in native_player._attr_linked_protocols}
        # Merge: keep existing IDs and add any new active ones
        merged_ids = list(existing_ids)
        for protocol_id in active_ids:
            if protocol_id not in merged_ids:
                merged_ids.append(protocol_id)
        self.mass.config.set(conf_key, merged_ids)

    def _get_cached_protocol_ids(self, player_id: str) -> list[str]:
        """Get cached linked protocol IDs from config."""
        conf_key = f"{CONF_PLAYERS}/{player_id}/values/{CONF_LINKED_PROTOCOL_PLAYER_IDS}"
        result = self.mass.config.get(conf_key, [])
        return list(result) if result else []

    def _remove_protocol_id_from_cache(
        self, parent_player_id: str, protocol_player_id: str
    ) -> None:
        """
        Permanently remove a protocol player ID from the cached linked protocol IDs.

        Use this when a protocol player config is being deleted, not just disabled.
        """
        conf_key = f"{CONF_PLAYERS}/{parent_player_id}/values/{CONF_LINKED_PROTOCOL_PLAYER_IDS}"
        cached_ids: list[str] = self.mass.config.get(conf_key, [])
        if protocol_player_id in cached_ids:
            cached_ids.remove(protocol_player_id)
            self.mass.config.set(conf_key, cached_ids)

    def _save_protocol_parent_id(self, protocol_player_id: str, parent_id: str) -> None:
        """Save the parent ID for a protocol player for persistence across restarts."""
        conf_key = f"{CONF_PLAYERS}/{protocol_player_id}/values/{CONF_PROTOCOL_PARENT_ID}"
        self.mass.config.set(conf_key, parent_id)

    def _get_cached_protocol_parent_id(self, protocol_player_id: str) -> str | None:
        """Get cached parent ID for a protocol player from config."""
        conf_key = f"{CONF_PLAYERS}/{protocol_player_id}/values/{CONF_PROTOCOL_PARENT_ID}"
        result = self.mass.config.get(conf_key, None)
        return str(result) if result else None

    def _clear_protocol_parent_id(self, protocol_player_id: str) -> None:
        """Clear the cached parent ID for a protocol player."""
        conf_key = f"{CONF_PLAYERS}/{protocol_player_id}/values/{CONF_PROTOCOL_PARENT_ID}"
        self.mass.config.set(conf_key, None)

    def _cleanup_protocol_links(self, player: Player) -> None:
        """Clean up protocol links when a player is permanently removed."""
        if player.type == PlayerType.PROTOCOL:
            # Protocol player being removed: remove link from parent
            if parent_id := player._attr_protocol_parent_id:
                if parent_player := self.get(parent_id):
                    # Use permanent=True to also remove from cached protocol IDs
                    self._remove_protocol_link(parent_player, player.player_id, permanent=True)
                    if (
                        parent_player.provider.domain == "universal_player"
                        and len(parent_player._attr_linked_protocols) == 0
                    ):
                        # No protocols left - remove universal player
                        self.logger.info(
                            "Universal player %s has no protocols left, removing",
                            parent_id,
                        )
                        self.mass.create_task(
                            self.mass.players.unregister(parent_id, permanent=True)
                        )
                    else:
                        parent_player.update_state()
        else:
            # Native player being removed: schedule protocol evaluation for linked protocols
            # so they can be assigned to a universal player
            for linked in player._attr_linked_protocols:
                if protocol_player := self.get(linked.output_protocol_id):
                    protocol_player._attr_protocol_parent_id = None
                    protocol_player.update_state()
                    self.logger.debug(
                        "Native player %s removed - scheduling evaluation for %s",
                        player.player_id,
                        protocol_player.player_id,
                    )
                    self._schedule_protocol_evaluation(protocol_player)

    def _identifiers_match(
        self, player_a: Player, player_b: Player, protocol_domain: str = ""
    ) -> bool:
        """
        Check if identifiers match between two players.

        Matching is done by comparing connection identifiers (MAC, serial, UUID).
        IP address is used as a fallback for protocol players only, because some
        devices report different virtual MAC addresses per protocol (e.g., DLNA vs
        AirPlay vs Chromecast may all have different MACs for the same device).
        """
        identifiers_a = player_a.device_info.identifiers
        identifiers_b = player_b.device_info.identifiers

        # Check identifiers in order of reliability
        # MAC_ADDRESS > SERIAL_NUMBER > UUID
        for conn_type in (
            IdentifierType.MAC_ADDRESS,
            IdentifierType.SERIAL_NUMBER,
            IdentifierType.UUID,
        ):
            val_a = identifiers_a.get(conn_type)
            val_b = identifiers_b.get(conn_type)

            if not val_a or not val_b:
                continue

            # Normalize values for comparison
            val_a_norm = val_a.lower().replace(":", "").replace("-", "")
            val_b_norm = val_b.lower().replace(":", "").replace("-", "")

            # Direct match
            if val_a_norm == val_b_norm:
                return True

            # Special case: Sonos UUID matching with DLNA _MR suffix
            # Sonos uses RINCON_xxx, DLNA uses RINCON_xxx_MR for Media Renderer
            if conn_type == IdentifierType.UUID:
                if val_b_norm.endswith("_mr") and val_b_norm[:-3] == val_a_norm:
                    return True
                if val_a_norm.endswith("_mr") and val_a_norm[:-3] == val_b_norm:
                    return True

        # Fallback: IP address matching for protocol players only
        # Some devices report different virtual MAC addresses per protocol,
        # but the IP address remains the same. Only use this for protocol-to-protocol
        # or protocol-to-universal matching to avoid false positives.
        if self._can_use_ip_matching(player_a, player_b):
            ip_a = identifiers_a.get(IdentifierType.IP_ADDRESS)
            ip_b = identifiers_b.get(IdentifierType.IP_ADDRESS)
            if ip_a and ip_b and ip_a == ip_b:
                return True

        return False

    def _can_use_ip_matching(self, player_a: Player, player_b: Player) -> bool:
        """
        Check if IP address matching can be used between two players.

        IP matching is only allowed when at least one player is a protocol player
        or universal player, to avoid false positives between unrelated devices.
        """
        # Check if at least one is a protocol player or universal player
        a_is_protocol = (
            player_a.type == PlayerType.PROTOCOL or player_a.provider.domain == "universal_player"
        )
        b_is_protocol = (
            player_b.type == PlayerType.PROTOCOL or player_b.provider.domain == "universal_player"
        )
        return a_is_protocol or b_is_protocol

    def _select_best_output_protocol(self, player: Player) -> tuple[Player, str]:
        """
        Select the best available output protocol for a player.

        Selection priority:
        1. Output protocol that is currently grouped/synced with other players.
        2. User's preferred output protocol (from player settings).
        3. Native playback (if player supports PLAY_MEDIA).
        4. Best available protocol by priority.
        """
        # 1. Check if any output protocol is currently grouped/synced
        for linked in player._attr_linked_protocols:
            if protocol_player := self.get(linked.output_protocol_id):
                if protocol_player.available and self._is_protocol_grouped(protocol_player):
                    return protocol_player, linked.output_protocol_id

        # 2. Check for user's preferred output protocol
        preferred = self.mass.config.get_raw_player_config_value(
            player.player_id, CONF_PREFERRED_OUTPUT_PROTOCOL, "auto"
        )
        if preferred and preferred != "auto":
            # User has a specific preference
            if preferred == "native":
                if PlayerFeature.PLAY_MEDIA in player.supported_features:
                    return player, "native"
            else:
                # Preferred is a protocol player_id
                for linked in player._attr_linked_protocols:
                    if linked.output_protocol_id == preferred:
                        if protocol_player := self.get(linked.output_protocol_id):
                            if protocol_player.available:
                                return protocol_player, linked.output_protocol_id
                        break

        # 3. Use native playback if available
        if PlayerFeature.PLAY_MEDIA in player.supported_features:
            return player, "native"

        # 4. Fall back to best protocol by priority
        for linked in sorted(player._attr_linked_protocols, key=lambda x: x.priority):
            if protocol_player := self.get(linked.output_protocol_id):
                if protocol_player.available:
                    return protocol_player, linked.output_protocol_id

        raise PlayerCommandFailed(f"Player {player.display_name} has no available output protocols")

    def _is_protocol_grouped(self, protocol_player: Player) -> bool:
        """
        Check if a protocol player is currently grouped/synced with other players.

        Used to prefer protocols that are actively participating in a group,
        ensuring consistent playback across grouped players.
        """
        # Check if this protocol player is synced to another player
        if protocol_player.synced_to:
            return True

        # Check if this protocol player has other players synced to it
        if protocol_player.group_members and len(protocol_player.group_members) > 1:
            return True

        # Check if there's an active group involving this player
        return bool(protocol_player.active_group)
