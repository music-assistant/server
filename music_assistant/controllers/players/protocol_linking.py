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
from contextlib import suppress
from copy import deepcopy
from typing import TYPE_CHECKING, cast

from music_assistant_models.enums import (
    EventType,
    IdentifierType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
    ProviderType,
)
from music_assistant_models.errors import PlayerCommandFailed, PlayerUnavailableError

from music_assistant.constants import (
    CONF_CACHED_ARP_MAC,
    CONF_GROUP_MEMBERS,
    CONF_LINKED_PROTOCOL_IDS,
    CONF_PLAYER_DSP,
    CONF_PLAYER_QUEUES,
    CONF_PLAYERS,
    CONF_PREFERRED_OUTPUT_PROTOCOL,
    CONF_PROTOCOL_KEY_SPLITTER,
    CONF_PROTOCOL_PARENT_ID,
    CONF_REPORTED_MAC,
    CONF_UNDERLYING_PLAYER_ID,
    PROTOCOL_PRIORITY,
    VERBOSE_LOG_LEVEL,
)
from music_assistant.helpers.util import (
    is_locally_administered_mac,
    is_valid_mac_address,
    normalize_mac_for_matching,
)
from music_assistant.models.player import LinkedOutputProtocol, Player
from music_assistant.providers.sync_group.constants import CONF_ALLOWED_MEMBERS
from music_assistant.providers.universal_player import UniversalPlayer, UniversalPlayerProvider
from music_assistant.providers.universal_player.constants import (
    CONF_DEVICE_IDENTIFIERS,
    CONF_DEVICE_INFO,
)

if TYPE_CHECKING:
    from collections.abc import Coroutine
    from typing import Any

    from music_assistant_models.player import OutputProtocol

    from music_assistant import MusicAssistant

