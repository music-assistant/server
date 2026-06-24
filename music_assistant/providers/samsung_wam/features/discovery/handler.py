"""Discovery handler."""

from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET
from http import HTTPStatus
from typing import TYPE_CHECKING, cast

import aiohttp
from aiohttp import ClientTimeout
from defusedxml import ElementTree as DefusedET
from pywam.speaker import Speaker

from music_assistant.constants import CONF_ENTRY_MANUAL_DISCOVERY_IPS
from music_assistant.providers.samsung_wam.features.base import WamProviderFeatureBase
from music_assistant.providers.samsung_wam.features.playback.models import WamSource
from music_assistant.providers.samsung_wam.features.state_sync.mapper import StateSyncMapper
from music_assistant.providers.samsung_wam.player import WamPlayer

from .consts import (
    PROBE_INTERVAL,
    PROBE_TASK_ID,
    PROBE_TIMEOUT,
    UPNP_DEVICE_DESCRIPTION_PATH,
    UPNP_PORT,
)

if TYPE_CHECKING:
    from music_assistant.providers.samsung_wam.provider import SamsungWamProvider


class DiscoveryHandler(WamProviderFeatureBase):
    """Coordinates finding and initializing speakers on the network."""

    def __init__(self, provider: SamsungWamProvider) -> None:
        """
        Initialize the discovery handler.

        :param provider: The SamsungWamProvider instance.
        """
        super().__init__(provider)
        self._discovery_locks: dict[str, asyncio.Lock] = {}

    async def start(self) -> None:
        """Start speaker discovery."""
        manual_ips: list[str] = (
            cast("list[str]", self.provider.config.get_value(CONF_ENTRY_MANUAL_DISCOVERY_IPS.key))
            or []
        )
        await self._probe_ips(manual_ips)
        self.mass.create_task(self._periodic_probe_task(), task_id=PROBE_TASK_ID)

    async def stop(self) -> None:
        """Stop speaker discovery."""
        self.mass.cancel_task(PROBE_TASK_ID)

    async def on_upnp_discovered(self, udn: str, ip_address: str) -> None:
        """
        Handle a UPnP/SSDP presence notification.

        :param udn: The Universal Device Name of the device.
        :param ip_address: The IP address of the discovered device.
        """
        # Skip if the player is already registered and available
        existing = self._get_player_by_udn(udn)
        if existing and existing.available:
            return

        # Probe the device to confirm model compatibility and obtain its canonical UDN,
        # which is sourced directly from the device description XML rather than SSDP headers
        if canonical_udn := await self.probe_ip(ip_address):
            await self._handle_presence(canonical_udn, ip_address)

    async def probe_ip(self, ip_address: str) -> str | None:
        """
        Probe a device to verify it is reachable and a supported WAM model.

        :param ip_address: The IP address to probe.
        :return: The device UDN, or None if the device is not reachable or unsupported.
        """
        location = f"http://{ip_address}:{UPNP_PORT}{UPNP_DEVICE_DESCRIPTION_PATH}"
        try:
            async with self.mass.http_session.get(
                location, timeout=ClientTimeout(total=PROBE_TIMEOUT)
            ) as resp:
                if resp.status != HTTPStatus.OK:
                    return None
                xml_text = await resp.text()

            root = DefusedET.fromstring(xml_text)

            model_el = root.find(".//{*}modelName")
            model_name = model_el.text if model_el is not None else None
            if model_name not in self.provider.supported_models:
                return None

            udn_el = root.find(".//{*}UDN")
            if udn_el is None or not udn_el.text:
                return None

            return str(udn_el.text.removeprefix("uuid:"))
        except TimeoutError, aiohttp.ClientError, ET.ParseError:
            return None

    async def _handle_presence(self, udn: str, ip_address: str) -> None:
        """
        Process a validated presence event for a device.

        :param udn: The canonical Universal Device Name of the device.
        :param ip_address: The IP address of the device.
        """
        lock = self._discovery_locks.setdefault(udn, asyncio.Lock())
        async with lock:
            existing = self._get_player_by_udn(udn)
            if existing:
                if not existing.available:
                    await existing.poll()
            else:
                await self._setup_player(udn, ip_address)

    async def _probe_ips(self, ips_to_probe: list[str]) -> None:
        """
        Actively probe a list of IP addresses.

        :param ips_to_probe: List of IP addresses to probe.
        """
        for ip in (ip for ip in ips_to_probe if ip):
            self.logger.debug("Probing %s", ip)
            if udn := await self.probe_ip(ip):
                await self._handle_presence(udn, ip)

    async def _periodic_probe_task(self) -> None:
        """Periodically probe manually configured IP addresses."""
        while not self.mass.closing:
            try:
                await asyncio.sleep(PROBE_INTERVAL)
                manual_ips: list[str] = (
                    cast(
                        "list[str]",
                        self.provider.config.get_value(CONF_ENTRY_MANUAL_DISCOVERY_IPS.key),
                    )
                    or []
                )
                if manual_ips:
                    await self._probe_ips(manual_ips)
            except asyncio.CancelledError:
                break
            except Exception as err:
                self.logger.warning("Periodic probe failed: %s", err, exc_info=err)

    async def _setup_player(self, udn: str, ip_address: str) -> None:
        """
        Initialize and register a newly discovered player.

        :param udn: The Universal Device Name of the device.
        :param ip_address: The IP address of the device.
        """
        self.logger.debug("Connecting to new player at %s", ip_address)

        temp_speaker = Speaker(ip_address)
        try:
            await temp_speaker.connect()
        except Exception as err:
            self.logger.warning("Failed to connect to player at %s: %s", ip_address, err)
            return

        try:
            await temp_speaker.update()
            attrs = StateSyncMapper.create_speaker_attributes(temp_speaker)
            if not attrs.mac:
                raise ConnectionError("Could not retrieve MAC address from speaker")
        except Exception as err:
            self.logger.warning("Failed to set up player at %s: %s", ip_address, err)
            await temp_speaker.disconnect()
            return

        if not self.mass.config.get_raw_player_config_value(attrs.mac, "enabled", True):
            self.logger.debug("Player at %s is disabled in configuration", ip_address)
            await temp_speaker.disconnect()
            return

        try:
            player = WamPlayer(self.provider, ip_address, udn, attrs.mac, temp_speaker)
            player.state_sync.apply_initial_state(attrs)

            await self.mass.players.register_or_update(player)
            player.state_sync.subscribe_speaker_events()
            self.provider.groups.register_player(player)

            if attrs.source and attrs.source not in (WamSource.WIFI, "Unknown"):
                # Set immediately if the speaker is already on an external input,
                # so the UI reflects the correct source on first register
                player.set_active_mass_source(attrs.source)
        except Exception as err:
            self.logger.warning("Failed to set up player at %s: %s", ip_address, err)
            await temp_speaker.disconnect()

    def _get_player_by_udn(self, udn: str) -> WamPlayer | None:
        """
        Retrieve an existing player by its UDN.

        :param udn: The Universal Device Name to search for.
        :return: The matching WamPlayer instance or None.
        """
        return next((p for p in self.players if p.udn == udn), None)
