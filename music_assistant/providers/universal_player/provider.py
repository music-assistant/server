"""Universal Player Provider implementation.

This provider manages UniversalPlayer instances that are auto-created for devices
that have no native (vendor-specific) provider in Music Assistant but support one
or more generic streaming protocols such as AirPlay, Chromecast, or DLNA.

The Universal Player acts as a virtual player wrapper that provides a unified
interface while delegating actual playback to the underlying protocol player(s).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.constants import CONF_PLAYERS
from music_assistant.models.player import DeviceInfo
from music_assistant.models.player_provider import PlayerProvider

from .constants import CONF_LINKED_PROTOCOL_IDS, UNIVERSAL_PLAYER_PREFIX
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

    async def discover_players(self) -> None:
        """
        Discover players.

        Universal players are created dynamically by the PlayerController,
        not through discovery. However, we restore previously created
        universal players from config.
        """
        for player_conf in await self.mass.config.get_player_configs(self.instance_id):
            if player_conf.player_id.startswith(UNIVERSAL_PLAYER_PREFIX):
                # Restore universal player from config
                # The stored protocol IDs enable fast matching when protocols register
                await self._restore_player(player_conf.player_id)

    async def _restore_player(self, player_id: str) -> None:
        """
        Restore a universal player from config.

        The stored protocol_player_ids enable fast matching when protocol players
        register - they can be linked immediately without waiting for identifier matching.
        """
        # Get stored config values
        config = self.mass.config.get(f"{CONF_PLAYERS}/{player_id}")
        if not config:
            return

        # Get stored protocol player IDs for fast matching
        values = config.get("values", {})
        stored_protocol_ids = values.get(CONF_LINKED_PROTOCOL_IDS, [])

        # Create with minimal device info - identifiers will be populated
        # when protocol players connect and their identifiers are merged
        device_info = DeviceInfo(
            model="Universal Player",
            manufacturer="Music Assistant",
        )

        name = config.get("name", f"Universal Player {player_id}")

        self.logger.debug(
            "Restoring universal player %s with stored protocol IDs: %s",
            player_id,
            stored_protocol_ids,
        )

        player = UniversalPlayer(
            provider=self,
            player_id=player_id,
            name=name,
            device_info=device_info,
            protocol_player_ids=list(stored_protocol_ids),  # Store expected protocol IDs
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
        if existing := self.mass.players.get(player_id):
            # Update existing player with new protocol players
            if isinstance(existing, UniversalPlayer):
                for pid in protocol_player_ids:
                    existing.add_protocol_player(pid)
                # Merge identifiers from new device_info
                for id_type, value in device_info.identifiers.items():
                    existing.device_info.add_identifier(id_type, value)
                # Persist updated protocol IDs to config
                await self._save_protocol_ids(player_id, existing._protocol_player_ids)
                existing.update_state()
            return existing

        # Create config for the new player with stored protocol IDs
        self.mass.config.create_default_player_config(
            player_id=player_id,
            provider=self.instance_id,
            name=name,
            enabled=True,
            values={
                CONF_LINKED_PROTOCOL_IDS: protocol_player_ids,
            },
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
            await self._save_protocol_ids(player_id, player._protocol_player_ids)
            player.update_state()

    async def remove_universal_player(self, player_id: str) -> None:
        """
        Remove a universal player.

        Called when all protocol players for a device are removed.

        :param player_id: ID of the universal player to remove.
        """
        await self.mass.players.unregister(player_id, permanent=True)

    def get_universal_player(self, player_id: str) -> UniversalPlayer | None:
        """Get a UniversalPlayer by ID if it exists and is managed by this provider."""
        if player := self.mass.players.get(player_id):
            if isinstance(player, UniversalPlayer):
                return player
        return None