# Config value keys that are bookkeeping of the universal player wrapper itself
# (protocol links and device-identity caches) and must never be carried over
# when a native player replaces a universal player.
UNIVERSAL_PLAYER_INTERNAL_CONF_KEYS = (
    CONF_LINKED_PROTOCOL_IDS,
    CONF_PROTOCOL_PARENT_ID,
    CONF_UNDERLYING_PLAYER_ID,
    CONF_DEVICE_IDENTIFIERS,
    CONF_DEVICE_INFO,
    CONF_CACHED_ARP_MAC,
    CONF_REPORTED_MAC,
)


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
        elif player.state.type == PlayerType.GROUP:
            return
        else:
            # A player that registers with a non-protocol type can no longer be a
            # protocol child: drop a leftover persisted parent link (e.g. from a
            # bridge client that turned web player) so the startup repair pass
            # doesn't heal its player type back to protocol.
            if self._get_cached_protocol_parent_id(player.player_id):
                self._clear_protocol_parent_id(player.player_id)
            # Native player (including STEREO_PAIR): try to find protocol players to link
            self._try_link_protocols_to_native(player)

    def _try_link_protocol_to_native(self, protocol_player: Player) -> None:
        """Try to link a protocol player to a native player."""
        protocol_domain = protocol_player.provider.domain

        # Derived protocol players (e.g. Sendspin bridges riding on another
        # protocol) resolve strictly via their underlying player - no identifier
        # matching or delayed evaluation. If the underlying player has no parent
        # yet, the link is established by _link_derived_protocols_of as soon as
        # the underlying player gets linked.
        if protocol_player.underlying_player_id:
            self._try_link_derived_protocol(protocol_player)
            return

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
            if parent_player.state.type == PlayerType.GROUP:
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
                protocol_player.refresh_state()
                parent_player.refresh_state()
                # Merge the protocol player's identifiers into the universal player
                # so identifiers discovered since the last persist (e.g. an
                # ARP-resolved MAC) are included and new protocol players
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

        # Parent is not registered yet. Leave the protocol player unparented
        # so the caller schedules delayed evaluation, which can wait for the
        # cached parent without stranding the protocol player on a dangling id.
        return False

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
            if native_player.state.type in (PlayerType.PROTOCOL, PlayerType.GROUP):
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
                        # Merge the protocol player's identifiers into the universal
                        # player so newly discovered identifiers are included as well
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
                        protocol_player.refresh_state()
                        native_player.refresh_state()
                        return True
                continue

            # Check cached protocol IDs first for fast matching on restart
            cached_ids = self._get_cached_protocol_ids(native_player.player_id)
            if protocol_player.player_id in cached_ids:
                self._add_protocol_link(native_player, protocol_player, protocol_domain)
                if protocol_player.protocol_parent_id:
                    protocol_player.refresh_state()
                    native_player.refresh_state()
                    return True
                # Link refused (domain duplicate) - try next native player
                continue

            # Fallback to identifier matching
            if self._identifiers_match(native_player, protocol_player, protocol_domain):
                self._add_protocol_link(native_player, protocol_player, protocol_domain)
                if protocol_player.protocol_parent_id:
                    protocol_player.refresh_state()
                    native_player.refresh_state()
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
                    protocol_player.refresh_state()
                    native_player.refresh_state()
                    return True
                # Link refused (domain duplicate) - stop checking siblings
                break
        return False

    def _try_link_derived_protocol(self, protocol_player: Player) -> bool:
        """
        Link a derived protocol player to the parent of its underlying player.

        Derived protocol players carry an underlying_player_id declared by their
        bridge, which makes the parent resolution deterministic: they always join
        the parent of the player they ride on (or that player itself when it is
        not a protocol player). Callers must ensure the player is not linked yet.

        :param protocol_player: The derived protocol player to link.
        :return: True if the player got linked, False if the underlying player
            (or its parent) is not available yet or the link was refused.
        """
        underlying = self.get_player(protocol_player.underlying_player_id or "")
        if underlying is None:
            return False
        if underlying.state.type == PlayerType.PROTOCOL:
            parent = (
                self.get_player(underlying.protocol_parent_id)
                if underlying.protocol_parent_id
                else None
            )
        else:
            parent = underlying
        if parent is None or parent.state.type == PlayerType.GROUP:
            return False

        self._add_protocol_link(parent, protocol_player, protocol_player.provider.domain)
        if not protocol_player.protocol_parent_id:
            # Link refused (e.g. parent already has an active link from this domain)
            return False

        if parent.provider.domain == "universal_player" and isinstance(parent, UniversalPlayer):
            # Track membership and identifiers on the universal player so the
            # derived protocol restores quickly on the next start.
            parent.add_protocol_player(protocol_player.player_id)
            for conn_type, value in protocol_player.device_info.identifiers.items():
                parent.device_info.add_identifier(conn_type, value)
            self._save_universal_player_data(parent)

        protocol_player.refresh_state()
        parent.refresh_state()
        self.logger.debug(
            "Linked derived protocol %s to %s (via underlying %s)",
            protocol_player.player_id,
            parent.player_id,
            underlying.player_id,
        )
        return True

    def _link_derived_protocols_of(self, underlying_player: Player) -> None:
        """
        Link any waiting derived protocol players that ride on the given player.

        Called after a player is linked to a parent (or registered as a native
        player), so derived protocol players that registered earlier can join
        the same parent.
        """
        for candidate in self.all_players(return_protocol_players=True):
            if candidate.underlying_player_id != underlying_player.player_id:
                continue
            if candidate.protocol_parent_id:
                continue
            self._try_link_derived_protocol(candidate)

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
            if protocol_player.state.type != PlayerType.PROTOCOL:
                # The player changed type while the evaluation was pending
                # (e.g. re-registered as a regular player); it must never be
                # linked as a protocol or wrapped in a universal player.
                return

            # Derived protocol players resolve strictly via their underlying
            # player and never match by identifiers or wrap into universal
            # players; they wait for _link_derived_protocols_of otherwise.
            if protocol_player.underlying_player_id:
                self._try_link_derived_protocol(protocol_player)
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

            # Refuse to create a universal player wrapper when the cached parent
            # config exists but is disabled. The user explicitly turned the
            # parent device off; surfacing its protocols as a separate player
            # would defeat that intent.
            cached_parent_id = self._get_cached_protocol_parent_id(player_id)
            if cached_parent_id:
                parent_raw = self.mass.config.get(f"{CONF_PLAYERS}/{cached_parent_id}")
                if parent_raw and not parent_raw.get("enabled", True):
                    self.logger.debug(
                        "Skipping universal player creation for %s: cached parent %s is disabled",
                        player_id,
                        cached_parent_id,
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
        protocol_domain = protocol_player.provider.domain

        for other_player in self.all_players(return_protocol_players=True):
            if other_player.player_id == protocol_player.player_id:
                continue
            if other_player.state.type != PlayerType.PROTOCOL:
                continue
            if other_player.protocol_parent_id:
                continue
            if other_player.underlying_player_id:
                # Derived protocol players follow their underlying player
                # once it is linked; they never seed a universal player.
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

        protocol_player.refresh_state()
        universal_player.refresh_state()

    def _update_universal_device_info(
        self, universal_player: UniversalPlayer, protocol_player: Player
    ) -> None:
        """
        Update universal player's device info from protocol player if needed.

        A universal player carries generic placeholder device info
        (model="Universal Player", manufacturer="Music Assistant") when no real
        values are known for it (yet). This method updates those values from a
        protocol player that has real device info.
        """
        # Check if universal player has generic placeholder device info
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

    def _get_known_protocol_ids(self, parent: Player) -> list[str]:
        """
        Get all protocol IDs tracked for a parent player.

        Includes both active links and cached/inactive protocol IDs so callers can
        safely migrate or clean up the full parent/protocol relationship.
        """
        result: list[str] = []
        seen: set[str] = set()

        for linked in parent.linked_output_protocols:
            if linked.output_protocol_id not in seen:
                result.append(linked.output_protocol_id)
                seen.add(linked.output_protocol_id)

        if parent.provider.domain == "universal_player" and isinstance(parent, UniversalPlayer):
            cached_ids = parent._protocol_player_ids
        else:
            cached_ids = self._get_cached_protocol_ids(parent.player_id)

        for protocol_id in cached_ids:
            if protocol_id not in seen:
                result.append(protocol_id)
                seen.add(protocol_id)

        return result

    def _migrate_protocol_ids_to_parent(self, parent: Player, protocol_ids: set[str]) -> None:
        """
        Persist protocol ownership on a new parent without requiring active links.

        This is used when protocol ownership moves from one parent to another during
        promotion/merge flows. Active protocols are already linked in memory, but
        disabled or temporarily unavailable protocols still need their cached parent
        relationship moved as well.
        """
        if not protocol_ids:
            return

        if parent.provider.domain == "universal_player" and isinstance(parent, UniversalPlayer):
            for protocol_id in protocol_ids:
                parent.add_protocol_player(protocol_id)
            self._save_universal_player_data(parent)
        else:
            conf_key = f"{CONF_PLAYERS}/{parent.player_id}/values/{CONF_LINKED_PROTOCOL_IDS}"
            cached_ids = self._get_cached_protocol_ids(parent.player_id)
            changed = False
            for protocol_id in protocol_ids:
                if protocol_id not in cached_ids:
                    cached_ids.append(protocol_id)
                    changed = True
            if changed:
                self.mass.config.set(conf_key, cached_ids)

        for protocol_id in protocol_ids:
            if self.mass.config.get(f"{CONF_PLAYERS}/{protocol_id}"):
                self._save_protocol_parent_id(protocol_id, parent.player_id)

    def _remove_protocol_ids_from_parent(self, parent: Player, protocol_ids: set[str]) -> None:
        """
        Remove protocol ownership from a parent before it is permanently cleaned up.

        This prevents `_cleanup_protocol_links` from treating already-migrated
        protocols as orphaned when the obsolete parent is unregistered.
        """
        if not protocol_ids:
            return

        remaining_links = [
            link
            for link in parent.linked_output_protocols
            if link.output_protocol_id not in protocol_ids
        ]
        if len(remaining_links) != len(parent.linked_output_protocols):
            parent.set_linked_output_protocols(remaining_links)

        if parent.provider.domain == "universal_player" and isinstance(parent, UniversalPlayer):
            for protocol_id in protocol_ids:
                parent.remove_protocol_player(protocol_id)
            if self.mass.config.get(f"{CONF_PLAYERS}/{parent.player_id}"):
                self.mass.config.set(
                    f"{CONF_PLAYERS}/{parent.player_id}/values/{CONF_LINKED_PROTOCOL_IDS}",
                    parent._protocol_player_ids,
                )
        else:
            for protocol_id in protocol_ids:
                self._remove_protocol_id_from_cache(parent.player_id, protocol_id)

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

            # Do not merge if both UPs have protocols from the same domain.
            # Multiple instances of the same protocol on one host (e.g., several
            # squeezelite players on the same VM) are separate devices that happen
            # to share an IP. Merging them would orphan one instance's protocol.
            domains_a = {
                link.protocol_domain
                for link in universal_player.linked_output_protocols
                if link.protocol_domain
            }
            domains_b = {
                link.protocol_domain
                for link in player.linked_output_protocols
                if link.protocol_domain
            }
            if domains_a & domains_b:
                self.logger.debug(
                    "Skipping merge of %s and %s: shared protocol domain(s) %s",
                    universal_player.player_id,
                    player.player_id,
                    domains_a & domains_b,
                )
                continue

            # Determine which player absorbs the other (more protocols wins).
            # On ties, fall back to a deterministic tiebreaker on player_id so
            # the merge outcome is stable across server restarts. Without this,
            # which UniversalPlayer "wins" depends on iteration order of
            # self._players.values(), which can shift between runs and causes
            # downstream player_id reshuffling (and broken entity bindings in
            # consumers like the Home Assistant MA integration).
            len_u = len(universal_player.linked_output_protocols)
            len_p = len(player.linked_output_protocols)
            if len_u > len_p:
                keep, remove = universal_player, player
            elif len_u < len_p:
                keep, remove = player, universal_player
            elif universal_player.player_id < player.player_id:
                keep, remove = universal_player, player
            else:
                keep, remove = player, universal_player

            self.logger.info(
                "Merging universal player %s into %s (shared identifiers)",
                remove.player_id,
                keep.player_id,
            )

            known_protocol_ids = set(self._get_known_protocol_ids(remove))
            active_protocol_ids = {
                link.output_protocol_id for link in remove.linked_output_protocols
            }
            moved_protocol_ids: set[str] = set()

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
                    if protocol_player.protocol_parent_id == keep.player_id:
                        moved_protocol_ids.add(protocol_player.player_id)
                        protocol_player.refresh_state()

            # Move cached-only protocol ownership as well so old-parent cleanup
            # does not wipe protocols that were intentionally preserved.
            cached_only_ids = known_protocol_ids - active_protocol_ids
            preserved_protocol_ids = moved_protocol_ids | cached_only_ids
            self._migrate_protocol_ids_to_parent(keep, preserved_protocol_ids)
            self._remove_protocol_ids_from_parent(remove, preserved_protocol_ids)

            # Merge identifiers
            for conn_type, value in remove.device_info.identifiers.items():
                keep.device_info.add_identifier(conn_type, value)
            keep.refresh_state()

            # Persist updated data before the obsolete player is removed
            self._save_universal_player_data(keep)

            # Carry over the user's configuration and re-point group memberships
            # before the permanent removal below deletes the losing wrapper's config
            self._migrate_universal_player_config(remove.player_id, keep.player_id)
            self._repoint_group_memberships(remove.player_id, keep.player_id)

            # Stop playback and remove the obsolete player
            self.mass.create_task(self._stop_and_unregister(remove))

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
            player.refresh_state()

        # Update availability from protocol players
        universal_player.refresh_state()

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
                player.refresh_state()
                separate_up.refresh_state()
                unlinked_players.remove(player)

        self._link_protocols_to_universal(universal_player, unlinked_players)
        universal_player.refresh_state()

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
            if protocol_player.underlying_player_id:
                # Derived protocol players link via their underlying player instead
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
                    protocol_player.refresh_state()
                    native_player.refresh_state()

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
            if protocol_player.underlying_player_id:
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

        # Finally, link derived protocol players that ride directly on this
        # native player (derived players riding on the protocol players linked
        # above are handled by _add_protocol_link itself).
        self._link_derived_protocols_of(native_player)

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

            known_protocol_ids = set(self._get_known_protocol_ids(player))
            active_protocol_ids = {
                link.output_protocol_id for link in player.linked_output_protocols
            }
            moved_protocol_ids: set[str] = set()

            # Transfer all protocol links from universal player to native player
            for linked in list(player.linked_output_protocols):
                if protocol_player := self.get_player(linked.output_protocol_id):
                    protocol_player.set_protocol_parent_id(None)
                    domain = linked.protocol_domain or protocol_player.provider.domain
                    self._add_protocol_link(native_player, protocol_player, domain)
                    if protocol_player.protocol_parent_id == native_player.player_id:
                        moved_protocol_ids.add(protocol_player.player_id)
                        protocol_player.refresh_state()
                    else:
                        # Link refused, keep the protocol owned by the universal player.
                        protocol_player.set_protocol_parent_id(player.player_id)

            if active_protocol_ids - moved_protocol_ids:
                # A link was refused, keep the universal player and hand over only
                # what moved so the refused protocols are not orphaned.
                self._migrate_protocol_ids_to_parent(native_player, moved_protocol_ids)
                self._remove_protocol_ids_from_parent(player, moved_protocol_ids)
                native_player.refresh_state()
                continue

            cached_only_ids = known_protocol_ids - active_protocol_ids
            preserved_protocol_ids = moved_protocol_ids | cached_only_ids
            # A device that kept its id across a type change lists itself here.
            # It must never become its own protocol, and it must also be dropped
            # from the obsolete universal player so the permanent cleanup below
            # doesn't treat it as an orphaned protocol (which would re-wrap the
            # native player in a fresh universal player).
            preserved_protocol_ids.discard(native_player.player_id)
            self._migrate_protocol_ids_to_parent(native_player, preserved_protocol_ids)
            self._remove_protocol_ids_from_parent(
                player, preserved_protocol_ids | {native_player.player_id}
            )
            native_player.refresh_state()

            # Carry over the user's configuration and re-point group memberships
            # before the permanent removal below deletes the universal player's config
            self._migrate_universal_player_config(player.player_id, native_player.player_id)
            self._repoint_group_memberships(player.player_id, native_player.player_id)

            # Stop playback and remove the now-obsolete universal player
            self.mass.create_task(self._stop_and_unregister(player))

    def _migrate_universal_player_config(self, universal_id: str, native_id: str) -> None:
        """
        Carry over user-set configuration from a replaced universal player.

        Copies the custom display name, player config values, DSP settings and
        per-queue settings of the (obsolete) universal player onto the native
        player that replaces it, without overwriting values explicitly set on
        the native player itself. Must be called while the universal player's
        config still exists, as the permanent removal deletes it.

        :param universal_id: Player id of the obsolete universal player being replaced.
        :param native_id: Player id of the native player that replaces it.
        """
        source_raw = self.mass.config.get(f"{CONF_PLAYERS}/{universal_id}")
        source_raw = source_raw if isinstance(source_raw, dict) else {}
        target_key = f"{CONF_PLAYERS}/{native_id}"
        target_raw = self.mass.config.get(target_key)
        target_raw = target_raw if isinstance(target_raw, dict) else {}
        player_config_changed = False

        # only carry an actual user rename, not the auto-generated default name;
        # likewise a name on the native player only counts as a user override when
        # it differs from the default name (fresh configs store name == default_name)
        custom_name = source_raw.get("name")
        target_name = target_raw.get("name")
        target_has_custom_name = bool(target_name) and target_name != target_raw.get("default_name")
        if (
            custom_name
            and custom_name != source_raw.get("default_name")
            and not target_has_custom_name
        ):
            self.mass.config.set(f"{target_key}/name", custom_name)
            player_config_changed = True

        source_values = source_raw.get("values")
        source_values = source_values if isinstance(source_values, dict) else {}
        target_values = target_raw.get("values")
        target_values = target_values if isinstance(target_values, dict) else {}
        for key, value in source_values.items():
            if key in UNIVERSAL_PLAYER_INTERNAL_CONF_KEYS:
                continue
            if CONF_PROTOCOL_KEY_SPLITTER in key:
                # stale virtual mirror of a protocol player's own config
                continue
            if key in target_values:
                continue
            self.mass.config.set(f"{target_key}/values/{key}", deepcopy(value))
            player_config_changed = True

        # DSP settings follow wholesale, unless the native player has its own
        dsp_changed = False
        source_dsp = self.mass.config.get(f"{CONF_PLAYER_DSP}/{universal_id}")
        if source_dsp and not self.mass.config.get(f"{CONF_PLAYER_DSP}/{native_id}"):
            self.mass.config.set(f"{CONF_PLAYER_DSP}/{native_id}", deepcopy(source_dsp))
            dsp_changed = True

        queue_changed = self._migrate_universal_queue_config(universal_id, native_id)

        if not (player_config_changed or dsp_changed or queue_changed):
            return
        self.logger.info(
            "Carried over configuration of universal player %s to %s", universal_id, native_id
        )
        if player_config_changed:
            # the native player's in-place config was loaded before the carry-over,
            # so reload it to make the migrated values (e.g. custom name) effective
            self.mass.create_task(self._reapply_player_config(native_id))

    def _migrate_universal_queue_config(self, universal_id: str, native_id: str) -> bool:
        """
        Move the per-queue settings of a replaced universal player to its replacement.

        Queue ids equal player ids, so the source entry is removed once it is carried over.

        :param universal_id: Player id of the obsolete universal player.
        :param native_id: Player id of the native player that replaces it.
        :return: True if any queue setting was carried over.
        """
        queue_changed = False
        source_queue_raw = self.mass.config.get(f"{CONF_PLAYER_QUEUES}/{universal_id}")
        source_queue_raw = source_queue_raw if isinstance(source_queue_raw, dict) else None
        if source_queue_values := (source_queue_raw or {}).get("values"):
            target_queue_key = f"{CONF_PLAYER_QUEUES}/{native_id}"
            target_queue_raw = self.mass.config.get(target_queue_key)
            target_queue_raw = (
                deepcopy(target_queue_raw) if isinstance(target_queue_raw, dict) else {}
            )
            target_queue_values = target_queue_raw.setdefault("values", {})
            for key, value in source_queue_values.items():
                if key in target_queue_values:
                    continue
                target_queue_values[key] = deepcopy(value)
                queue_changed = True
            if queue_changed:
                target_queue_raw["queue_id"] = native_id
                self.mass.config.set(target_queue_key, target_queue_raw)
        if source_queue_raw is not None:
            self.mass.config.remove(f"{CONF_PLAYER_QUEUES}/{universal_id}")
        return queue_changed

    async def _reapply_player_config(self, player_id: str) -> None:
        """Reload the stored config onto a registered player and refresh its state."""
        if not (player := self.get_player(player_id)):
            return
        config = await self.mass.config.get_player_config(player_id)
        player.set_config(config)
        player.update_state()
        self.mass.signal_event(EventType.PLAYER_CONFIG_UPDATED, object_id=player_id, data=config)

    def _repoint_group_memberships(self, old_player_id: str, new_player_id: str) -> None:
        """
        Re-point group memberships from a removed player to its successor.

        When a universal player is replaced by a native player or merged into
        another universal player, other players that list the removed player as
        a group member (or allowed member) must follow its successor so those
        memberships are not silently lost. Updates the persisted config and keeps
        any registered player whose membership changed in sync.

        :param old_player_id: Player id that is being removed.
        :param new_player_id: Player id that replaces it.
        """
        all_player_configs = self.mass.config.get(CONF_PLAYERS, {})
        if not isinstance(all_player_configs, dict):
            return
        for other_id, other_cfg in all_player_configs.items():
            if not isinstance(other_cfg, dict):
                continue
            other_values = other_cfg.get("values")
            if not isinstance(other_values, dict):
                continue
            for key in (CONF_GROUP_MEMBERS, CONF_ALLOWED_MEMBERS):
                members = other_values.get(key)
                if not isinstance(members, list) or old_player_id not in members:
                    continue
                new_members: list[str] = []
                for member_id in members:
                    resolved = new_player_id if member_id == old_player_id else member_id
                    if resolved not in new_members:
                        new_members.append(resolved)
                self.mass.config.set(f"{CONF_PLAYERS}/{other_id}/values/{key}", new_members)
                # keep a registered player's in-place config copy and state in sync
                if other_player := self.get_player(other_id):
                    if entry := other_player.config.values.get(key):
                        entry.value = new_members
                    other_player.refresh_state()

    async def _stop_and_unregister(self, player: Player) -> None:
        """
        Stop active playback on a player and then permanently unregister it.

        Used when an obsolete universal player is replaced or merged away: while
        it is not idle its protocol child keeps playing the dead queue's stream
        until the buffer drains, so playback is stopped first. Queue ownership is
        intentionally not transferred.

        :param player: The obsolete player to stop and permanently remove.
        """
        if player.playback_state != PlaybackState.IDLE:
            with suppress(PlayerCommandFailed, PlayerUnavailableError):
                await self.mass.player_queues.stop(player.player_id)
        await self.unregister(player.player_id, permanent=True)

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
        # Never link a player to itself (hides it as its own protocol child).
        if native_player.player_id == protocol_player.player_id:
            return
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

        # Derived transports (e.g. a Sendspin bridge riding on an AirPlay player)
        # reference the base output they run on top of; "native" when they ride
        # on the parent player itself
        derived_from = protocol_player.underlying_player_id
        if derived_from == native_player.player_id:
            derived_from = "native"

        # Add the new link
        updated_protocols.append(
            LinkedOutputProtocol(
                output_protocol_id=protocol_player.player_id,
                protocol_domain=protocol_domain,
                priority=priority,
                derived_from=derived_from,
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

        # The freshly linked player may have derived protocol players (e.g. a
        # Sendspin bridge riding on it) waiting to join the same parent.
        self._link_derived_protocols_of(protocol_player)

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
        # Only save if the player config still exists to avoid creating partial entries
        if not self.mass.config.get(f"{CONF_PLAYERS}/{protocol_player_id}"):
            return
        conf_key = f"{CONF_PLAYERS}/{protocol_player_id}/values/{CONF_PROTOCOL_PARENT_ID}"
        self.mass.config.set(conf_key, parent_id)

    def _save_underlying_player_id(self, player: Player) -> None:
        """
        Persist the derived-transport edge of a player to config.

        Allows the edge to be resolved (e.g. by the config UI) even while the
        player is not registered. Clears a previously persisted edge when the
        player is no longer derived (e.g. a bridge client turned web player).
        """
        # Only save if the player config still exists to avoid creating partial entries
        if not self.mass.config.get(f"{CONF_PLAYERS}/{player.player_id}"):
            return
        conf_key = f"{CONF_PLAYERS}/{player.player_id}/values/{CONF_UNDERLYING_PLAYER_ID}"
        if player.underlying_player_id:
            self.mass.config.set(conf_key, player.underlying_player_id)
        elif self.mass.config.get(conf_key) is not None:
            self.mass.config.set(conf_key, None)

    def _get_cached_protocol_parent_id(self, protocol_player_id: str) -> str | None:
        """Get cached parent ID for a protocol player from config."""
        conf_key = f"{CONF_PLAYERS}/{protocol_player_id}/values/{CONF_PROTOCOL_PARENT_ID}"
        result = self.mass.config.get(conf_key, None)
        return str(result) if result else None

    def _clear_protocol_parent_id(self, protocol_player_id: str) -> None:
        """Clear the cached parent ID for a protocol player."""
        # Only clear if the player config still exists to avoid creating partial entries
        if not self.mass.config.get(f"{CONF_PLAYERS}/{protocol_player_id}"):
            return
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

        # Add link entries for any cached protocols that aren't currently linked
        for protocol_id in cached_protocol_ids:
            if protocol_id in linked_protocol_ids:
                continue  # Already linked

            # Get protocol player config to determine the protocol domain
            protocol_config = self.mass.config.get(f"{CONF_PLAYERS}/{protocol_id}")
            if not protocol_config:
                continue

            # Determine protocol domain from provider
            protocol_provider: str = protocol_config.get("provider")
            if not protocol_provider:
                continue

            # Extract domain from provider instance_id (e.g., "airplay--uuid" -> "airplay")
            protocol_domain = protocol_provider.split("--", maxsplit=1)[0]

            # Skip if parent already has a link from this domain
            existing_domains = {
                link.protocol_domain for link in native_player.linked_output_protocols
            }
            if protocol_domain in existing_domains:
                continue

            # Get priority for this protocol
            priority = PROTOCOL_PRIORITY.get(protocol_domain, 100)

            # Resolve the derived-transport edge from the live player when
            # registered, else from the persisted edge in config
            protocol_player = self.get_player(protocol_id)
            derived_from = (
                protocol_player.underlying_player_id
                if protocol_player
                else protocol_config.get("values", {}).get(CONF_UNDERLYING_PLAYER_ID)
            )
            if derived_from == native_player.player_id:
                derived_from = "native"

            # Appended in place rather than through set_linked_output_protocols():
            # the player is still registering, so nothing is listening yet, and the
            # update-all pass that closes registration marks it dirty and
            # recalculates its state.
            native_player.linked_output_protocols.append(
                LinkedOutputProtocol(
                    output_protocol_id=protocol_id,
                    protocol_domain=protocol_domain,
                    priority=priority,
                    derived_from=derived_from,
                )
            )
            self.logger.debug(
                "Recovered cached protocol link %s -> %s",
                native_player.player_id,
                protocol_id,
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
                        parent_player.refresh_state()
                else:
                    # Parent not registered yet — still purge the cached id
                    self._remove_protocol_id_from_cache(parent_id, player.player_id)
        else:
            # Native/universal player being removed: handle all linked protocol players.
            # Collect all known protocol IDs from both active links and cached state,
            # since disabled/inactive protocols may only exist in the cached parent data.
            all_protocol_ids = set(self._get_known_protocol_ids(player))
            for protocol_id in all_protocol_ids:
                if protocol_player := self.get_player(protocol_id):
                    # Protocol player is available: clear parent and schedule re-evaluation
                    # so it can be matched to a new parent or a new universal player
                    self.logger.debug(
                        "Player %s removed - scheduling evaluation for protocol %s",
                        player.player_id,
                        protocol_id,
                    )
                    self._detach_protocol_child(protocol_player)
                else:
                    # Clear cached parent ID in config so protocol won't try to
                    # restore a link to the deleted player on next restart
                    self._clear_protocol_parent_id(protocol_id)
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

    def _detach_protocol_children(self, parent_id: str) -> None:
        """
        Detach the registered protocol players of a parent player that is going away.

        Covers the removal paths that don't unregister the parent first (e.g. its
        provider is unloaded), where the parent is not around anymore to enumerate
        its protocol players.

        :param parent_id: Player id of the parent that is being removed.
        """
        for protocol_player in list(self._players.values()):
            if protocol_player.state.type != PlayerType.PROTOCOL:
                continue
            # a protocol player waiting for a parent that never registered only has
            # the link in its config, so fall back to the cached parent
            linked_parent_id = protocol_player.protocol_parent_id or (
                self._get_cached_protocol_parent_id(protocol_player.player_id)
            )
            if linked_parent_id != parent_id:
                continue
            self.logger.debug(
                "Player %s removed - scheduling evaluation for protocol %s",
                parent_id,
                protocol_player.player_id,
            )
            self._detach_protocol_child(protocol_player)

    def _detach_protocol_child(self, protocol_player: Player) -> None:
        """Clear a protocol player's parent link and schedule a fresh evaluation."""
        self._clear_protocol_parent_id(protocol_player.player_id)
        protocol_player.set_protocol_parent_id(None)
        protocol_player.refresh_state()
        self._schedule_protocol_evaluation(protocol_player)

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
                if protocol_player.available_for_playback and self._is_protocol_grouped(
                    protocol_player
                ):
                    self.logger.log(
                        VERBOSE_LOG_LEVEL,
                        "Selected protocol for %s: %s (grouped)",
                        player.state.name,
                        protocol_player.state.name,
                    )
                    return protocol_player, player.get_linked_protocol(linked.output_protocol_id)

        # 2. Check for user's preferred output protocol.
        # The value is only stored while it differs from the entry's default, which is computed
        # per player: "native" when a native output is available, otherwise "auto". Both of those
        # are handled identically by the steps below, so an absent value can safely fall through.
        preferred = self.mass.config.get_raw_player_config_value(
            player.player_id, CONF_PREFERRED_OUTPUT_PROTOCOL
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
                            if protocol_player.available_for_playback:
                                self.logger.log(
                                    VERBOSE_LOG_LEVEL,
                                    "Selected protocol for %s: %s (user preference)",
                                    player.state.name,
                                    protocol_player.state.name,
                                )
                                return protocol_player, player.get_linked_protocol(
                                    linked.output_protocol_id
                                )
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
                if protocol_player.available_for_playback:
                    self.logger.log(
                        VERBOSE_LOG_LEVEL,
                        "Selected protocol for %s: %s (priority-based)",
                        player.state.name,
                        protocol_player.state.name,
                    )
                    return protocol_player, player.get_linked_protocol(linked.output_protocol_id)

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
                and protocol_player.available_for_playback
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
            child_player.player_id, CONF_PREFERRED_OUTPUT_PROTOCOL
        )
        if not child_preferred or child_preferred in {"auto", "native"}:
            return None, None

        # Find child's preferred protocol, with its current availability
        child_protocol = None
        for output_protocol in child_player.output_protocols:
            if output_protocol.output_protocol_id == child_preferred:
                child_protocol = output_protocol
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
            if protocol_player and PlayerFeature.SET_MEMBERS in protocol_player.supported_features:
                return parent_output_protocol, child_protocol
        return None, None

    def _parent_has_live_native_session(self, parent_player: Player) -> bool:
        """
        Return True when the parent currently holds a live native playback session.

        The active output protocol lingers for a few seconds after stop, so a non-idle
        playback state is required to distinguish a real session from a just-stopped one.
        """
        return parent_player.active_output_protocol == "native" and (
            parent_player.state.playback_state in (PlaybackState.PLAYING, PlaybackState.PAUSED)
        )

    def _order_members_for_native_join(
        self,
        player_ids: list[str],
        parent_player: Player,
        parent_supports_native_grouping: bool,
    ) -> list[str]:
        """
        Order members so a live native session can be joined without splitting the group.

        When the parent already holds a live native session, children that cannot group
        natively are evaluated first: they may force a shared protocol for the whole group,
        and processing them before the native-capable children lets those join that same
        protocol instead of being stranded in a separate native sub-group. The order is left
        untouched when the parent is not playing natively, so fresh-group selection is unchanged.

        :param player_ids: The member IDs to be added, in their original order.
        :param parent_player: The parent player being joined.
        :param parent_supports_native_grouping: Whether the parent can group natively.
        """
        if not self._parent_has_live_native_session(parent_player):
            return player_ids
        return sorted(
            player_ids,
            key=lambda pid: bool(
                (child := self.get_player(pid))
                and self._can_use_native_grouping(
                    child, parent_player, parent_supports_native_grouping
                )
            ),
        )

    def _try_join_active_native_session(
        self,
        child_player: Player,
        parent_player: Player,
        parent_protocol_domain: str | None,
        parent_supports_native_grouping: bool,
        native_members: list[str],
    ) -> bool:
        """
        Add the child to native_members if it can join the parent's active native session.

        A child's preferred output protocol must only steer protocol selection when the child
        initiates its own playback; when it joins a parent that is already playing natively it
        should adopt native grouping if compatible, rather than forcing the whole group onto
        the child's preferred protocol. Skipped once a protocol has been selected for the group,
        so mixed batches stay cohesive on a single protocol.

        :param child_player: The player being added to the group.
        :param parent_player: The parent player being joined.
        :param parent_protocol_domain: The protocol domain already selected for the group, if any.
        :param parent_supports_native_grouping: Whether the parent can group natively.
        :param native_members: The native members list to append to when the child joins.
        """
        if not (
            self._parent_has_live_native_session(parent_player)
            and not parent_protocol_domain
            and self._can_use_native_grouping(
                child_player, parent_player, parent_supports_native_grouping
            )
        ):
            return False
        native_members.append(child_player.player_id)
        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "Joining parent's active native session for %s",
            child_player.state.name,
        )
        return True

    def _translate_native_members_to_protocol(
        self, parent_player: Player, protocol_domain: str, member_ids: list[str]
    ) -> list[str]:
        """
        Translate natively grouped members onto the protocol domain the parent plays through.

        Members that do not have that protocol cannot follow the parent at all and are
        dropped with a warning instead.

        :param parent_player: The parent player the members are grouped with.
        :param protocol_domain: The protocol domain selected for the group.
        :param member_ids: The member IDs that were selected for native grouping.
        """
        translated: list[str] = []
        for member_id in member_ids:
            member_player = self.get_player(member_id)
            if not member_player:
                continue
            # a native group's members may be listed by their protocol player id
            if member_player.protocol_parent_id:
                member_player = self.get_player(member_player.protocol_parent_id) or member_player
            member_protocol = member_player.get_output_protocol_by_domain(protocol_domain)
            if not member_protocol or not member_protocol.available:
                self.logger.warning(
                    "Cannot group %s with %s: the group plays through the %s protocol, "
                    "which %s does not support",
                    member_player.state.name,
                    parent_player.state.name,
                    protocol_domain,
                    member_player.state.name,
                )
                continue
            # For native protocol players, use the member's player_id directly
            translated.append(
                member_player.player_id
                if member_protocol.is_native
                else member_protocol.output_protocol_id
            )
            self.logger.log(
                VERBOSE_LOG_LEVEL,
                "Moving %s from native grouping to the %s protocol",
                member_player.state.name,
                protocol_domain,
            )
        return translated

    def _move_native_members_to_group_protocol(
        self,
        parent_player: Player,
        parent_protocol_player: Player | None,
        parent_protocol_domain: str | None,
        protocol_members: list[str],
        native_members: list[str],
    ) -> None:
        """
        Move the members selected for native grouping onto the protocol the group ended up on.

        Does nothing unless the group ends up on one of the parent's protocols while the
        parent's native grouping needs the parent's own stream: only then do those members
        have no session left to attach to.

        :param parent_player: The parent player being joined.
        :param parent_protocol_player: The protocol player selected for the group, if any.
        :param parent_protocol_domain: The protocol domain selected for the group, if any.
        :param protocol_members: The protocol member list the translated IDs are added to.
        :param native_members: The native member IDs, emptied when they are moved over.
        """
        if not (
            native_members
            and parent_protocol_domain
            and parent_protocol_player
            and parent_protocol_player.player_id != parent_player.player_id
            and parent_player.native_grouping_requires_own_stream
        ):
            return
        protocol_members.extend(
            self._translate_native_members_to_protocol(
                parent_player, parent_protocol_domain, native_members
            )
        )
        native_members.clear()

    def _migrate_stranded_native_members(
        self,
        parent_player: Player,
        parent_protocol_player: Player,
        protocol_members: list[str],
    ) -> list[str]:
        """
        Move the native members that a switch to the given protocol strands onto that protocol.

        Returns the member IDs that are still attached to the parent's own stream, so the
        caller can release them from it. Empty unless the parent renders that stream itself
        while its native grouping attaches the members to exactly that stream: only then are
        they left without anything to play.

        :param parent_player: The parent player that is about to switch protocol.
        :param parent_protocol_player: The protocol player the parent will render through.
        :param protocol_members: The protocol member list the translated IDs are added to.
        """
        if parent_protocol_player.player_id == parent_player.player_id:
            return []
        if not parent_player.native_grouping_requires_own_stream:
            return []
        if parent_player.active_output_protocol not in (None, "native"):
            return []
        if parent_player.state.playback_state not in (PlaybackState.PLAYING, PlaybackState.PAUSED):
            return []
        stranded = [
            member_id
            for member_id in parent_player.group_members
            if member_id != parent_player.player_id
        ]
        for protocol_id in self._translate_native_members_to_protocol(
            parent_player, parent_protocol_player.provider.domain, stranded
        ):
            if protocol_id not in protocol_members:
                protocol_members.append(protocol_id)
        return stranded

    async def _stop_native_session(
        self,
        parent_player: Player,
        parent_protocol_player: Player,
        stranded_native_members: list[str],
    ) -> None:
        """
        Release the given members from the parent's own stream and stop it.

        :param parent_player: The parent player whose native session is handed over.
        :param parent_protocol_player: The protocol player taking the output over.
        :param stranded_native_members: The members to release, already migrated.
        """
        self.logger.debug(
            "Stopping the native session of %s before switching to %s, migrated members: %s",
            parent_player.state.name,
            parent_protocol_player.state.name,
            stranded_native_members,
        )
        # The members already joined the protocol group, so the native session only has to
        # release them and stop. Releasing them first also clears the native group, which
        # keeps a later native playback command from resurrecting it. Both calls take the
        # provider's own lock, so they must run one after the other.
        await parent_player.set_members(player_ids_to_remove=stranded_native_members)
        await parent_player.stop()

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
        0. If the parent is already playing natively and the child can be grouped
           natively, join that native session (a child joining an existing group must
           not force the whole group onto its own preferred output protocol)
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
        player_ids = self._order_members_for_native_join(
            player_ids, parent_player, parent_supports_native_grouping
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

            # Priority 0: The parent is already playing natively and the child can join
            # that native session directly - adopt it before considering the child's own
            # preferred output protocol.
            if self._try_join_active_native_session(
                child_player,
                parent_player,
                parent_protocol_domain,
                parent_supports_native_grouping,
                native_members,
            ):
                continue

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

        # Post-pass: the protocol selected for the group is only known once every child has
        # been processed, so the members picked for native grouping are corrected here.
        self._move_native_members_to_group_protocol(
            parent_player,
            parent_protocol_player,
            parent_protocol_domain,
            protocol_members,
            native_members,
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

        # Members that ride the parent's own stream are stranded by the protocol switch below,
        # so they join the protocol group in this very same call and their native session is
        # torn down afterwards.
        stranded_native_members = (
            self._migrate_stranded_native_members(
                parent_player, parent_protocol_player, filtered_protocol_add
            )
            if filtered_protocol_add
            else []
        )

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
            # A paused player still holds its output, so the handover has to run for it
            # too - only the resume at the end is reserved for a player that was playing,
            # so adding a member does not start playback on its own.
            was_rendering = was_playing or parent_player.state.playback_state == (
                PlaybackState.PAUSED
            )

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
                "switching=%s, was_rendering=%s",
                is_native_protocol,
                already_using_native,
                already_using_this_protocol,
                switching_protocols,
                was_rendering,
            )

            # Update active output protocol if not already using native
            if not (is_native_protocol and already_using_native):
                parent_player.set_active_output_protocol(parent_protocol_player.player_id)

            # Hand the output over only if we're switching protocols
            if was_rendering and switching_protocols:
                self.logger.info(
                    "Handing the output of %s over to the %s protocol%s",
                    parent_player.state.name,
                    parent_protocol_player.provider.domain,
                    " and resuming playback" if was_playing else "",
                )
                if stranded_native_members:
                    await self._stop_native_session(
                        parent_player, parent_protocol_player, stranded_native_members
                    )
                # Collect existing members from old protocol before stopping it,
                # so we can re-add them to the new protocol afterwards.
                old_protocol_members: list[str] = []
                if (
                    previous_protocol
                    and previous_protocol not in (None, "native")
                    and (old_protocol_player := self.get_player(previous_protocol))
                    and old_protocol_player.player_id != parent_protocol_player.player_id
                ):
                    # Collect child members (exclude the leader itself)
                    old_protocol_members = [
                        m
                        for m in old_protocol_player.group_members
                        if m != old_protocol_player.player_id
                    ]
                    # Translate protocol IDs back to parent player IDs
                    old_parent_members: list[str] = []
                    for member_id in old_protocol_members:
                        if member_player := self.get_player(member_id):
                            parent_id = member_player.protocol_parent_id or member_id
                            if parent_id != parent_player.player_id:
                                old_parent_members.append(parent_id)
                    self.logger.debug(
                        "Stopping old protocol player %s before switching to %s, "
                        "migrating members: %s",
                        old_protocol_player.state.name,
                        parent_protocol_player.state.name,
                        old_parent_members,
                    )
                    # Use internal handler to stop the specific protocol player,
                    # bypassing group/sync redirect and queue redirect logic.
                    await self.mass.players._handle_cmd_stop(old_protocol_player.player_id)
                else:
                    old_parent_members = []
                # Resume playback on the new protocol and re-add migrated members.
                if was_playing:
                    await self.mass.players.cmd_resume(parent_player.player_id)
                if old_parent_members:
                    self.logger.debug(
                        "Re-adding migrated members %s to %s on new protocol",
                        old_parent_members,
                        parent_player.state.name,
                    )
                    # Use internal handler because we are already inside a
                    # _handle_set_members call chain that holds the play lock.
                    await self.mass.players._handle_set_members(
                        parent_player,
                        player_ids_to_add=old_parent_members,
                    )

        self.logger.debug(
            "After set_members, protocol player %s state: group_members=%s, synced_to=%s",
            parent_protocol_player.state.name,
            parent_protocol_player.group_members,
            parent_protocol_player.synced_to,
        )
