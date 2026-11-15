"""DLNA Player Provider."""

import asyncio
import time
from dataclasses import replace
from ipaddress import AddressValueError, IPv4Address
from typing import TYPE_CHECKING, cast
from urllib.parse import urlsplit

from aiohttp import ClientError
from async_upnp_client.aiohttp import AiohttpSessionRequester
from async_upnp_client.client_factory import UpnpFactory
from async_upnp_client.exceptions import UpnpError
from async_upnp_client.search import async_search
from music_assistant_models.player import DeviceInfo

from music_assistant.constants import CONF_ENTRY_MANUAL_DISCOVERY_IPS
from music_assistant.helpers.json import SerializableType
from music_assistant.models.player_provider import PlayerProvider

from .helpers import DLNANotifyServer
from .player import DLNAPlayer

if TYPE_CHECKING:
    from async_upnp_client.client import UpnpRequester
    from async_upnp_client.utils import CaseInsensitiveDict
    from music_assistant_models.config_entries import ConfigEntry, ConfigValueType

SSDP_PORT = 1900
# Manual devices are not discovered automatically, so try to (re)connect periodically
MANUAL_RESCAN_INTERVAL = 60


class DLNAPlayerProvider(PlayerProvider):
    """DLNA Player provider."""

    _ignored_udns: set[str]
    _manual_udns: dict[str, str]
    _manual_rescan_task_id: str

    lock: asyncio.Lock
    requester: UpnpRequester
    upnp_factory: UpnpFactory
    notify_server: DLNANotifyServer

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider."""
        return (replace(CONF_ENTRY_MANUAL_DISCOVERY_IPS, validate=_validate_manual_addresses),)

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.lock = asyncio.Lock()
        self._ignored_udns = set()
        self._manual_udns = {}
        self._manual_rescan_task_id = f"dlna_manual_rescan_{self.instance_id}"
        self.requester = AiohttpSessionRequester(self.mass.http_session, with_sleep=True)
        self.upnp_factory = UpnpFactory(self.requester, non_strict=True)
        self.notify_server = DLNANotifyServer(self.requester, self.mass)

    async def unload(self, is_removed: bool = False) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        """
        # A rescan that already started runs as a task under the same id, so cancel both
        self.mass.cancel_timer(self._manual_rescan_task_id)
        self.mass.cancel_task(self._manual_rescan_task_id)
        self.mass.streams.unregister_dynamic_route("/notify", "NOTIFY")
        self._ignored_udns = set()

    async def discover_players(self) -> None:
        """Connect manually configured devices that multicast SSDP discovery cannot find."""
        addresses = self._get_manual_addresses()
        if not addresses:
            return
        results = await asyncio.gather(
            *(self._discover_by_manual_address(address) for address in addresses),
            return_exceptions=True,
        )
        for address, result in zip(addresses, results, strict=True):
            if isinstance(result, Exception):
                self.logger.warning(
                    "Error connecting manual DLNA device %s", address, exc_info=result
                )
        self.mass.call_later(
            MANUAL_RESCAN_INTERVAL, self.discover_players, task_id=self._manual_rescan_task_id
        )

    async def get_diagnostics(self) -> dict[str, SerializableType]:
        """Return diagnostics info for this provider to include in diagnostics reports."""
        now = time.time()
        devices: list[SerializableType] = [
            {
                "model": player.device_info.model,
                "manufacturer": player.device_info.manufacturer,
                "available": player.available,
                "eventing": not player.force_poll,
                "last_seen_age_sec": round(now - player.last_seen),
            }
            for player in self.protocols
            if isinstance(player, DLNAPlayer)
        ]
        return {
            "ignored_devices": len(self._ignored_udns),
            "devices": devices,
        }

    async def on_upnp_service_discovered(
        self, search_target: str, discovery_info: CaseInsensitiveDict
    ) -> None:
        """Handle SSDP discovery callbacks."""
        del search_target
        if not (parsed := _parse_discovery_info(discovery_info)):
            return
        udn, description_url = parsed
        await self._device_discovered(udn, description_url)

    def _get_manual_addresses(self) -> list[str]:
        """Return the configured manual addresses (IP addresses or description URLs)."""
        addresses = cast(
            "list[str]", self.config.get_value(CONF_ENTRY_MANUAL_DISCOVERY_IPS.key) or []
        )
        return list(dict.fromkeys(address.strip() for address in addresses if address.strip()))

    async def _discover_by_manual_address(self, address: str) -> None:
        """Discover a device by an IPv4 address or description URL."""
        if _is_description_url(address):
            await self._discover_by_description_url(address)
        else:
            await self._discover_by_ip_address(address)

    def _is_manual_player_connected(self, address: str) -> bool:
        """Return whether the player using a manual address is already set up and available."""
        if not (udn := self._manual_udns.get(address)):
            return False
        player = self.get_protocol(udn)
        return player is not None and player.available

    async def _discover_by_ip_address(self, ip_address: str) -> None:
        """Find a device by sending a unicast SSDP search to its IP address."""
        if self._is_manual_player_connected(ip_address):
            return

        async def on_response(discovery_info: CaseInsensitiveDict) -> None:
            if not (parsed := _parse_discovery_info(discovery_info)):
                return
            udn, description_url = parsed
            self._manual_udns[ip_address] = udn
            try:
                await self._device_discovered(udn, description_url)
            except (UpnpError, ClientError, TimeoutError) as err:
                self.logger.debug("Manual DLNA device %s not reachable: %r", ip_address, err)

        try:
            await async_search(
                on_response,
                timeout=4,
                search_target="urn:schemas-upnp-org:device:MediaRenderer:1",
                target=(ip_address, SSDP_PORT),
            )
        except OSError as err:
            self.logger.debug("Unicast SSDP search to %s failed: %r", ip_address, err)

    async def _discover_by_description_url(self, description_url: str) -> None:
        """Set up a device using its description URL."""
        if self._is_manual_player_connected(description_url):
            return
        try:
            if not (udn := self._manual_udns.get(description_url)):
                upnp_device = await self.upnp_factory.async_create_device(description_url)
                udn = upnp_device.udn
                self._manual_udns[description_url] = udn
            await self._device_discovered(udn, description_url)
        except (UpnpError, ClientError, TimeoutError) as err:
            self.logger.debug(
                "Manual DLNA device %s not reachable by description URL: %r", description_url, err
            )

    async def _device_discovered(self, udn: str, description_url: str) -> None:
        """Handle discovered DLNA player."""
        async with self.lock:
            # skip devices that we've already determined should be ignored
            if udn in self._ignored_udns:
                return

            if dlna_player := self.get_protocol(udn):
                # existing player
                assert isinstance(dlna_player, DLNAPlayer)
                if dlna_player.description_url == description_url and dlna_player.available:
                    # nothing to do, device is already connected
                    return
                # update description url to newly discovered one
                dlna_player.description_url = description_url
            else:
                # new player detected, setup our DLNAPlayer wrapper
                # ignore disabled players
                if not self.mass.players.wants_protocol(udn):
                    self.logger.debug("Ignoring disabled player: %s", udn)
                    return

                dlna_player = DLNAPlayer(
                    provider=self,
                    protocol_id=udn,
                    description_url=description_url,
                )
                # will be updated later when device connects
                dlna_player._attr_device_info = DeviceInfo(
                    model="unknown",
                    manufacturer="unknown",
                )

            # Setup will return False if the device should be ignored (e.g., passive speaker)
            if not await dlna_player.setup():
                self._ignored_udns.add(udn)


