"""Sony SongPal player provider."""

from __future__ import annotations

import asyncio
import ipaddress
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType
from songpal import Device, SongpalException
from songpal.discovery import Discover

from music_assistant.models.player_provider import PlayerProvider

from .avtransport import fetch_avtransport_control_url
from .constants import (
    AVTRANSPORT_ST,
    CONF_MANUAL_ENDPOINTS,
    CONNECT_TIMEOUT,
    REDISCOVERY_INTERVAL,
    SSDP_TIMEOUT,
)
from .player import SongpalPlayer
from .util import (
    endpoint_key,
    is_bravia_device,
    parse_manual_endpoints,
    player_id_from_macs,
)

if TYPE_CHECKING:
    from songpal.discovery import DiscoveredDevice


@dataclass(slots=True)
class _Candidate:
    """A SongPal endpoint found by SSDP or entered by hand."""

    endpoint: str
    name: str | None = None
    upnp_location: str | None = None


class SongpalPlayerProvider(PlayerProvider):
    """Discover Sony SongPal devices and register one player per device."""

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        """Set up discovery state."""
        super().__init__(*args, **kwargs)
        self._players: dict[str, SongpalPlayer] = {}
        self._discovery_task: asyncio.Task[None] | None = None
        self._discover_lock = asyncio.Lock()

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return the manual endpoint list for devices SSDP misses."""
        return (
            ConfigEntry(
                key=CONF_MANUAL_ENDPOINTS,
                type=ConfigEntryType.STRING,
                label="Manual endpoints",
                required=False,
                default_value="",
                description=(
                    "One SongPal endpoint URL per line, for devices SSDP does not find. "
                    "Example: http://192.168.1.20:10000/sony"
                ),
            ),
        )

    async def loaded_in_mass(self) -> None:
        """Start rediscovery after the provider is loaded."""
        self._discovery_task = self.mass.create_task(self._discovery_loop())

    async def unload(self, is_removed: bool = False) -> None:
        """Stop discovery and disconnect every player."""
        del is_removed
        task = self._discovery_task
        self._discovery_task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        for player in list(self._players.values()):
            await player.close()
            await self.mass.players.unregister(player.player_id)
        self._players.clear()

    async def discover_players(self) -> None:
        """Find SongPal devices over SSDP and connect any manual endpoints."""
        async with self._discover_lock:
            candidates = await self._collect_candidates()
            hosts_needing_transport: set[str] = set()
            control_urls: dict[str, str | None] = {}
            for candidate in candidates:
                control_url = None
                if candidate.upnp_location:
                    control_url = await fetch_avtransport_control_url(
                        self.mass.http_session_no_ssl, candidate.upnp_location
                    )
                control_urls[candidate.endpoint] = control_url
                if control_url is None:
                    host = urlsplit(candidate.endpoint).hostname
                    if host:
                        hosts_needing_transport.add(host)
            if hosts_needing_transport:
                found = await self._search_avtransport(hosts_needing_transport)
                for candidate in candidates:
                    if control_urls[candidate.endpoint]:
                        continue
                    host = urlsplit(candidate.endpoint).hostname
                    if host and host in found:
                        control_urls[candidate.endpoint] = found[host]
            for candidate in candidates:
                try:
                    await self._register_candidate(
                        candidate, control_urls.get(candidate.endpoint)
                    )
                except Exception:
                    self.logger.exception(
                        "Failed to set up SongPal device %s", candidate.endpoint
                    )

    async def _discovery_loop(self) -> None:
        while True:
            try:
                await self.discover_players()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("SongPal discovery failed")
            await asyncio.sleep(REDISCOVERY_INTERVAL)

    async def _collect_candidates(self) -> list[_Candidate]:
        found: list[DiscoveredDevice] = []

        async def on_device(device: DiscoveredDevice) -> None:
            found.append(device)

        try:
            await Discover.discover(SSDP_TIMEOUT, callback=on_device)
        except Exception:
            self.logger.exception("SongPal SSDP discovery failed")

        candidates: list[_Candidate] = []
        seen: set[str] = set()
        for device in found:
            if is_bravia_device(device.services):
                self.logger.debug("Skipping Bravia device %s", device.name)
                continue
            key = endpoint_key(device.endpoint)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                _Candidate(
                    endpoint=device.endpoint,
                    name=device.name,
                    upnp_location=device.upnp_location,
                )
            )
        for endpoint in self._manual_endpoints():
            key = endpoint_key(endpoint)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(_Candidate(endpoint=endpoint))
        return candidates

    def _manual_endpoints(self) -> list[str]:
        raw = self.config.get_value(CONF_MANUAL_ENDPOINTS)
        if not isinstance(raw, str):
            return []
        endpoints = parse_manual_endpoints(raw)
        skipped = [
            line.strip()
            for line in raw.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        if len(endpoints) != len(skipped):
            self.logger.warning("Ignoring manual SongPal endpoints that are not HTTP URLs")
        return endpoints

    async def _search_avtransport(self, hosts: set[str]) -> dict[str, str]:
        """SSDP-search AVTransport and keep control URLs for the given hosts."""
        from async_upnp_client.search import async_search

        found: dict[str, str] = {}

        async def on_device(device: dict) -> None:
            location = device.get("location")
            if not isinstance(location, str):
                return
            host = urlsplit(location).hostname
            if host not in hosts or host in found:
                return
            control_url = await fetch_avtransport_control_url(
                self.mass.http_session_no_ssl, location
            )
            if control_url:
                found[host] = control_url

        try:
            await async_search(
                timeout=SSDP_TIMEOUT,
                search_target=AVTRANSPORT_ST,
                async_callback=on_device,
            )
        except Exception:
            self.logger.exception("AVTransport discovery failed")
        return found

    async def _register_candidate(
        self, candidate: _Candidate, control_url: str | None
    ) -> None:
        device = Device(candidate.endpoint)
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT):
                await device.get_supported_methods()
                interface = await device.get_interface_information()
        except (SongpalException, TimeoutError) as err:
            self.logger.warning("Cannot connect to %s: %s", candidate.endpoint, err)
            return
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT):
                sysinfo = await device.get_system_info()
        except (SongpalException, TimeoutError) as err:
            self.logger.warning("Cannot read system info from %s: %s", candidate.endpoint, err)
            return
        player_id = player_id_from_macs(sysinfo.macAddr, sysinfo.wirelessMacAddr)
        if not player_id:
            self.logger.warning("SongPal device %s has no usable MAC address", candidate.endpoint)
            return
        name = candidate.name or interface.modelName or candidate.endpoint
        ip_address = await _ip_for_endpoint(candidate.endpoint)
        existing = self._players.get(player_id)
        if existing is None:
            current = self.mass.players.get_player(player_id)
            if isinstance(current, SongpalPlayer) and current.provider is self:
                existing = current
                self._players[player_id] = current
            elif current is not None:
                self.logger.debug(
                    "Player id %s belongs to another provider", player_id
                )
                return
        if existing is not None:
            await existing.update_connection(
                endpoint=device.endpoint,
                control_url=control_url,
                name=name,
                ip_address=ip_address,
            )
            return
        player = SongpalPlayer(
            self,
            player_id,
            device,
            name,
            endpoint=device.endpoint,
            control_url=control_url,
            ip_address=ip_address,
        )
        await player.prepare()
        await self.mass.players.register(player)
        self._players[player_id] = player
        player.update_state()
        await player.start_listening()


async def _ip_for_endpoint(endpoint: str) -> str | None:
    host = urlsplit(endpoint).hostname
    if not host:
        return None
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    try:
        infos = await asyncio.getaddrinfo(host, None)
    except OSError:
        return None
    if not infos:
        return None
    return infos[0][4][0]
