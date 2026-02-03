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
from music_assistant.providers.samsung_wam.exceptions import PlayerDisabledError
from music_assistant.providers.samsung_wam.features.base import WamProviderFeatureBase
from music_assistant.providers.samsung_wam.features.state_sync.mapper import StateSyncMapper
from music_assistant.providers.samsung_wam.player import WamPlayer

from .consts import (
    PROBE_INTERVAL,
    PROBE_TASK_ID,
    PROBE_TIMEOUT,
    UPNP_DEVICE_DESCRIPTION_PATH,
    UPNP_PORT,
)
from .models import DiscoveryEventType, DiscoveryInfo, DiscoverySource, ProbeResult
from .ssdp_listener import DiscoverySsdpListener

if TYPE_CHECKING:
    from music_assistant.providers.samsung_wam.provider import SamsungWamProvider


class DiscoveryHandler(WamProviderFeatureBase):
    """Coordinates finding and initializing speakers on the network."""

    def __init__(self, provider: SamsungWamProvider) -> None:
        """Initialize the discovery handler.

        :param provider: The SamsungWamProvider instance.
        """
        super().__init__(provider)
        self._ssdp_listener = DiscoverySsdpListener(self._handle_ssdp_event, self.logger)
        self._discovery_locks: dict[str, asyncio.Lock] = {}
        self._discover_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start discovery mechanisms."""
        self._discover_task = self.mass.create_task(self._ssdp_listener.start())
        self.logger.debug("SSDP discovery listener started")

        manual_ips: list[str] = (
            cast("list[str]", self.provider.config.get_value(CONF_ENTRY_MANUAL_DISCOVERY_IPS.key))
            or []
        )
        await self._probe_ips(manual_ips, DiscoverySource.MANUAL)

        self.mass.create_task(self._periodic_probe_task(), task_id=PROBE_TASK_ID)

    async def stop(self) -> None:
        """Stop discovery mechanisms."""
        self.mass.cancel_task(PROBE_TASK_ID)
        if self._discover_task and not self._discover_task.done():
            self._discover_task.cancel()
        await self._ssdp_listener.stop()

    async def _handle_ssdp_event(self, info: DiscoveryInfo) -> None:
        """Process callback from SSDP infrastructure.

        :param info: Information about the discovery event.
        """
        if info.event_type == DiscoveryEventType.PRESENCE:
            # Re-probe to verify model compliance and alive status
            if await self.probe_ip(info.ip_address):
                await self.handle_discovery_event(info)
        else:
            await self.handle_discovery_event(info)

    async def probe_ip(self, ip_address: str) -> ProbeResult | None:
        """Perform an active probe to check if a device is online and valid.

        :param ip_address: The IP address to probe.
        :return: A ProbeResult if the device is valid, else None.
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
            model_name = (
                element.text if (element := root.find(".//{*}modelName")) is not None else "Unknown"
            )

            if model_name not in self.provider.supported_models:
                return None

            udn = element.text if (element := root.find(".//{*}UDN")) is not None else None
            if not udn:
                return None

            udn = udn.removeprefix("uuid:")
            return ProbeResult(udn=udn, model_name=model_name)
        except (TimeoutError, aiohttp.ClientError, ET.ParseError):
            return None

    async def handle_discovery_event(self, info: DiscoveryInfo) -> None:
        """Process a validated discovery event.

        :param info: The discovery details.
        """
        lock = self._discovery_locks.setdefault(info.udn, asyncio.Lock())
        async with lock:
            if info.event_type == DiscoveryEventType.OFFLINE:
                self.logger.debug("Received offline broadcast for %s", info.udn)
                return

            if info.event_type == DiscoveryEventType.PRESENCE:
                existing_player = self._get_player_by_udn(info.udn)
                if existing_player:
                    if not existing_player.available:
                        await existing_player.poll()
                else:
                    await self._setup_player(info)

    async def _probe_ips(self, ips_to_probe: list[str], source: DiscoverySource) -> None:
        """Actively probe a list of IP addresses.

        :param ips_to_probe: List of IP addresses to probe.
        :param source: The source of the discovery trigger.
        """
        for ip in (ip for ip in ips_to_probe if ip):
            self.logger.debug("Performing active probe for IP: %s (source: %s)", ip, source.value)
            if probe_result := await self.probe_ip(ip):
                info = DiscoveryInfo(
                    udn=probe_result.udn,
                    ip_address=ip,
                    event_type=DiscoveryEventType.PRESENCE,
                    discovery_source=source,
                )
                await self.handle_discovery_event(info)

    async def _periodic_probe_task(self) -> None:
        """Background task to periodically probe known manual IPs."""
        while not self.mass.closing:
            try:
                await asyncio.sleep(PROBE_INTERVAL)
                ips_to_check: set[str] = set()
                manual_ips: list[str] = (
                    cast(
                        "list[str]",
                        self.provider.config.get_value(CONF_ENTRY_MANUAL_DISCOVERY_IPS.key),
                    )
                    or []
                )
                ips_to_check.update(manual_ips)
                if ips_to_check:
                    await self._probe_ips(list(ips_to_check), DiscoverySource.MANUAL)
            except asyncio.CancelledError:
                break
            except Exception as err:
                self.logger.warning("Error in periodic probe task: %s", err, exc_info=err)

    async def _setup_player(self, info: DiscoveryInfo) -> None:
        """Initialize and register a newly discovered player.

        :param info: Information about the discovered device.
        """
        self.logger.debug("Pre-flight connection to new player at %s", info.ip_address)

        temp_speaker = Speaker(info.ip_address)
        await temp_speaker.connect()

        try:
            await temp_speaker.update()

            attrs = StateSyncMapper.create_speaker_attributes(temp_speaker)
            if not attrs.mac:
                raise ConnectionError("Failed to get MAC address during pre-flight.")

            if not self.mass.config.get_raw_player_config_value(attrs.mac, "enabled", True):
                raise PlayerDisabledError("Player disabled in configuration.")

            player = WamPlayer(self.provider, info.ip_address, info.udn, attrs.mac, temp_speaker)
            player.state_sync.apply_initial_state(attrs)

            await self.mass.players.register_or_update(player)
            self.players[player.player_id] = player
            self.provider.groups.register_player(player)

        except PlayerDisabledError:
            self.logger.debug("Player at %s is disabled in configuration.", info.ip_address)
            await temp_speaker.disconnect()
        except Exception as err:
            self.logger.warning("Failed to set up player at %s: %s", info.ip_address, err)
            await temp_speaker.disconnect()

    def _get_player_by_udn(self, udn: str) -> WamPlayer | None:
        """Retrieve an existing player by its UDN.

        :param udn: The Universal Device Name to search for.
        :return: The matching WamPlayer instance or None.
        """
        return next((p for p in self.players.values() if p.udn == udn), None)
