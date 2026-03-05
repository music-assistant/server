"""Dashie Kiosk Player provider for Music Assistant."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlparse

from music_assistant.models.player_provider import PlayerProvider

from .client import DashieKioskClient
from .constants import CONF_MANUAL_PLAYERS, CONF_PLAYERS, DASHIE_HA_DOMAIN, RETRY_INTERVAL
from .player import DashieKioskPlayer

if TYPE_CHECKING:
    from hass_client.models import Device as HassDevice
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant import MusicAssistant
    from music_assistant.providers.hass import HomeAssistantProvider

_LOGGER = logging.getLogger(__name__)


class DashieKioskProvider(PlayerProvider):
    """Player provider for Dashie Kiosk Android tablets."""

    hass_prov: HomeAssistantProvider | None
    _pending_players: dict[str, HassDevice | None]

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        hass_prov: HomeAssistantProvider | None,
    ) -> None:
        """Initialize the provider."""
        super().__init__(mass, manifest, config)
        self.hass_prov = hass_prov
        self._pending_players = {}
        self._retry_task: asyncio.Task[None] | None = None

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        await super().loaded_in_mass()
        # Set up HA-discovered players
        player_ids = cast("list[str]", self.config.get_value(CONF_PLAYERS)) or []
        if player_ids and self.hass_prov:
            await self._setup_ha_players(player_ids)
        # Set up manually configured players
        manual_addresses = cast("list[str]", self.config.get_value(CONF_MANUAL_PLAYERS)) or []
        for raw_address in manual_addresses:
            address = raw_address.strip()
            if not address:
                continue
            success = await self._setup_manual_player(address)
            if not success:
                self._pending_players[address] = None
        # Start retry loop for any devices that failed to connect
        if self._pending_players:
            _LOGGER.info(
                "%d device(s) offline at startup, will retry: %s",
                len(self._pending_players),
                ", ".join(self._pending_players.keys()),
            )
            self._retry_task = self.mass.create_task(self._retry_pending_players())

    async def _setup_ha_players(self, player_ids: list[str]) -> None:
        """Set up players discovered via Home Assistant."""
        assert self.hass_prov is not None
        # Fetch device and entity registries from HA
        device_registry = {x["id"]: x for x in await self.hass_prov.hass.get_device_registry()}
        entity_registry = {
            x["entity_id"]: x for x in await self.hass_prov.hass.get_entity_registry()
        }
        for entity_id in player_ids:
            entity_entry = entity_registry.get(entity_id)
            if not entity_entry:
                _LOGGER.warning("Entity %s not found in registry, skipping", entity_id)
                continue
            if entity_entry.get("platform") != DASHIE_HA_DOMAIN:
                continue
            device_id = entity_entry.get("device_id", "")
            hass_device = device_registry.get(device_id)
            device_name = hass_device.get("name", "?") if hass_device else "?"
            config_url = hass_device.get("configuration_url", "?") if hass_device else "?"
            _LOGGER.info(
                "Setting up %s -> device_id=%s, name=%s, config_url=%s",
                entity_id,
                device_id,
                device_name,
                config_url,
            )
            success = await self._setup_player(entity_id, hass_device)
            if not success:
                self._pending_players[entity_id] = hass_device

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        if self._retry_task and not self._retry_task.done():
            self._retry_task.cancel()
        await super().unload(is_removed)

    async def _retry_pending_players(self) -> None:
        """Periodically retry connecting to devices that were offline at startup."""
        while self._pending_players:
            await asyncio.sleep(RETRY_INTERVAL)
            for player_key in list(self._pending_players.keys()):
                hass_device = self._pending_players[player_key]
                _LOGGER.debug("Retrying connection for %s", player_key)
                if hass_device is not None:
                    success = await self._setup_player(player_key, hass_device)
                else:
                    success = await self._setup_manual_player(player_key)
                if success:
                    del self._pending_players[player_key]
                    _LOGGER.info("Successfully connected to %s on retry", player_key)
        _LOGGER.info("All pending devices connected")

    async def _setup_player(
        self,
        entity_id: str,
        hass_device: HassDevice | None,
    ) -> bool:
        """Set up a player from an HA entity. Returns True on success."""
        # Extract host and port from configuration_url (e.g. "http://192.168.86.30:2323")
        config_url = hass_device.get("configuration_url", "") if hass_device else ""
        if not config_url:
            _LOGGER.warning("No configuration_url for %s, cannot connect directly", entity_id)
            return False
        parsed = urlparse(config_url)
        host = parsed.hostname
        port = str(parsed.port or 2323)
        if not host:
            _LOGGER.warning("Could not parse host from %s", config_url)
            return False
        # Create a direct REST API client
        client = DashieKioskClient(self.mass.http_session_no_ssl, host, port, password="")
        try:
            async with asyncio.timeout(15):
                await client.get_device_info()
        except Exception as err:
            _LOGGER.warning("Unable to connect to Dashie Kiosk at %s:%s - %s", host, port, err)
            return False
        # Collect device info from HA registry
        dev_info: dict[str, Any] = {}
        if hass_device:
            if model := hass_device.get("model"):
                dev_info["model"] = model
            if manufacturer := hass_device.get("manufacturer"):
                dev_info["manufacturer"] = manufacturer
            if sw_version := hass_device.get("sw_version"):
                dev_info["software_version"] = sw_version
        # Create and register the player
        player = DashieKioskPlayer(self, entity_id, client, f"{host}:{port}", dev_info)
        player.set_attributes()
        await self.mass.players.register(player)
        return True

    async def _setup_manual_player(self, address: str) -> bool:
        """Set up a player from a manual IP:port address. Returns True on success."""
        if ":" in address:
            host, port = address.rsplit(":", 1)
        else:
            host = address
            port = "2323"
        client = DashieKioskClient(self.mass.http_session_no_ssl, host, port, password="")
        try:
            async with asyncio.timeout(15):
                await client.get_device_info()
        except Exception as err:
            _LOGGER.warning("Unable to connect to Dashie Kiosk at %s:%s - %s", host, port, err)
            return False
        # Use the device ID from the device info, falling back to the address
        device_id = client.device_info.get("deviceID", address)
        player = DashieKioskPlayer(self, device_id, client, f"{host}:{port}")
        player.set_attributes()
        await self.mass.players.register(player)
        return True
