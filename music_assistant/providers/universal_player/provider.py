"""
Universal Player Provider implementation.

This provider manages UniversalPlayer instances that are auto-created for devices
that have no native (vendor-specific) provider in Music Assistant but support one
or more generic streaming protocols such as AirPlay, Chromecast, or DLNA.

The Universal Player acts as a virtual player wrapper that provides a unified
interface while delegating actual playback to the underlying protocol player(s).
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from music_assistant_models.enums import IdentifierType, PlayerType

from music_assistant.constants import (
    CONF_LINKED_PROTOCOL_IDS,
    CONF_PLAYERS,
    CONF_PROTOCOL_PARENT_ID,
)
from music_assistant.models.player import DeviceInfo
from music_assistant.models.player_provider import PlayerProvider

from .constants import (
    CONF_CREATED_AT,
    CONF_DEVICE_IDENTIFIERS,
    CONF_DEVICE_INFO,
    UNIVERSAL_PLAYER_PREFIX,
)
from .player import UniversalPlayer

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry

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

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to setup this provider."""
        # Nothing to configure - universal players are auto-created
        return ()

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        # Serializes resolving, restoring and creating universal players, so a device
        # can never end up with two of them (and thus two player ids).
        self._lock = asyncio.Lock()

    async def discover_players(self) -> None:
        """
        Discover players.

        Universal players are created dynamically by the PlayerController,
        not through discovery. However, we restore previously created
        universal players from config.
        """
        async with self._lock:
            for player_conf in await self.mass.config.get_player_configs(
                self.instance_id, include_unavailable=True, include_disabled=True
            ):
                # Restore universal player from config
                # The stored protocol IDs enable fast matching when protocols register
                await self._restore_player(player_conf.player_id)

    async def create_universal_player(
        self,
        player_id: str,
        name: str,
        device_info: DeviceInfo,
        protocol_player_ids: list[str],
    ) -> Player:
        """
        Create a new UniversalPlayer.

        Called by the PlayerController when multiple protocol players are
        detected for a device without a native player.

        :param player_id: Player id for the new player, as minted by `mint_player_id`.
        :param name: Display name for the player.
        :param device_info: Aggregated device information.
        :param protocol_player_ids: List of protocol player IDs to link.
        :return: The created UniversalPlayer instance.
        """
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
                CONF_CREATED_AT: time.time_ns(),
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

    async def ensure_universal_players_for_protocols(
        self, protocol_players: list[Player]
    ) -> dict[str, Player]:
        """
        Ensure a universal player exists for a set of protocol players of one device.

        A device keeps the universal player it already belongs to, so its player id -
        the identity API consumers (such as the Home Assistant integration) bind to -
        stays the same for the lifetime of the device. Only a device that was never
        wrapped before gets a newly minted id.

        A protocol domain the universal player already serves means a second device
        behind the same identifiers (e.g. two AirPlay instances on one host); such a
        player gets a universal player of its own.

        :param protocol_players: List of protocol players for the same device.
        :return: The universal player per protocol player id, keyed by protocol player id.
        """
        async with self._lock:
            # Re-check - another task may have already handled these players
            # Filter out players that are already linked to a parent
            protocol_players = [p for p in protocol_players if not p.protocol_parent_id]
            if not protocol_players:
                return {}

            # The parent link persisted on the protocol player is the canonical side
            # of the relation: it names the universal player this device belongs to.
            assignments: dict[str, Player] = {}
            unassigned: list[Player] = []
            for player in protocol_players:
                if universal_player := await self._resolve_stored_universal_player(player):
                    assignments[player.player_id] = universal_player
                else:
                    unassigned.append(player)

            target = next(iter(assignments.values()), None)
            served_domains: set[str] = set()
            if target is None:
                # this device was never wrapped before: create one universal player
                # for it, taking a single protocol player per domain
                members = self._first_player_per_domain(unassigned)
                target = await self.create_universal_player(
                    player_id=self.mint_player_id(),
                    name=self._get_clean_player_name(members),
                    device_info=self._aggregate_device_info(members),
                    protocol_player_ids=[p.player_id for p in members],
                )
                for player in members:
                    assignments[player.player_id] = target
                    served_domains.add(player.provider.domain)
                unassigned = [p for p in unassigned if p.player_id not in assignments]
            else:
                served_domains = self._served_domains(target)
                served_domains.update(
                    player.provider.domain
                    for player in protocol_players
                    if assignments.get(player.player_id) is target
                )

            for player in unassigned:
                if player.provider.domain in served_domains:
                    assignments[player.player_id] = await self._create_separate_universal_player(
                        player
                    )
                    continue
                served_domains.add(player.provider.domain)
                await self.add_protocol_to_universal_player(target.player_id, player.player_id)
                assignments[player.player_id] = target

            return assignments

    def mint_player_id(self) -> str:
        """Return an unused player id for a new universal player."""
        while True:
            player_id = f"{UNIVERSAL_PLAYER_PREFIX}{uuid4().hex[:8]}"
            if self.mass.players.get_player(player_id):
                continue
            if self.mass.config.get(f"{CONF_PLAYERS}/{player_id}"):
                continue
            return player_id

    def get_universal_player(self, player_id: str) -> UniversalPlayer | None:
        """Get a UniversalPlayer by ID if it exists and is managed by this provider."""
        if player := self.mass.players.get_player(player_id):
            if isinstance(player, UniversalPlayer):
                return player
        return None

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

    async def _restore_player(self, player_id: str) -> None:
        """
        Restore a universal player from config.

        The stored protocol_player_ids enable fast matching when protocol players
        register - they can be linked immediately without waiting for identifier matching.
        Device identifiers are also restored to enable matching new protocol players.
        """
        if self.get_universal_player(player_id):
            # a restore replaces the player instance, which would drop the output
            # protocol links of the registered one while its members still point here
            return

        # Get stored config values
        config = self.mass.config.get(f"{CONF_PLAYERS}/{player_id}")
        if not config:
            return

        # Get stored values
        values = config.get("values") or {}
        stored_identifiers = values.get(CONF_DEVICE_IDENTIFIERS, {})
        stored_device_info = values.get(CONF_DEVICE_INFO, {})

        all_player_configs = self.mass.config.get(CONF_PLAYERS, {})
        valid_protocol_ids = self._resolve_stored_protocol_ids(player_id, all_player_configs)

        # When nothing links to the stored universal player config (anymore),
        # keep it - it holds user customizations - and simply skip restoring:
        # the config is picked up again once a protocol player that stored this
        # player as its parent registers.
        if not valid_protocol_ids:
            self.logger.debug(
                "Not restoring universal player %s - no linked protocol players remain",
                player_id,
            )
            return

        # Protocols that (also) belong to a native player mean this universal
        # player is a leftover wrapper: repair the protocol links to point at
        # the rightful native parent and replace the wrapper by that native
        # player instead of restoring it.
        native_claims: dict[str, str] = {}
        for protocol_id in valid_protocol_ids:
            for other_player_id, other_config in all_player_configs.items():
                if other_player_id == player_id:
                    continue
                if other_config.get("provider") == "universal_player":
                    continue
                other_values = other_config.get("values") or {}
                if protocol_id in (other_values.get(CONF_LINKED_PROTOCOL_IDS) or []):
                    native_claims[protocol_id] = other_player_id
                    break
        if native_claims:
            # Members are same-device by construction, so members not claimed by
            # any native follow the first claimer (keeps the cascade-disable
            # repair intact for protocols that appeared after the parent left).
            default_parent = next(iter(native_claims.values()))
            by_parent: dict[str, list[str]] = {}
            for protocol_id in valid_protocol_ids:
                parent_id = native_claims.get(protocol_id, default_parent)
                by_parent.setdefault(parent_id, []).append(protocol_id)
            for native_id, protocol_ids in by_parent.items():
                self.logger.info(
                    "Not restoring universal player %s - protocols %s are linked "
                    "to native player %s",
                    player_id,
                    protocol_ids,
                    native_id,
                )
                await self._reparent_protocols_to_native(native_id, protocol_ids)
            # Mirror the runtime replacement by a native player: carry the
            # wrapper's user settings and group memberships over to the native
            # player and delete the wrapper's now-obsolete config, so it doesn't
            # linger as a permanently unavailable entry in the settings UI.
            # Skipped while the wrapper is still registered - the runtime
            # replacement flow owns that transition.
            if not self.mass.players.get_player(player_id):
                self.logger.info(
                    "Removing stored config of universal player %s - replaced by %s",
                    player_id,
                    default_parent,
                )
                self.mass.players._migrate_universal_player_config(player_id, default_parent)
                self.mass.players._repoint_group_memberships(player_id, default_parent)
                self.mass.players.delete_player_config(player_id)
            return

        stored_protocol_ids = valid_protocol_ids

        # Persist the updated protocol IDs to config if they changed
        if valid_protocol_ids != list(values.get(CONF_LINKED_PROTOCOL_IDS) or []):
            self.mass.config.set(
                f"{CONF_PLAYERS}/{player_id}/values/{CONF_LINKED_PROTOCOL_IDS}",
                valid_protocol_ids,
            )

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

        # the default name, not the custom one: display_name already prefers the
        # custom name, while update_state persists this one as the default name
        name = config.get("default_name") or config.get("name") or f"Universal Player {player_id}"

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

    def _resolve_stored_protocol_ids(
        self, player_id: str, all_player_configs: dict[str, dict[str, Any]]
    ) -> list[str]:
        """
        Resolve the current protocol player membership of a stored universal player.

        Reconciles the universal player's stored member list with the parent links
        persisted on the protocol players themselves, dropping members that moved
        away or unlinked. Configs are never deleted here.
        """
        config = all_player_configs.get(player_id) or {}
        values = config.get("values") or {}
        stored_protocol_ids = list(values.get(CONF_LINKED_PROTOCOL_IDS) or [])

        # The child's persisted parent link is the canonical side of the relation:
        # also pick up children that point at us but are missing from our stored
        # list (e.g. only one side of the link survived an interrupted shutdown).
        for child_id, child_config in all_player_configs.items():
            child_values = child_config.get("values") or {}
            if child_values.get(CONF_PROTOCOL_PARENT_ID) != player_id:
                continue
            if child_id not in stored_protocol_ids:
                stored_protocol_ids.append(child_id)

        valid_protocol_ids = []
        for protocol_id in stored_protocol_ids:
            protocol_config = all_player_configs.get(protocol_id)
            if not protocol_config:
                # Config doesn't exist, keep it for now (player may register later)
                valid_protocol_ids.append(protocol_id)
                continue
            protocol_values = protocol_config.get("values") or {}
            parent_id = protocol_values.get(CONF_PROTOCOL_PARENT_ID)
            if parent_id == player_id:
                # the persisted parent link proves this child still belongs to us,
                # even if its player_type was left stale by an aborted registration
                valid_protocol_ids.append(protocol_id)
                continue
            if parent_id:
                self.logger.info(
                    "Removing %s from universal player %s - moved to parent %s",
                    protocol_id,
                    player_id,
                    parent_id,
                )
                continue
            if protocol_config.get("player_type") != "protocol":
                self.logger.info(
                    "Removing %s from universal player %s - player type changed to %s",
                    protocol_id,
                    player_id,
                    protocol_config.get("player_type"),
                )
                continue
            # unlinked protocol player: no longer ours, but keep its config -
            # it may relink (or be adopted by another player) once it registers
            self.logger.info(
                "Removing %s from universal player %s - no longer linked",
                protocol_id,
                player_id,
            )
        return valid_protocol_ids

    async def _reparent_protocols_to_native(
        self, native_parent_id: str, protocol_ids: list[str]
    ) -> None:
        """
        Restore protocol players' parent link to their rightful native parent.

        Used to repair configs when a stale universal player wrapped protocols that
        belong to a native player: the protocols' cached parent_id was overwritten to
        point at the universal player when it was created. Protocols of a disabled
        native parent are cascade-disabled as well, so they don't immediately wrap
        into a fresh universal player on the next registration cycle.
        """
        parent_config = self.mass.config.get(f"{CONF_PLAYERS}/{native_parent_id}") or {}
        parent_enabled = parent_config.get("enabled", True)
        for protocol_id in protocol_ids:
            protocol_raw = self.mass.config.get(f"{CONF_PLAYERS}/{protocol_id}")
            if not protocol_raw:
                continue
            self.mass.config.set(
                f"{CONF_PLAYERS}/{protocol_id}/values/{CONF_PROTOCOL_PARENT_ID}",
                native_parent_id,
            )
            if parent_enabled or not protocol_raw.get("enabled", True):
                continue
            self.logger.info(
                "Disabling orphaned protocol player %s to match its disabled parent %s",
                protocol_id,
                native_parent_id,
            )
            await self.mass.config.save_player_config(protocol_id, {"enabled": False})

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

    async def _create_separate_universal_player(self, protocol_player: Player) -> Player:
        """
        Create a separate universal player for a protocol player that was rejected.

        Used when a second instance of the same protocol domain (e.g., two AirPlay
        instances on the same host) cannot join the universal player of the device.

        :param protocol_player: The protocol player that needs its own universal player.
        """
        return await self.create_universal_player(
            player_id=self.mint_player_id(),
            name=self._get_clean_player_name([protocol_player]),
            device_info=self._aggregate_device_info([protocol_player]),
            protocol_player_ids=[protocol_player.player_id],
        )

    async def _resolve_stored_universal_player(
        self, protocol_player: Player
    ) -> UniversalPlayer | None:
        """
        Return the universal player a protocol player is persistently linked to, if any.

        :param protocol_player: The protocol player to resolve the universal player of.
        """
        parent_id = self.mass.config.get(
            f"{CONF_PLAYERS}/{protocol_player.player_id}/values/{CONF_PROTOCOL_PARENT_ID}"
        )
        if not isinstance(parent_id, str) or not parent_id:
            return None
        if existing := self.get_universal_player(parent_id):
            return existing
        raw_conf = self.mass.config.get(f"{CONF_PLAYERS}/{parent_id}")
        # the "up" prefix alone is not enough, a native player id could
        # coincidentally carry it, so require our own provider as well
        if not isinstance(raw_conf, dict) or raw_conf.get("provider") != self.instance_id:
            return None
        if not raw_conf.get("enabled", True):
            # the user turned this device off, bringing it back would defeat that intent
            return None
        await self._restore_player(parent_id)
        return self.get_universal_player(parent_id)

    def _served_domains(self, universal_player: Player) -> set[str]:
        """Return the protocol domains that are occupied on a universal player."""
        return {
            link.protocol_domain
            for link in universal_player.linked_output_protocols
            # a registered player occupies this domain slot even if unavailable
            if link.protocol_domain and self.mass.players.get_player(link.output_protocol_id)
        }

    def _first_player_per_domain(self, protocol_players: list[Player]) -> list[Player]:
        """Return the first protocol player of every distinct protocol domain."""
        seen: set[str] = set()
        members: list[Player] = []
        for player in protocol_players:
            if player.provider.domain in seen:
                continue
            seen.add(player.provider.domain)
            members.append(player)
        return members

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
