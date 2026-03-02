"""Universal Player Provider implementation.

This provider manages UniversalPlayer instances that are auto-created for devices
that have no native (vendor-specific) provider in Music Assistant but support one
or more generic streaming protocols such as AirPlay, Chromecast, or DLNA.

The Universal Player acts as a virtual player wrapper that provides a unified
interface while delegating actual playback to the underlying protocol player(s).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from music_assistant_models.enums import IdentifierType, PlayerType

from music_assistant.constants import CONF_LINKED_PROTOCOL_IDS, CONF_PLAYERS
from music_assistant.helpers.util import normalize_mac_for_matching
from music_assistant.models.player import DeviceInfo
from music_assistant.models.player_provider import PlayerProvider

from .constants import CONF_DEVICE_IDENTIFIERS, CONF_DEVICE_INFO, UNIVERSAL_PLAYER_PREFIX
from .player import UniversalPlayer

if TYPE_CHECKING:
    from music_assistant.models.player import Player


class UniversalPlayerProvider(PlayerProvider):
    """
    Universal Player Provider.

    Manages virtual players for devices that have no native (vendor-specific) provider
    but support generic streaming protocols like AirPlay, Chromecast, or DLNA.
    These players are automatically created when protocol players with PlayerType.PROTOCOL
    are registered, providing a unified interface while delegating playback to the
    underlying protocol player(s).
    """

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        # Lock to prevent race conditions during universal player creation
        self._universal_player_locks: dict[str, asyncio.Lock] = {}

    async def discover_players(self) -> None:
        """
        Discover players.

        Universal players are created dynamically by the PlayerController,
        not through discovery. However, we restore previously created
        universal players from config.
        """
        for player_conf in await self.mass.config.get_player_configs(
            self.instance_id, include_unavailable=True, include_disabled=True
        ):
            if player_conf.player_id.startswith(UNIVERSAL_PLAYER_PREFIX):
                # Restore universal player from config
                # The stored protocol IDs enable fast matching when protocols register
                await self._restore_player(player_conf.player_id)

    async def _restore_player(self, player_id: str) -> None:
        """
        Restore a universal player from config.

        The stored protocol_player_ids enable fast matching when protocol players
        register - they can be linked immediately without waiting for identifier matching.
        Device identifiers are also restored to enable matching new protocol players.
        """
        # Get stored config values
        config = self.mass.config.get(f"{CONF_PLAYERS}/{player_id}")
        if not config:
            return

        # Get stored values
        values = config.get("values", {})
        stored_protocol_ids = list(values.get(CONF_LINKED_PROTOCOL_IDS, []))
        stored_identifiers = values.get(CONF_DEVICE_IDENTIFIERS, {})
        stored_device_info = values.get(CONF_DEVICE_INFO, {})

        # Filter out protocol IDs that are no longer PROTOCOL type players
        valid_protocol_ids = []
        for protocol_id in stored_protocol_ids:
            protocol_config = self.mass.config.get(f"{CONF_PLAYERS}/{protocol_id}")
            if not protocol_config:
                # Config doesn't exist, keep it for now (player may register later)
                valid_protocol_ids.append(protocol_id)
                continue
            protocol_player_type = protocol_config.get("player_type")
            if protocol_player_type == "protocol":
                valid_protocol_ids.append(protocol_id)
            else:
                self.logger.info(
                    "Removing %s from universal player %s - player type changed to %s",
                    protocol_id,
                    player_id,
                    protocol_player_type,
                )

        # If no valid protocol IDs remain, delete this stale universal player
        if not valid_protocol_ids:
            self.logger.info(
                "Deleting stale universal player %s - no valid protocol players remain",
                player_id,
            )
            await self.mass.config.remove_player_config(player_id)
            return

        stored_protocol_ids = valid_protocol_ids

        # Persist the filtered protocol IDs to config if they changed
        if len(valid_protocol_ids) != len(values.get(CONF_LINKED_PROTOCOL_IDS, [])):
            self.mass.config.set(
                f"{CONF_PLAYERS}/{player_id}/values/{CONF_LINKED_PROTOCOL_IDS}",
                valid_protocol_ids,
            )

        # Check if protocols have been linked to a native player (stale universal player)
        for protocol_id in stored_protocol_ids:
            protocol_config = self.mass.config.get(f"{CONF_PLAYERS}/{protocol_id}")
            if protocol_config:
                protocol_values = protocol_config.get("values", {})
                protocol_parent_id = protocol_values.get("protocol_parent_id")
                if protocol_parent_id and protocol_parent_id != player_id:
                    self.logger.info(
                        "Deleting stale universal player %s - protocol %s has moved to parent %s",
                        player_id,
                        protocol_id,
                        protocol_parent_id,
                    )
                    await self.mass.config.remove_player_config(player_id)
                    return

            # Check if native player has this protocol in linked_protocol_ids
            all_player_configs = self.mass.config.get(CONF_PLAYERS, {})
            for other_player_id, other_config in all_player_configs.items():
                if other_player_id == player_id:
                    continue
                if other_config.get("provider") == "universal_player":
                    continue
                other_values = other_config.get("values", {})
                linked_protocols = other_values.get(CONF_LINKED_PROTOCOL_IDS, [])
                if protocol_id in linked_protocols:
                    self.logger.info(
                        "Deleting stale universal player %s - "
                        "protocol %s is linked to native player %s",
                        player_id,
                        protocol_id,
                        other_player_id,
                    )
                    await self.mass.config.remove_player_config(player_id)
                    return

        # Restore device info with stored values or defaults
        device_info = DeviceInfo(
            model=stored_device_info.get("model", "Universal Player"),
            manufacturer=stored_device_info.get("manufacturer", "Music Assistant"),
        )

        # Restore identifiers (convert string keys back to IdentifierType enum)
        for id_type_str, value in stored_identifiers.items():
            try:
                id_type = IdentifierType(id_type_str)
                device_info.add_identifier(id_type, value)
            except ValueError:
                self.logger.warning(
                    "Unknown identifier type %s for player %s", id_type_str, player_id
                )

        name = config.get("name", f"Universal Player {player_id}")

        self.logger.debug(
            "Restoring universal player %s with %d protocol IDs and %d identifiers",
            player_id,
            len(stored_protocol_ids),
            len(stored_identifiers),
        )

        player = UniversalPlayer(
            provider=self,
            player_id=player_id,
            name=name,
            device_info=device_info,
            protocol_player_ids=list(stored_protocol_ids),
        )
        await self.mass.players.register_or_update(player)

    async def create_universal_player(
        self,
        device_key: str,
        name: str,
        device_info: DeviceInfo,
        protocol_player_ids: list[str],
    ) -> Player:
        """
        Create a new UniversalPlayer.

        Called by the PlayerController when multiple protocol players are
        detected for a device without a native player.

        :param device_key: Unique device key (typically MAC address).
        :param name: Display name for the player.
        :param device_info: Aggregated device information.
        :param protocol_player_ids: List of protocol player IDs to link.
        :return: The created UniversalPlayer instance.
        """
        # Generate player_id from device_key
        player_id = f"{UNIVERSAL_PLAYER_PREFIX}{device_key}"

        # Check if player already exists
        if existing := self.mass.players.get_player(player_id):
            # Update existing player with new protocol players
            if isinstance(existing, UniversalPlayer):
                for pid in protocol_player_ids:
                    existing.add_protocol_player(pid)
                # Merge identifiers from new device_info
                for id_type, value in device_info.identifiers.items():
                    existing.device_info.add_identifier(id_type, value)
                # Persist updated data to config
                await self._save_player_data(player_id, existing)
                existing.update_state()
            return existing

        # Create config for the new player (complex values saved separately after)
        self.mass.config.create_default_player_config(
            player_id=player_id,
            provider=self.instance_id,
            player_type=PlayerType.GROUP,
            name=name,
            enabled=True,
            values={
                CONF_LINKED_PROTOCOL_IDS: protocol_player_ids,
            },
        )

        # Save device identifiers and info to config (these are nested dicts,
        # not supported by ConfigValueType, so we save them directly)
        base_key = f"{CONF_PLAYERS}/{player_id}/values"
        self.mass.config.set(
            f"{base_key}/{CONF_DEVICE_IDENTIFIERS}",
            {k.value: v for k, v in device_info.identifiers.items()},
        )
        self.mass.config.set(
            f"{base_key}/{CONF_DEVICE_INFO}",
            {"model": device_info.model, "manufacturer": device_info.manufacturer},
        )

        self.logger.info(
            "Creating universal player %s with protocol players: %s",
            player_id,
            protocol_player_ids,
        )

        # Create the player instance
        player = UniversalPlayer(
            provider=self,
            player_id=player_id,
            name=name,
            device_info=device_info,
            protocol_player_ids=protocol_player_ids,
        )

        await self.mass.players.register_or_update(player)
        return player

    async def _save_protocol_ids(self, player_id: str, protocol_player_ids: list[str]) -> None:
        """Save protocol player IDs to config for persistence across restarts."""
        conf_key = f"{CONF_PLAYERS}/{player_id}/values/{CONF_LINKED_PROTOCOL_IDS}"
        self.mass.config.set(conf_key, protocol_player_ids)
        self.logger.debug(
            "Saved protocol IDs for %s: %s",
            player_id,
            protocol_player_ids,
        )

    async def _save_player_data(self, player_id: str, player: UniversalPlayer) -> None:
        """Save all player data to config for persistence across restarts."""
        base_key = f"{CONF_PLAYERS}/{player_id}/values"

        # Save protocol IDs
        self.mass.config.set(
            f"{base_key}/{CONF_LINKED_PROTOCOL_IDS}",
            player._protocol_player_ids,
        )

        # Save identifiers (convert IdentifierType enum keys to strings)
        self.mass.config.set(
            f"{base_key}/{CONF_DEVICE_IDENTIFIERS}",
            {k.value: v for k, v in player.device_info.identifiers.items()},
        )

        # Save device info (model, manufacturer)
        self.mass.config.set(
            f"{base_key}/{CONF_DEVICE_INFO}",
            {
                "model": player.device_info.model,
                "manufacturer": player.device_info.manufacturer,
            },
        )

        self.logger.debug(
            "Saved player data for %s: %d protocols, %d identifiers",
            player_id,
            len(player._protocol_player_ids),
            len(player.device_info.identifiers),
        )

    async def add_protocol_to_universal_player(
        self, player_id: str, protocol_player_id: str
    ) -> None:
        """
        Add a protocol player to an existing universal player.

        Called when a new protocol player is discovered that matches an existing
        universal player.

        :param player_id: ID of the universal player.
        :param protocol_player_id: ID of the protocol player to add.
        """
        if player := self.get_universal_player(player_id):
            player.add_protocol_player(protocol_player_id)
            # Save all player data (protocol IDs, identifiers, device info)
            await self._save_player_data(player_id, player)
            player.update_state()

    async def remove_universal_player(self, player_id: str) -> None:
        """
        Remove a universal player.

        Called when all protocol players for a device are removed.

        :param player_id: ID of the universal player to remove.
        """
        await self.mass.players.unregister(player_id, permanent=True)

    async def ensure_universal_player_for_protocols(
        self, protocol_players: list[Player]
    ) -> Player | None:
        """
        Ensure a universal player exists for a set of protocol players.

        This method handles the orchestration of creating or updating a universal player
        for the given protocol players. It uses per-device locking to prevent race
        conditions when multiple protocols for the same device register simultaneously.

        When a second instance of the same protocol domain tries to join an existing
        universal player (e.g., two AirPlay instances on the same host), the duplicate
        is separated out and given its own universal player with a player_id-based key.

        :param protocol_players: List of protocol players for the same device.
        :return: The created or updated universal player, or None if operation failed.
        """
        device_key = self._get_device_key_from_players(protocol_players)
        if not device_key:
            return None

        universal_player_id = f"{UNIVERSAL_PLAYER_PREFIX}{device_key}"

        # Use a per-device lock to prevent race conditions
        if device_key not in self._universal_player_locks:
            self._universal_player_locks[device_key] = asyncio.Lock()

        async with self._universal_player_locks[device_key]:
            # Re-check - another task may have already handled these players
            # Filter out players that are already linked to a parent
            protocol_players = [p for p in protocol_players if not p.protocol_parent_id]
            if not protocol_players:
                return None

            # Check if universal player already exists
            if existing := self.mass.players.get_player(universal_player_id):
                if isinstance(existing, UniversalPlayer):
                    # Separate players into those that can join vs those that are
                    # domain-duplicates (a domain already active on the universal player)
                    active_domains: set[str] = set()
                    for link in existing.linked_output_protocols:
                        if not link.protocol_domain:
                            continue
                        linked_player = self.mass.players.get_player(link.output_protocol_id)
                        if linked_player and linked_player.available:
                            active_domains.add(link.protocol_domain)
                    can_join = [
                        p for p in protocol_players if p.provider.domain not in active_domains
                    ]
                    rejected = [p for p in protocol_players if p.provider.domain in active_domains]

                    # Add players that can join to the existing universal player
                    for player in can_join:
                        await self.add_protocol_to_universal_player(
                            universal_player_id, player.player_id
                        )

                    # Create separate universal players for rejected (domain-duplicate)
                    # players using player_id-based device keys
                    for player in rejected:
                        fallback_key = player.player_id.replace(":", "").replace("-", "").lower()
                        await self._create_separate_universal_player(fallback_key, player)

                return existing

            # Create new universal player
            device_info = self._aggregate_device_info(protocol_players)
            name = self._get_clean_player_name(protocol_players)
            protocol_player_ids = [p.player_id for p in protocol_players]

            return await self.create_universal_player(
                device_key=device_key,
                name=name,
                device_info=device_info,
                protocol_player_ids=protocol_player_ids,
            )

    def get_universal_player(self, player_id: str) -> UniversalPlayer | None:
        """Get a UniversalPlayer by ID if it exists and is managed by this provider."""
        if player := self.mass.players.get_player(player_id):
            if isinstance(player, UniversalPlayer):
                return player
        return None

    async def _create_separate_universal_player(
        self, device_key: str, protocol_player: Player
    ) -> Player | None:
        """
        Create a separate universal player for a protocol player that was rejected.

        Used when a second instance of the same protocol domain (e.g., two AirPlay
        instances on the same host) cannot join the existing universal player.
        A unique device_key derived from the player_id ensures no collision.

        :param device_key: Unique device key for this player (player_id-based).
        :param protocol_player: The protocol player that needs its own universal player.
        """
        universal_player_id = f"{UNIVERSAL_PLAYER_PREFIX}{device_key}"

        # Check if this separate universal player already exists
        if existing := self.mass.players.get_player(universal_player_id):
            if isinstance(existing, UniversalPlayer):
                await self.add_protocol_to_universal_player(
                    universal_player_id, protocol_player.player_id
                )
            return existing

        device_info = self._aggregate_device_info([protocol_player])
        name = self._get_clean_player_name([protocol_player])

        return await self.create_universal_player(
            device_key=device_key,
            name=name,
            device_info=device_info,
            protocol_player_ids=[protocol_player.player_id],
        )

    async def remove_player(self, player_id: str) -> None:
        """Remove a universal player and clean up any stale protocol player configs."""
        if player := self.get_universal_player(player_id):
            # Clean up configs for protocol players tracked by this universal player
            # that are not currently registered (unavailable/stale).
            # Available protocol players are handled by _cleanup_protocol_links
            # in the player controller (clears parent + schedules re-evaluation).
            for protocol_id in list(player._protocol_player_ids):
                if not self.mass.players.get_player(protocol_id):
                    self.logger.info(
                        "Cleaning up stale protocol config %s from universal player %s",
                        protocol_id,
                        player_id,
                    )
                    self.mass.players.delete_player_config(protocol_id)
        await self.remove_universal_player(player_id)

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
            # Use normalize_mac_for_matching to handle locally-administered MAC variants
            # Some protocols (like AirPlay) report a variant where bit 1 of the first octet
            # is set (e.g., 54:78:... vs 56:78:...), but they represent the same device
            if mac := identifiers.get(IdentifierType.MAC_ADDRESS):
                return normalize_mac_for_matching(mac)
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
            player_name = player.state.name
            if is_valid_name(player_name):
                return player_name

        # Fallback to first player's name if no valid name found
        return protocol_players[0].display_name