def _is_description_url(address: str) -> bool:
    """Return whether a manual address is a valid http(s) description URL."""
    parts = urlsplit(address)
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def _is_valid_manual_address(address: str) -> bool:
    """Return whether a manual address is a valid IPv4 address or description URL."""
    if _is_description_url(address):
        return True
    try:
        IPv4Address(address)
    except AddressValueError:
        return False
    return True


def _validate_manual_addresses(value: ConfigValueType) -> bool:
    """Return whether all manual addresses are valid (empty addresses are ignored)."""
    if not isinstance(value, list):
        return False
    return all(
        isinstance(address, str)
        and (not address.strip() or _is_valid_manual_address(address.strip()))
        for address in value
    )


def _parse_discovery_info(discovery_info: CaseInsensitiveDict) -> tuple[str, str] | None:
    """Return (udn, description URL) of an SSDP discovery message, or None."""
    ssdp_st: str | None = discovery_info.get("st", discovery_info.get("nt"))
    if not ssdp_st or "MediaRenderer" not in ssdp_st:
        return None
    ssdp_usn: str | None = discovery_info.get("usn")
    if not ssdp_usn:
        return None
    ssdp_udn: str | None = discovery_info.get("_udn")
    if not ssdp_udn and ssdp_usn.startswith("uuid:"):
        ssdp_udn = ssdp_usn.split("::")[0]
    if not ssdp_udn:
        return None
    description_url: str | None = discovery_info.get("location")
    if not description_url:
        return None
    return ssdp_udn, description_url
