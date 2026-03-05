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

from music_assistant_models.enums import (
    IdentifierType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
    ProviderType,
)
from music_assistant_models.errors import PlayerCommandFailed
from music_assistant_models.player import OutputProtocol

from music_assistant.constants import (
    CONF_LINKED_PROTOCOL_IDS,
    CONF_PLAYERS,
    CONF_PREFERRED_OUTPUT_PROTOCOL,
    CONF_PROTOCOL_PARENT_ID,
    PROTOCOL_PRIORITY,
    VERBOSE_LOG_LEVEL,
)
from music_assistant.helpers.util import (
    is_locally_administered_mac,
    is_valid_mac_address,
    normalize_mac_for_matching,
)
from music_assistant.models.player import Player
from music_assistant.providers.universal_player import UniversalPlayer, UniversalPlayerProvider

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
        _delayed_evaluation_lock: asyncio.Lock
        logger: logging.Logger

        def all_players(  # noqa: D102
            self,
            return_unavailable: bool = True,
            return_disabled: bool = False,
            provider_filter: str | None = None,
            return_protocol_players: bool = False,
        ) -> list[Player]: ...

        def get_player(self, player_id: str) -> Player | None: ...  # noqa: D102

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
        return player.state.type == PlayerType.PROTOCOL

    def _evaluate_protocol_links(self, player: Player) -> None:
        """
        Evaluate and establish protocol links for a player.

        Called when a player is registered to:
        1. If it's from a protocol provider - try to link to a native player.
        2. If it's a native player - try to link any existing protocol players.
        """
        if player.state.type == PlayerType.PROTOCOL:
            # Protocol player: try to find a native parent
            self._try_link_protocol_to_native(player)
        elif player.state.type in (PlayerType.GROUP, PlayerType.STEREO_PAIR):
            return
        else:
            # Native player: try to find protocol players to link
            self._try_link_protocols_to_native(player)

    def _try_link_protocol_to_native(self, protocol_player: Player) -> None:
        """Try to link a protocol player to a native player."""
        protocol_domain = protocol_player.provider.domain

        # Check for cached parent_id from previous session and restore link immediately
        cached_parent_id = self._get_cached_protocol_parent_id(protocol_player.player_id)
        if cached_parent_id:
            result = self._try_restore_cached_parent(
                protocol_player, cached_parent_id, protocol_domain
            )
            if result:
                return
            # Link was refused or parent has active domain - fall through to search

        # Look for a matching native player
        if self._try_link_to_existing_player(protocol_player, protocol_domain):
            return

        # No native player found - schedule delayed evaluation to allow other protocols to register
        if not protocol_player.protocol_parent_id:
            self._schedule_protocol_evaluation(protocol_player)

    def _try_restore_cached_parent(
        self, protocol_player: Player, cached_parent_id: str, protocol_domain: str
    ) -> bool:
        """
        Try to restore a cached parent link from a previous session.

        :param protocol_player: The protocol player to link.
        :param cached_parent_id: The cached parent player ID.
        :param protocol_domain: The protocol domain (e.g., "airplay").
        :return: True if handled (linked or waiting), False if should fall through.
        """
        if parent_player := self.get_player(cached_parent_id):
            if parent_player.state.type in (PlayerType.GROUP, PlayerType.STEREO_PAIR):
                self._clear_protocol_parent_id(protocol_player.player_id)
                return False
            already_linked = any(
                link.output_protocol_id == protocol_player.player_id
                for link in parent_player.linked_output_protocols
            )
            if already_linked:
                # Already linked from a previous call - just restore parent and identifiers
                protocol_player.set_protocol_parent_id(cached_parent_id)
            else:
                # Try to add the link (may be refused if domain already has active link)
                self._add_protocol_link(parent_player, protocol_player, protocol_domain)
            if protocol_player.protocol_parent_id:
                protocol_player.update_state()
                parent_player.update_state()
                # Copy identifiers from protocol player to universal player on restore.
                # Restored universal players start with empty identifiers which must be
                # repopulated from their protocol players so that new protocol players
                # (like Sendspin bridges) can match via identifiers.
                if parent_player.provider.domain == "universal_player" and isinstance(
                    parent_player, UniversalPlayer
                ):
                    for conn_type, value in protocol_player.device_info.identifiers.items():
                        parent_player.device_info.add_identifier(conn_type, value)
                    self._update_universal_device_info(parent_player, protocol_player)
                    # Check if this universal player should now be merged with another
                    # (e.g., DLNA brought a MAC via ARP that matches an AirPlay universal)
                    self._check_merge_universal_players(parent_player)
                return True
            # Link was refused (domain already active on parent) - fall through
            return False

        # Parent not registered yet - set parent and skip evaluation
        protocol_player.set_protocol_parent_id(cached_parent_id)
        return True

    def _try_link_to_existing_player(self, protocol_player: Player, protocol_domain: str) -> bool:
        """
        Try to link a protocol player to an existing native or universal player.

        :param protocol_player: The protocol player to link.
        :param protocol_domain: The protocol domain (e.g., "airplay").
        :return: True if linked successfully, False if no match found.
        """
        # Protocol players should only link to:
        # 1. True native players (Sonos, etc.)
        # 2. Universal players
        # NOT to other protocol players (they get merged via universal_player)
        for native_player in self.all_players(return_protocol_players=False):
            if native_player.player_id == protocol_player.player_id:
                continue
            if native_player.state.type in (
                PlayerType.PROTOCOL,
                PlayerType.GROUP,
                PlayerType.STEREO_PAIR,
            ):
                continue

            # For universal players, check if this protocol player is in its stored list
            # or if identifiers match (for new protocol players like Sendspin bridges
            # that weren't previously known to the Universal Player)
            if native_player.provider.domain == "universal_player":
                if isinstance(native_player, UniversalPlayer):
                    is_known = protocol_player.player_id in native_player._protocol_player_ids
                    is_match = not is_known and self._identifiers_match(
                        native_player, protocol_player, protocol_domain
                    )
                    if is_known or is_match:
                        self._add_protocol_link(native_player, protocol_player, protocol_domain)
                        # Check if linking actually succeeded (may be refused for
                        # duplicate domain)
                        if not protocol_player.protocol_parent_id:
                            continue
                        # Copy identifiers from protocol player to universal player
                        # This is important for restored universal players which start
                        # with empty identifiers
                        for conn_type, value in protocol_player.device_info.identifiers.items():
                            native_player.device_info.add_identifier(conn_type, value)
                        # Update model/manufacturer if universal player has generic values
                        self._update_universal_device_info(native_player, protocol_player)
                        # Register newly matched protocol player with the universal player
                        if is_match:
                            native_player.add_protocol_player(protocol_player.player_id)
                        # Persist updated data to config (async via task)
                        self._save_universal_player_data(native_player)
                        # Check if this universal player should now be merged with another
                        self._check_merge_universal_players(native_player)
                        protocol_player.update_state()
                        native_player.update_state()
                        return True
                continue

            # Check cached protocol IDs first for fast matching on restart
            cached_ids = self._get_cached_protocol_ids(native_player.player_id)
            if protocol_player.player_id in cached_ids:
                self._add_protocol_link(native_player, protocol_player, protocol_domain)
                if protocol_player.protocol_parent_id:
                    protocol_player.update_state()
                    native_player.update_state()
                    return True
                # Link refused (domain duplicate) - try next native player
                continue

            # Fallback to identifier matching
            if self._identifiers_match(native_player, protocol_player, protocol_domain):
                self._add_protocol_link(native_player, protocol_player, protocol_domain)
                if protocol_player.protocol_parent_id:
                    protocol_player.update_state()
                    native_player.update_state()
                    return True
                # Link refused (domain duplicate) - try next native player
                continue

            # Final fallback: check if any already-linked protocol player on this native
            # player shares identifiers with the new protocol player ("sibling matching").
            # This handles native players (e.g., HEOS) that don't have their own MAC/serial
            # identifiers but have protocol players (e.g., AirPlay) from the same device
            # that do share identifiers with the new protocol player (e.g., Sendspin bridge).
            if self._match_via_linked_protocols(native_player, protocol_player, protocol_domain):
                return True

        return False

    def _match_via_linked_protocols(
        self,
        native_player: Player,
        protocol_player: Player,
        protocol_domain: str,
    ) -> bool:
        """
        Try to match a protocol player to a native player via sibling protocol identifiers.

        Check if any of the native player's already-linked protocol players share
        identifiers with the new protocol player. This handles native players that lack
        their own device identifiers but have sibling protocols from the same physical device.

        :param native_player: The native player to potentially link to.
        :param protocol_player: The new protocol player to link.
        :param protocol_domain: The protocol domain of the new player.
        :return: True if linked successfully, False if no match found.
        """
        for linked in native_player.linked_output_protocols:
            linked_player = self.get_player(linked.output_protocol_id)
            if not linked_player:
                continue
            if self._identifiers_match(linked_player, protocol_player, protocol_domain):
                self._add_protocol_link(native_player, protocol_player, protocol_domain)
                if protocol_player.protocol_parent_id:
                    protocol_player.update_state()
                    native_player.update_state()
                    return True
                # Link refused (domain duplicate) - stop checking siblings
                break
        return False

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
        if cached_parent_id and not self.get_player(cached_parent_id):
            # Previously linked to a native player that hasn't registered yet
            # Use longer delay to give native providers time to start up
            delay = 45.0
            self.logger.debug(
                "Protocol player %s waiting for cached parent %s (45s delay)",
                player_id,
                cached_parent_id,
            )
        else:
            # Standard delay for protocol player discovery
            # Allows time for other protocols and native players to register
            delay = 15.0

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

        Uses a shared lock to serialize evaluations - multiple protocol players from the
        same device may trigger concurrent evaluations that would otherwise race each other.
        """
        self._pending_protocol_evaluations.pop(player_id, None)

        async with self._delayed_evaluation_lock:
            protocol_player = self.get_player(player_id)
            if not protocol_player or protocol_player.protocol_parent_id:
                return

            protocol_domain = protocol_player.provider.domain

            # Re-try linking to an existing native/universal player
            if self._try_link_to_existing_player(protocol_player, protocol_domain):
                return

            # Check if there's an existing universal player we should join
            if existing_universal := self._find_matching_universal_player(protocol_player):
                await self._add_protocol_to_existing_universal(
                    existing_universal, protocol_player, protocol_domain
                )
                if protocol_player.protocol_parent_id is not None:
                    return
                # Link refused (domain duplicate) - fall through to create separate UP

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
        protocol_domain = protocol_player.provider.domain

        for other_player in self.all_players(return_protocol_players=True):
            if other_player.player_id == protocol_player.player_id:
                continue
            if other_player.state.type != PlayerType.PROTOCOL:
                continue
            if other_player.protocol_parent_id:
                continue
            # Skip players from the same protocol domain
            # Multiple instances of the same protocol on one host are separate players
            if other_player.provider.domain == protocol_domain:
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
        # Refuse if the universal player already has a registered player from this domain.
        # This prevents a second instance (e.g., two snapcast players on the same host)
        # from replacing the first. The caller falls through to create a separate UP.
        for link in universal_player.linked_output_protocols:
            if link.protocol_domain == protocol_domain and self.get_player(link.output_protocol_id):
                return

        self._add_protocol_link(universal_player, protocol_player, protocol_domain)

        # Check if linking actually succeeded (may be refused for duplicate domain)
        if not protocol_player.protocol_parent_id:
            return

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

            # Check if this universal player should now be merged with another
            self._check_merge_universal_players(universal_player)

        protocol_player.update_state()
        universal_player.update_state()

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

    def _check_merge_universal_players(self, universal_player: UniversalPlayer) -> None:
        """
        Check if another universal player should be merged into this one.

        Called after identifiers are copied from a protocol player to a universal player.
        When a protocol player brings new identifiers (e.g., MAC from ARP enrichment),
        the universal player may now match another universal player that was created
        from a different protocol (e.g., DLNA-based universal player now matches
        AirPlay-based universal player because they share the same MAC address).

        The universal player with more protocol links absorbs the other one.
        """
        for player in list(self._players.values()):
            if player.provider.domain != "universal_player":
                continue
            if player.player_id == universal_player.player_id:
                continue
            if not isinstance(player, UniversalPlayer):
                continue

            if not self._identifiers_match(universal_player, player, ""):
                continue

            # Determine which player absorbs the other (more protocols wins)
            keep, remove = (
                (universal_player, player)
                if len(universal_player.linked_output_protocols)
                >= len(player.linked_output_protocols)
                else (player, universal_player)
            )

            self.logger.info(
                "Merging universal player %s into %s (shared identifiers)",
                remove.player_id,
                keep.player_id,
            )

            # Transfer protocol links from the removed player to the keeper
            for linked in list(remove.linked_output_protocols):
                if protocol_player := self.get_player(linked.output_protocol_id):
                    protocol_player.set_protocol_parent_id(None)
                    domain = linked.protocol_domain or protocol_player.provider.domain

                    # Check if keeper already has an active link from this domain
                    if self._parent_has_active_protocol_from_domain(keep, domain):
                        self.logger.debug(
                            "Skipping duplicate %s link during merge: %s",
                            domain,
                            linked.output_protocol_id,
                        )
                        continue

                    self._add_protocol_link(keep, protocol_player, domain)
                    protocol_player.update_state()

            # Merge identifiers
            for conn_type, value in remove.device_info.identifiers.items():
                keep.device_info.add_identifier(conn_type, value)

            # Add protocol player IDs from the removed player
            if isinstance(keep, UniversalPlayer):
                for pid in remove._protocol_player_ids:
                    keep.add_protocol_player(pid)

            remove.set_linked_output_protocols([])
            keep.update_state()

            # Persist updated data and remove the obsolete player
            self._save_universal_player_data(keep)
            self.mass.create_task(self.unregister(remove.player_id, permanent=True))

            # Only merge one at a time (re-evaluation will catch cascading merges)
            break

    def _link_protocols_to_universal(
        self, universal_player: Player, protocol_players: list[Player]
    ) -> None:
        """Link protocol players to a universal player, cleaning up existing links."""
        for player in protocol_players:
            # Clean up if linked to another player
            if player.protocol_parent_id:
                if parent := self.get_player(player.protocol_parent_id):
                    self._remove_protocol_link(parent, player.player_id)
                player.set_protocol_parent_id(None)
            # Link to universal player
            self._add_protocol_link(universal_player, player, player.provider.domain)
            player.update_state()

        # Update availability from protocol players
        universal_player.update_state()

    async def _create_or_update_universal_player(self, protocol_players: list[Player]) -> None:
        """
        Create or update a UniversalPlayer for a set of protocol players.

        Delegates to the universal player provider which handles orchestration,
        locking, and player creation. The controller then links the protocols
        to the universal player.
        """
        # Filter out players that got linked during the async delay
        protocol_players = [p for p in protocol_players if not p.protocol_parent_id]
        if not protocol_players:
            return

        # Get the universal_player provider
        universal_provider: UniversalPlayerProvider | None = None
        for provider in self.mass.get_providers(ProviderType.PLAYER):
            if provider.domain == "universal_player":
                universal_provider = cast("UniversalPlayerProvider", provider)
                break

        if not universal_provider:
            return

        # Delegate to provider - it handles locking, create/update decision, etc.
        universal_player = await universal_provider.ensure_universal_player_for_protocols(
            protocol_players
        )

        if not universal_player:
            return

        # Link the protocols to the universal player (controller manages cross-provider state)
        # Filter out players that were already linked
        # (e.g., via _add_protocol_to_existing_universal)
        unlinked_players = [p for p in protocol_players if not p.protocol_parent_id]

        # Split unlinked players: those that can join the main universal player
        # vs those that got separate universal players (domain-duplicates)
        for player in list(unlinked_players):
            # Check if a separate universal player was created for this player
            fallback_key = player.player_id.replace(":", "").replace("-", "").lower()
            separate_id = f"up{fallback_key}"
            if separate_up := self.get_player(separate_id):
                # Link to the separate universal player instead
                self._add_protocol_link(separate_up, player, player.provider.domain)
                player.update_state()
                separate_up.update_state()
                unlinked_players.remove(player)

        self._link_protocols_to_universal(universal_player, unlinked_players)
        universal_player.update_state()

    def _try_link_protocols_to_native(self, native_player: Player) -> None:
        """Try to link protocol players to a native player."""
        # First, check if there's a universal player for this device that should be replaced
        self._check_replace_universal_player(native_player)

        # Look for protocol players that should be linked
        for protocol_player in self.all_players(return_protocol_players=True):
            if protocol_player.state.type != PlayerType.PROTOCOL:
                continue
            if protocol_player.protocol_parent_id:
                # Already linked to a parent (could be this native player after replacement)
                continue

            protocol_domain = protocol_player.provider.domain

            # Skip if this native player already has an active link from this domain
            # (prevents a second instance of the same protocol from trying to link)
            if self._parent_has_active_protocol_from_domain(native_player, protocol_domain):
                continue

            if self._identifiers_match(native_player, protocol_player, protocol_domain):
                self._add_protocol_link(native_player, protocol_player, protocol_domain)
                # Check if linking succeeded (may be refused for duplicate domain)
                if protocol_player.protocol_parent_id is not None:
                    protocol_player.update_state()
                    native_player.update_state()

        # Proactively recover disabled/missing protocols from config
        # This ensures disabled protocols show up in the UI so they can be re-enabled
        self._recover_cached_protocol_links(native_player)

        # Second pass: match remaining unlinked protocol players via sibling identifiers.
        # After cache recovery, the native player has linked protocols (e.g., AirPlay)
        # whose identifiers can be used to match new protocol players (e.g., Sendspin bridge)
        # that share the same device identifiers but couldn't match the native player directly.
        for protocol_player in self.all_players(return_protocol_players=True):
            if protocol_player.state.type != PlayerType.PROTOCOL:
                continue
            if protocol_player.protocol_parent_id:
                continue
            protocol_domain = protocol_player.provider.domain
            if self._parent_has_active_protocol_from_domain(native_player, protocol_domain):
                continue
            if self._match_via_linked_protocols(native_player, protocol_player, protocol_domain):
                self.logger.debug(
                    "Linked %s to %s via sibling protocol identifiers",
                    protocol_player.player_id,
                    native_player.player_id,
                )

    def _check_replace_universal_player(self, native_player: Player) -> None:
        """Check if a universal player should be replaced by this native player."""
        # Skip if native_player is itself a universal player (prevent self-replacement)
        if native_player.provider.domain == "universal_player":
            return

        # Look for universal players that match this native player
        for player in list(self._players.values()):
            if player.provider.domain != "universal_player":
                continue

            # Check by identifiers first
            identifiers_match = self._identifiers_match(native_player, player, "")

            # Also check if native player's ID is in the universal player's stored protocol list
            # This handles players that changed type (e.g., sendspin web players changed from
            # PROTOCOL to PLAYER type) and have no identifiers to match against
            player_id_in_protocols = (
                isinstance(player, UniversalPlayer)
                and native_player.player_id in player._protocol_player_ids
            )

            if not identifiers_match and not player_id_in_protocols:
                continue

            # Transfer all protocol links from universal player to native player
            for linked in list(player.linked_output_protocols):
                if protocol_player := self.get_player(linked.output_protocol_id):
                    protocol_player.set_protocol_parent_id(None)
                    domain = linked.protocol_domain or protocol_player.provider.domain
                    self._add_protocol_link(native_player, protocol_player, domain)
                    protocol_player.update_state()

            player.set_linked_output_protocols([])
            native_player.update_state()

            # Remove the now-obsolete universal player
            self.mass.create_task(self.unregister(player.player_id, permanent=True))

    def _parent_has_active_protocol_from_domain(
        self, parent: Player, domain: str, exclude_player_id: str | None = None
    ) -> bool:
        """
        Check if a parent already has an active (registered) protocol player from a given domain.

        This prevents a second protocol player of the same domain (e.g., a second AirPlay
        instance on the same host) from replacing the first one's link on the same parent.

        :param parent: The parent player to check.
        :param domain: The protocol domain to check for (e.g., "airplay", "dlna").
        :param exclude_player_id: Optional player ID to exclude from the check
            (used when checking if a player's own domain is already linked).
        """
        for link in parent.linked_output_protocols:
            if link.protocol_domain != domain:
                continue
            if exclude_player_id and link.output_protocol_id == exclude_player_id:
                continue
            # A registered player from this domain blocks the link, even if unavailable.
            # Being offline doesn't make it a different device — it's still occupying
            # this domain slot. The provider should remove stale players explicitly.
            if self.get_player(link.output_protocol_id):
                return True
        return False

    def _add_protocol_link(
        self, native_player: Player, protocol_player: Player, protocol_domain: str
    ) -> None:
        """Add a protocol link from native player to protocol player."""
        # Guard: refuse to replace an existing active link from the same domain.
        # This prevents a second instance of the same protocol (e.g., two AirPlay
        # instances on the same host) from silently replacing the first one.
        if self._parent_has_active_protocol_from_domain(
            native_player, protocol_domain, exclude_player_id=protocol_player.player_id
        ):
            self.logger.debug(
                "Refusing to link %s to %s: parent already has an active %s link",
                protocol_player.player_id,
                native_player.player_id,
                protocol_domain,
            )
            return

        # Remove any existing link for the same protocol domain
        updated_protocols = [
            link
            for link in native_player.linked_output_protocols
            if link.protocol_domain != protocol_domain
        ]

        # Get priority for this protocol
        priority = PROTOCOL_PRIORITY.get(protocol_domain, 100)

        # Add the new link
        updated_protocols.append(
            OutputProtocol(
                output_protocol_id=protocol_player.player_id,
                name=protocol_player.provider.name,
                protocol_domain=protocol_domain,
                priority=priority,
            )
        )
        native_player.set_linked_output_protocols(updated_protocols)

        # Set protocol player's parent
        protocol_player.set_protocol_parent_id(native_player.player_id)

        # Persist linked protocol IDs to config for fast restart
        # (only for non-universal players, as universal players handle this themselves)
        if native_player.provider.domain != "universal_player":
            self._save_linked_protocol_ids(native_player)
        # Always save the parent ID on the protocol player for reverse lookup on restart
        # (needed for both native and universal parents to enable fast restore)
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
        updated_protocols = [
            link
            for link in native_player.linked_output_protocols
            if link.output_protocol_id != protocol_player_id
        ]
        native_player.set_linked_output_protocols(updated_protocols)

        # Clear parent reference on protocol player if it still exists
        if protocol_player := self.get_player(protocol_player_id):
            if protocol_player.protocol_parent_id == native_player.player_id:
                protocol_player.set_protocol_parent_id(None)

        # Update persisted linked protocol IDs
        if native_player.provider.domain != "universal_player":
            if permanent:
                # Permanently remove from cache (player config is being deleted)
                self._remove_protocol_id_from_cache(native_player.player_id, protocol_player_id)
            # Note: we don't call _save_linked_protocol_ids here anymore for non-permanent
            # removals because the merge approach will preserve the ID in the cache
        # Always clear the cached parent ID (for both native and universal parents)
        self._clear_protocol_parent_id(protocol_player_id)

    def _save_linked_protocol_ids(self, native_player: Player) -> None:
        """
        Save linked protocol IDs to config for persistence across restarts.

        This method merges active protocol IDs with existing cached IDs to preserve
        disabled protocol players in the cache. This allows disabled protocols to be
        shown in the UI so they can be re-enabled.
        """
        conf_key = f"{CONF_PLAYERS}/{native_player.player_id}/values/{CONF_LINKED_PROTOCOL_IDS}"
        # Get existing cached IDs to preserve disabled protocols
        existing_ids: list[str] = self.mass.config.get(conf_key, [])
        # Get currently active protocol IDs
        active_ids = {link.output_protocol_id for link in native_player.linked_output_protocols}
        # Merge: keep existing IDs and add any new active ones
        merged_ids = list(existing_ids)
        for protocol_id in active_ids:
            if protocol_id not in merged_ids:
                merged_ids.append(protocol_id)
        self.mass.config.set(conf_key, merged_ids)

    def _get_cached_protocol_ids(self, player_id: str) -> list[str]:
        """Get cached linked protocol IDs from config."""
        conf_key = f"{CONF_PLAYERS}/{player_id}/values/{CONF_LINKED_PROTOCOL_IDS}"
        result = self.mass.config.get(conf_key, [])
        return list(result) if result else []

    def _remove_protocol_id_from_cache(
        self, parent_player_id: str, protocol_player_id: str
    ) -> None:
        """
        Permanently remove a protocol player ID from the cached linked protocol IDs.

        Use this when a protocol player config is being deleted, not just disabled.
        """
        conf_key = f"{CONF_PLAYERS}/{parent_player_id}/values/{CONF_LINKED_PROTOCOL_IDS}"
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

    def _recover_cached_protocol_links(self, native_player: Player) -> None:
        """
        Recover protocol links from config for disabled/missing protocols.

        This ensures that disabled protocols show up in the output_protocols list
        so they can be re-enabled by the user. It also handles the case where
        protocol players haven't registered yet during startup.
        """
        # Get currently linked protocol IDs
        linked_protocol_ids = {
            link.output_protocol_id for link in native_player.linked_output_protocols
        }

        # Get cached protocol IDs from config (includes protocols that were explicitly linked)
        cached_protocol_ids = self._get_cached_protocol_ids(native_player.player_id)

        # Also check all protocol players that have protocol_parent_id pointing to this player
        # (this handles disabled protocols that may not be in linked_protocol_ids)
        all_player_configs = self.mass.config.get(CONF_PLAYERS, {})
        for protocol_id, protocol_config in all_player_configs.items():
            # Skip if not a protocol player
            if protocol_config.get("player_type") != "protocol":
                continue
            # Check if this protocol has a parent_id pointing to this native player
            protocol_values = protocol_config.get("values", {})
            protocol_parent_id = protocol_values.get(CONF_PROTOCOL_PARENT_ID)
            if protocol_parent_id == native_player.player_id:
                if protocol_id not in cached_protocol_ids:
                    cached_protocol_ids.append(protocol_id)

        if not cached_protocol_ids:
            return

        # Add OutputProtocol entries for any cached protocols that aren't currently linked
        for protocol_id in cached_protocol_ids:
            if protocol_id in linked_protocol_ids:
                continue  # Already linked

            # Get protocol player config to determine the protocol domain and availability
            protocol_config = self.mass.config.get(f"{CONF_PLAYERS}/{protocol_id}")
            if not protocol_config:
                continue

            # Determine protocol domain from provider
            protocol_provider: str = protocol_config.get("provider")
            if not protocol_provider:
                continue

            # Extract domain from provider instance_id (e.g., "airplay--uuid" -> "airplay")
            protocol_domain = protocol_provider.split("--")[0]

            # Skip if parent already has a link from this domain
            existing_domains = {
                link.protocol_domain for link in native_player.linked_output_protocols
            }
            if protocol_domain in existing_domains:
                continue

            # Get provider name for display
            provider_name = protocol_domain.title()  # Default fallback
            if provider := self.mass.get_provider(protocol_domain, return_unavailable=True):
                provider_name = provider.name

            # Get priority for this protocol
            priority = PROTOCOL_PRIORITY.get(protocol_domain, 100)

            # Check if protocol player is available (registered)
            protocol_player = self.get_player(protocol_id)
            is_available = protocol_player is not None and protocol_player.available

            # Add the OutputProtocol entry
            native_player.linked_output_protocols.append(
                OutputProtocol(
                    output_protocol_id=protocol_id,
                    name=provider_name,
                    protocol_domain=protocol_domain,
                    priority=priority,
                    is_native=False,
                    available=is_available,
                )
            )
            self.logger.debug(
                "Recovered cached protocol link %s -> %s (available: %s)",
                native_player.player_id,
                protocol_id,
                is_available,
            )

    def _cleanup_protocol_links(self, player: Player) -> None:
        """Clean up protocol links when a player is permanently removed."""
        if player.state.type == PlayerType.PROTOCOL:
            # Protocol player being removed: remove link from parent
            if parent_id := player.protocol_parent_id:
                if parent_player := self.get_player(parent_id):
                    # Use permanent=True to also remove from cached protocol IDs
                    self._remove_protocol_link(parent_player, player.player_id, permanent=True)
                    if (
                        parent_player.provider.domain == "universal_player"
                        and len(parent_player.linked_output_protocols) == 0
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
            # Native/universal player being removed: handle all linked protocol players.
            # Collect all known protocol IDs from both active links and cached config,
            # since cached config may contain protocols that weren't restored this session.
            all_protocol_ids: set[str] = {
                link.output_protocol_id for link in player.linked_output_protocols
            }
            all_protocol_ids.update(self._get_cached_protocol_ids(player.player_id))
            for protocol_id in all_protocol_ids:
                # Clear cached parent ID in config so protocol won't try to
                # restore a link to the deleted player on next restart
                self._clear_protocol_parent_id(protocol_id)
                if protocol_player := self.get_player(protocol_id):
                    # Protocol player is available: clear parent and schedule re-evaluation
                    # so it can be matched to a new parent or a new universal player
                    protocol_player.set_protocol_parent_id(None)
                    protocol_player.update_state()
                    self.logger.debug(
                        "Player %s removed - scheduling evaluation for protocol %s",
                        player.player_id,
                        protocol_id,
                    )
                    self._schedule_protocol_evaluation(protocol_player)
                else:
                    # Protocol player is not registered yet — it may still be
                    # mid-discovery (e.g., DLNA connecting via SSDP). Don't delete
                    # its config as that would cause a KeyError when it finishes
                    # registering. Stale configs are harmless and get cleaned up
                    # naturally on subsequent restarts.
                    self.logger.debug(
                        "Player %s removed - protocol %s not registered, skipping cleanup",
                        player.player_id,
                        protocol_id,
                    )

    def _identifiers_match(
        self, player_a: Player, player_b: Player, protocol_domain: str = ""
    ) -> bool:
        """
        Check if identifiers match between two players.

        Matching is done by comparing connection identifiers (MAC, serial, UUID).
        As a last resort, IP address is used when at least one player has a
        locally-administered MAC, indicating the device uses MAC randomization
        and ARP could not resolve the real hardware address.

        Invalid identifiers (e.g., 00:00:00:00:00:00 MAC addresses) are filtered out
        to prevent false matches between unrelated devices.
        """
        identifiers_a = player_a.device_info.identifiers
        identifiers_b = player_b.device_info.identifiers

        # Check identifiers in order of reliability
        # MAC_ADDRESS > SERIAL_NUMBER > UUID > CAST_UUID > AIRPLAY_ID
        for conn_type in (
            IdentifierType.MAC_ADDRESS,
            IdentifierType.SERIAL_NUMBER,
            IdentifierType.UUID,
            IdentifierType.CAST_UUID,
            IdentifierType.AIRPLAY_ID,
        ):
            val_a = identifiers_a.get(conn_type)
            val_b = identifiers_b.get(conn_type)

            if not val_a or not val_b:
                continue

            # Filter out invalid MAC addresses (00:00:00:00:00:00, ff:ff:ff:ff:ff:ff)
            if conn_type == IdentifierType.MAC_ADDRESS:
                if not is_valid_mac_address(val_a) or not is_valid_mac_address(val_b):
                    self.logger.log(
                        VERBOSE_LOG_LEVEL,
                        "Skipping invalid MAC address for matching: %s=%s, %s=%s",
                        player_a.display_name,
                        val_a,
                        player_b.display_name,
                        val_b,
                    )
                    continue

            # Normalize values for comparison
            if conn_type == IdentifierType.MAC_ADDRESS:
                # Use MAC normalization that handles locally-administered bit differences
                # Some protocols (like AirPlay) report a locally-administered MAC variant
                # where bit 1 of the first octet is set (e.g., 54:78:... vs 56:78:...)
                val_a_norm = normalize_mac_for_matching(val_a)
                val_b_norm = normalize_mac_for_matching(val_b)

                # Direct match on current MAC
                if val_a_norm == val_b_norm:
                    return True

                # Multi-MAC matching: also check original reported MACs.
                # Devices with multiple interfaces (WiFi + Ethernet) may have ARP
                # resolve one MAC while the protocol reports a different one.
                macs_a = {val_a_norm}
                macs_b = {val_b_norm}
                reported_a = player_a.extra_data.get("reported_mac")
                reported_b = player_b.extra_data.get("reported_mac")
                if reported_a and is_valid_mac_address(reported_a):
                    macs_a.add(normalize_mac_for_matching(reported_a))
                if reported_b and is_valid_mac_address(reported_b):
                    macs_b.add(normalize_mac_for_matching(reported_b))
                if macs_a & macs_b:
                    return True

                # No MAC match - continue to next identifier type
                continue

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

        # Last resort: IP-based matching.
        # Two players on the same IP are very likely the same physical device.
        # This handles two cases:
        # 1. MAC randomization: at least one player has no real MAC (LA or missing),
        #    so ARP couldn't resolve a usable address.
        # 2. Different MACs per protocol: some devices (e.g., Yamaha MusicCast) report
        #    different valid globally-unique MACs per protocol (DLNA vs AirPlay differ
        #    by 1 in the last octet). IP matching is safe here because two different
        #    devices on a LAN cannot share the same IP simultaneously.
        #    To avoid false positives between unrelated native players, this path
        #    requires at least one player to be a protocol or universal player.
        ip_a = identifiers_a.get(IdentifierType.IP_ADDRESS)
        ip_b = identifiers_b.get(IdentifierType.IP_ADDRESS)
        if ip_a and ip_b and ip_a == ip_b:
            mac_a = identifiers_a.get(IdentifierType.MAC_ADDRESS)
            mac_b = identifiers_b.get(IdentifierType.MAC_ADDRESS)
            a_is_real = (
                mac_a is not None
                and is_valid_mac_address(mac_a)
                and not is_locally_administered_mac(mac_a)
            )
            b_is_real = (
                mac_b is not None
                and is_valid_mac_address(mac_b)
                and not is_locally_administered_mac(mac_b)
            )
            # Case 1: at least one player has no real hardware MAC
            if not (a_is_real and b_is_real):
                return True
            # Case 2: both have real MACs but at least one is a protocol/universal player
            a_is_protocol = (
                player_a.type == PlayerType.PROTOCOL
                or player_a.provider.domain == "universal_player"
            )
            b_is_protocol = (
                player_b.type == PlayerType.PROTOCOL
                or player_b.provider.domain == "universal_player"
            )
            if a_is_protocol or b_is_protocol:
                return True

        return False

    def _select_best_output_protocol(self, player: Player) -> tuple[Player, OutputProtocol | None]:
        """
        Select the best available output protocol for a player.

        Selection priority:
        1. Output protocol that is currently grouped/synced with other players.
        2. User's preferred output protocol (from player settings).
        3. Native playback (if player supports PLAY_MEDIA).
        4. Best available protocol by priority.

        Returns tuple of (target_player, output_protocol).
        output_protocol is None when using native playback.
        """
        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "Selecting output protocol for %s",
            player.state.name,
        )

        # 1. Check if any output protocol is currently grouped
        for linked in player.linked_output_protocols:
            if protocol_player := self.get_player(linked.output_protocol_id):
                if protocol_player.available and self._is_protocol_grouped(protocol_player):
                    self.logger.log(
                        VERBOSE_LOG_LEVEL,
                        "Selected protocol for %s: %s (grouped)",
                        player.state.name,
                        protocol_player.state.name,
                    )
                    return protocol_player, linked

        # 2. Check for user's preferred output protocol
        preferred = self.mass.config.get_raw_player_config_value(
            player.player_id, CONF_PREFERRED_OUTPUT_PROTOCOL, "auto"
        )
        if preferred and preferred != "auto":
            if preferred == "native":
                if PlayerFeature.PLAY_MEDIA in player.supported_features:
                    self.logger.log(
                        VERBOSE_LOG_LEVEL,
                        "Selected protocol for %s: native (user preference)",
                        player.state.name,
                    )
                    return player, None
            else:
                for linked in player.linked_output_protocols:
                    if linked.output_protocol_id == preferred:
                        if protocol_player := self.get_player(linked.output_protocol_id):
                            if protocol_player.available:
                                self.logger.log(
                                    VERBOSE_LOG_LEVEL,
                                    "Selected protocol for %s: %s (user preference)",
                                    player.state.name,
                                    protocol_player.state.name,
                                )
                                return protocol_player, linked
                        break

        # 3. Use native playback if available
        if PlayerFeature.PLAY_MEDIA in player.supported_features:
            self.logger.log(
                VERBOSE_LOG_LEVEL, "Selected protocol for %s: native", player.state.name
            )
            return player, None

        # 4. Fall back to best protocol by priority
        for linked in sorted(player.linked_output_protocols, key=lambda x: x.priority):
            if protocol_player := self.get_player(linked.output_protocol_id):
                if protocol_player.available:
                    self.logger.log(
                        VERBOSE_LOG_LEVEL,
                        "Selected protocol for %s: %s (priority-based)",
                        player.state.name,
                        protocol_player.state.name,
                    )
                    return protocol_player, linked

        raise PlayerCommandFailed(f"Player {player.state.name} has no available output protocols")

    def _get_control_target(
        self,
        player: Player,
        required_feature: PlayerFeature,
        require_active: bool = False,
    ) -> Player | None:
        """
        Get the best player(protocol) to send control commands to.

        Prefers the active output protocol, otherwise uses the first available
        protocol player that supports the needed feature.
        """
        # If we have an active protocol, use that
        if (
            player.active_output_protocol
            and player.active_output_protocol != "native"
            and (protocol_player := self.mass.players.get_player(player.active_output_protocol))
            and required_feature in protocol_player.supported_features
        ):
            return protocol_player

        # if the player natively supports the required feature, use that
        if (
            player.active_output_protocol == "native"
            and required_feature in player.supported_features
        ):
            return player

        # If require_active is set, and no active protocol found, return None
        if require_active:
            return None

        # if the player natively supports the required feature, use that
        if required_feature in player.supported_features:
            return player
        # Otherwise, use the first available linked protocol
        for linked in player.linked_output_protocols:
            if (
                (protocol_player := self.mass.players.get_player(linked.output_protocol_id))
                and protocol_player.available
                and required_feature in protocol_player.supported_features
            ):
                return protocol_player

        return None

    def _is_protocol_grouped(self, protocol_player: Player) -> bool:
        """
        Check if a protocol player is currently grouped/synced with other players.

        Used to prefer protocols that are actively participating in a group,
        ensuring consistent playback across grouped players.
        """
        is_grouped = bool(
            protocol_player.state.synced_to
            or (
                protocol_player.state.group_members and len(protocol_player.state.group_members) > 1
            )
            or protocol_player.state.active_group
        )
        if is_grouped:
            self.logger.log(
                VERBOSE_LOG_LEVEL,
                "Protocol player %s is grouped",
                protocol_player.state.name,
            )
        return is_grouped

    def _translate_members_to_remove_for_protocols(
        self,
        parent_player: Player,
        player_ids: list[str],
        parent_protocol_player: Player | None,
        parent_protocol_domain: str | None,
    ) -> tuple[list[str], list[str]]:
        """
        Translate member IDs to remove into protocol and native lists.

        :param parent_player: The parent player to remove members from.
        :param player_ids: List of visible player IDs to remove.
        :param parent_protocol_player: The parent's protocol player if available.
        :param parent_protocol_domain: The parent's protocol domain if available.
        """
        self.logger.debug(
            "Translating members to remove for %s: player_ids=%s, parent_protocol_domain=%s",
            parent_player.state.name,
            player_ids,
            parent_protocol_domain,
        )
        protocol_members: list[str] = []
        native_members: list[str] = []

        for child_player_id in player_ids:
            child_player = self.get_player(child_player_id)
            if not child_player:
                continue

            # Check if this member is in the parent's group via protocol
            if parent_protocol_domain and parent_protocol_player:
                child_protocol = child_player.get_output_protocol_by_domain(parent_protocol_domain)
                if child_protocol and child_protocol.available:
                    # For native protocol players, use the child's player_id directly
                    child_protocol_id = (
                        child_player.player_id
                        if child_protocol.is_native
                        else child_protocol.output_protocol_id
                    )
                    if child_protocol_id in parent_protocol_player.group_members:
                        self.logger.debug(
                            "Translating removal: %s -> protocol %s",
                            child_player_id,
                            child_protocol_id,
                        )
                        protocol_members.append(child_protocol_id)
                        continue

            # Check if child's protocol player is in parent's native group_members
            # This handles native protocol players (e.g., native AirPlay player like Apple TV)
            # where the parent itself contains protocol player IDs in its group_members
            translated = False
            for linked in child_player.linked_output_protocols:
                if linked.output_protocol_id in parent_player.group_members:
                    self.logger.debug(
                        "Translating removal (native parent): %s -> protocol %s",
                        child_player_id,
                        linked.output_protocol_id,
                    )
                    native_members.append(linked.output_protocol_id)
                    translated = True
                    break

            if not translated:
                native_members.append(child_player_id)

        return protocol_members, native_members

    def _filter_protocol_members(self, member_ids: list[str], protocol_player: Player) -> list[str]:
        """Filter member IDs to only include players from the same protocol domain."""
        return [
            pid
            for pid in member_ids
            if (p := self.get_player(pid)) and p.provider.domain == protocol_player.provider.domain
        ]

    def _filter_native_members(self, member_ids: list[str], parent_player: Player) -> list[str]:
        """Filter member IDs to only include players compatible with the parent."""
        return [
            pid
            for pid in member_ids
            if (p := self.get_player(pid))
            and (
                p.provider.instance_id == parent_player.provider.instance_id
                or pid in parent_player._attr_can_group_with
                or p.provider.instance_id in parent_player._attr_can_group_with
            )
        ]

    def _try_child_preferred_protocol(
        self,
        child_player: Player,
        parent_player: Player,
    ) -> tuple[str | None, str | None]:
        """
        Try to use child's preferred output protocol for grouping.

        Returns tuple of (child_protocol_id, protocol_domain) or (None, None).
        """
        child_preferred = self.mass.config.get_raw_player_config_value(
            child_player.player_id, CONF_PREFERRED_OUTPUT_PROTOCOL, "auto"
        )
        if not child_preferred or child_preferred in {"auto", "native"}:
            return None, None

        # Find child's preferred protocol in linked protocols
        child_protocol = None
        for linked in child_player.linked_output_protocols:
            if linked.output_protocol_id == child_preferred:
                child_protocol = linked
                break

        if not child_protocol or not child_protocol.available:
            return None, None

        # Check if parent supports this protocol (including native protocol)
        parent_protocol = parent_player.get_output_protocol_by_domain(
            child_protocol.protocol_domain
        )
        if not parent_protocol or not parent_protocol.available:
            return None, None

        # Check if this protocol supports set_members
        protocol_player = parent_player.get_protocol_player(parent_protocol.output_protocol_id)
        if (
            not protocol_player
            or PlayerFeature.SET_MEMBERS not in protocol_player.state.supported_features
        ):
            return None, None

        return child_protocol.output_protocol_id, child_protocol.protocol_domain

    def _can_use_native_grouping(
        self,
        child_player: Player,
        parent_player: Player,
        parent_supports_native: bool,
    ) -> bool:
        """Check if child can be grouped with parent using native grouping."""
        if not parent_supports_native:
            return False
        return (
            child_player.provider.instance_id == parent_player.provider.instance_id
            or child_player.player_id in parent_player._attr_can_group_with
            or child_player.provider.instance_id in parent_player._attr_can_group_with
        )

    def _try_find_common_protocol(
        self, child_player: Player, parent_player: Player
    ) -> tuple[OutputProtocol | None, OutputProtocol | None]:
        """
        Find common protocol that supports set_members.

        Returns tuple of (parent_protocol, child_protocol) or (None, None).
        """
        for parent_output_protocol in parent_player.output_protocols:
            if not parent_output_protocol.available:
                continue
            child_protocol = child_player.get_output_protocol_by_domain(
                parent_output_protocol.protocol_domain
            )
            if not child_protocol or not child_protocol.available:
                continue
            protocol_player = parent_player.get_protocol_player(
                parent_output_protocol.output_protocol_id
            )
            if (
                protocol_player
                and PlayerFeature.SET_MEMBERS in protocol_player.state.supported_features
            ):
                return parent_output_protocol, child_protocol
        return None, None

    def _translate_members_for_protocols(
        self,
        parent_player: Player,
        player_ids: list[str],
        parent_protocol_player: Player | None,
        parent_protocol_domain: str | None,
    ) -> tuple[list[str], list[str], Player | None, str | None]:
        """
        Translate member IDs to protocol or native IDs.

        Selection priority when grouping:
        1. Try child's preferred output protocol (from player settings)
        2. Try parent's active output protocol (if any and child supports it)
        3. Try native grouping (if parent and child are compatible)
        4. Search for common protocol that supports set_members
        5. Log warning if no option works

        Returns tuple of (protocol_members, native_members, protocol_player, protocol_domain).
        """
        protocol_members: list[str] = []
        native_members: list[str] = []
        parent_supports_native_grouping = (
            PlayerFeature.SET_MEMBERS in parent_player.supported_features
        )

        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "Translating members for %s: parent_supports_native=%s, parent_protocol=%s (%s)",
            parent_player.state.name,
            parent_supports_native_grouping,
            parent_protocol_player.state.name if parent_protocol_player else "none",
            parent_protocol_domain or "none",
        )

        for child_player_id in player_ids:
            child_player = self.get_player(child_player_id)
            if not child_player:
                continue

            self.logger.log(
                VERBOSE_LOG_LEVEL,
                "Processing child %s (type=%s, protocols=%s)",
                child_player.state.name,
                child_player.state.type,
                [p.protocol_domain for p in child_player.output_protocols],
            )

            # Priority 1: Try child's preferred output protocol
            # (only if no active protocol or if it matches the active protocol)
            child_protocol_id, protocol_domain = self._try_child_preferred_protocol(
                child_player, parent_player
            )
            if (
                child_protocol_id
                and protocol_domain
                and (not parent_protocol_domain or protocol_domain == parent_protocol_domain)
            ):
                if not parent_protocol_player or parent_protocol_domain != protocol_domain:
                    parent_protocol = parent_player.get_output_protocol_by_domain(protocol_domain)
                    if parent_protocol:
                        parent_protocol_player = parent_player.get_protocol_player(
                            parent_protocol.output_protocol_id
                        )
                        parent_protocol_domain = protocol_domain
                protocol_members.append(child_protocol_id)
                self.logger.log(
                    VERBOSE_LOG_LEVEL,
                    "Using child's preferred protocol %s for %s",
                    protocol_domain,
                    child_player.state.name,
                )
                continue

            # Priority 2: Try parent's active output protocol (if it supports SET_MEMBERS)
            if parent_protocol_domain and parent_protocol_player:
                # Verify the active protocol supports SET_MEMBERS
                if PlayerFeature.SET_MEMBERS in parent_protocol_player.state.supported_features:
                    child_protocol = child_player.get_output_protocol_by_domain(
                        parent_protocol_domain
                    )
                    if child_protocol and child_protocol.available:
                        # For native protocol players, use the child's player_id directly
                        # (e.g., a native sendspin web player IS the protocol player)
                        child_protocol_id = (
                            child_player.player_id
                            if child_protocol.is_native
                            else child_protocol.output_protocol_id
                        )
                        protocol_members.append(child_protocol_id)
                        self.logger.log(
                            VERBOSE_LOG_LEVEL,
                            "Using parent's active protocol %s for %s",
                            parent_protocol_domain,
                            child_player.state.name,
                        )
                        continue
                else:
                    self.logger.log(
                        VERBOSE_LOG_LEVEL,
                        "Parent's active protocol %s does not support SET_MEMBERS, "
                        "will search for alternative",
                        parent_protocol_domain,
                    )
                    # Clear the parent protocol so Priority 4 can select a new one
                    parent_protocol_player = None
                    parent_protocol_domain = None

            # Priority 3: Try native grouping
            if self._can_use_native_grouping(
                child_player, parent_player, parent_supports_native_grouping
            ):
                native_members.append(child_player_id)
                self.logger.log(
                    VERBOSE_LOG_LEVEL,
                    "Using native grouping for %s",
                    child_player.state.name,
                )
                continue

            # Priority 4: Search for common protocol that supports set_members
            parent_protocol, child_protocol = self._try_find_common_protocol(
                child_player, parent_player
            )
            if parent_protocol and child_protocol:
                if (
                    not parent_protocol_player
                    or parent_protocol_domain != parent_protocol.protocol_domain
                ):
                    parent_protocol_player = parent_player.get_protocol_player(
                        parent_protocol.output_protocol_id
                    )
                    if parent_protocol_player:
                        parent_protocol_domain = parent_protocol_player.provider.domain
                # For native protocol players, use the child's player_id directly
                child_protocol_id = (
                    child_player.player_id
                    if child_protocol.is_native
                    else child_protocol.output_protocol_id
                )
                protocol_members.append(child_protocol_id)
                self.logger.log(
                    VERBOSE_LOG_LEVEL,
                    "Selected common protocol %s for grouping %s with %s",
                    parent_protocol.protocol_domain,
                    child_player.state.name,
                    parent_player.state.name,
                )
                continue

            # Priority 5: No option worked - log warning
            self.logger.warning(
                "Cannot group %s with %s: no compatible grouping method found "
                "(tried: child preferred protocol, native grouping, "
                "parent active protocol, common protocols)",
                child_player.state.name,
                parent_player.state.name,
            )

        return protocol_members, native_members, parent_protocol_player, parent_protocol_domain

    async def _forward_protocol_set_members(
        self,
        parent_player: Player,
        parent_protocol_player: Player,
        protocol_members_to_add: list[str],
        protocol_members_to_remove: list[str],
    ) -> None:
        """
        Forward protocol members to protocol player's set_members and manage active output protocol.

        :param parent_player: The parent player (native/universal).
        :param parent_protocol_player: The protocol player to forward commands to.
        :param protocol_members_to_add: Protocol player IDs to add.
        :param protocol_members_to_remove: Protocol player IDs to remove.
        """
        filtered_protocol_add = self._filter_protocol_members(
            protocol_members_to_add, parent_protocol_player
        )
        filtered_protocol_remove = self._filter_protocol_members(
            protocol_members_to_remove, parent_protocol_player
        )
        self.logger.debug(
            "Protocol grouping on %s: filtered_add=%s, filtered_remove=%s",
            parent_protocol_player.state.name,
            filtered_protocol_add,
            filtered_protocol_remove,
        )

        if not filtered_protocol_add and not filtered_protocol_remove:
            return

        # Safety check: verify protocol player supports SET_MEMBERS
        if PlayerFeature.SET_MEMBERS not in parent_protocol_player.state.supported_features:
            self.logger.error(
                "Protocol player %s does not support SET_MEMBERS, cannot perform grouping. "
                "This should have been caught earlier in the flow.",
                parent_protocol_player.state.name,
            )
            return

        self.logger.debug(
            "Calling set_members on protocol player %s with add=%s, remove=%s",
            parent_protocol_player.state.name,
            filtered_protocol_add,
            filtered_protocol_remove,
        )
        await parent_protocol_player.set_members(
            player_ids_to_add=filtered_protocol_add or None,
            player_ids_to_remove=filtered_protocol_remove or None,
        )

        # Set active output protocol on added child players
        if filtered_protocol_add:
            for child_protocol_id in filtered_protocol_add:
                if child_protocol := self.get_player(child_protocol_id):
                    if child_protocol.protocol_parent_id:
                        if child_player := self.get_player(child_protocol.protocol_parent_id):
                            if child_player.active_output_protocol != child_protocol_id:
                                self.logger.debug(
                                    "Setting active output protocol on child %s to %s",
                                    child_player.state.name,
                                    child_protocol_id,
                                )
                                child_player.set_active_output_protocol(child_protocol_id)

        # If we added members via this protocol, set it as the active output protocol
        # and restart playback if currently playing AND we're switching protocols
        if filtered_protocol_add:
            previous_protocol = parent_player.active_output_protocol
            was_playing = parent_player.state.playback_state == PlaybackState.PLAYING

            # Determine if we're switching protocols (which requires restart)
            # Native protocol: parent_protocol_player is the same as parent_player
            is_native_protocol = parent_protocol_player.player_id == parent_player.player_id
            already_using_native = previous_protocol in (None, "native")
            already_using_this_protocol = previous_protocol == parent_protocol_player.player_id

            # Only restart if we're actually switching to a different protocol
            switching_protocols = not (
                (is_native_protocol and already_using_native) or already_using_this_protocol
            )

            self.logger.debug(
                "Protocol grouping: is_native=%s, already_native=%s, already_this=%s, "
                "switching=%s, was_playing=%s",
                is_native_protocol,
                already_using_native,
                already_using_this_protocol,
                switching_protocols,
                was_playing,
            )

            # Update active output protocol if not already using native
            if not (is_native_protocol and already_using_native):
                parent_player.set_active_output_protocol(parent_protocol_player.player_id)

            # Restart playback only if we're switching protocols
            if was_playing and switching_protocols:
                self.logger.info(
                    "Restarting playback on %s via %s protocol after switching protocols",
                    parent_player.state.name,
                    parent_protocol_player.provider.domain,
                )
                # Use resume to restart from current position
                await self.mass.players._handle_cmd_resume(parent_player.player_id)

        self.logger.debug(
            "After set_members, protocol player %s state: group_members=%s, synced_to=%s",
            parent_protocol_player.state.name,
            parent_protocol_player.group_members,
            parent_protocol_player.synced_to,
        )
