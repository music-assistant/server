"""WiiM Player Provider implementation."""

from __future__ import annotations

import asyncio
import logging
import socket
from ipaddress import ip_address as parse_ip_address
from typing import TYPE_CHECKING, cast

from music_assistant_models.enums import IdentifierType
from wiim import WiimController
from wiim.discovery import async_create_wiim_device, verify_wiim_device
from wiim.exceptions import WiimDeviceException, WiimRequestException
from zeroconf import ServiceStateChange

from music_assistant.constants import CONF_ENTRY_MANUAL_DISCOVERY_IPS, VERBOSE_LOG_LEVEL
from music_assistant.helpers.util import (
    get_port_from_zeroconf,
    get_primary_ip_address_from_zeroconf,
)
from music_assistant.models.player_provider import PlayerProvider

from .constants import PLAYER_ID_PREFIX
from .player import WiimPlayer

if TYPE_CHECKING:
    from zeroconf.asyncio import AsyncServiceInfo

    from music_assistant.mass import MusicAssistant


async def _resolve_callback_host(mass: MusicAssistant, target_ip: str) -> str:
    """
    Resolve the local IP for the WiiM device's UPnP event callbacks.

    The WiiM SDK binds its UPnP notify server to this address and advertises it
    back to the device as the callback host, so it must be BOTH a locally
    bindable interface address AND reachable by the device. ``streams.publish_ip``
    is the *advertised* stream address and is not necessarily locally bindable
    (a container behind a published IP, or a multi-homed host), so binding the
    notify server to it fails with ``EADDRNOTAVAIL``. Resolve the source IP per
    device via the routing table instead, mirroring the AirPlay provider's
    ``resolve_if_ip`` helper. Falls back to ``publish_ip`` (the previous
    behaviour) when the lookup is inconclusive, so this is never worse than before.
    """
    # Honour an explicitly configured, concrete stream bind IP first.
    bind_ip = str(mass.streams.bind_ip)
    if bind_ip not in ("0.0.0.0", "::", ""):
        return bind_ip

    def _routing_lookup() -> str:
        try:
            is_ipv6 = parse_ip_address(target_ip).version == 6
        except ValueError:
            is_ipv6 = False
        family = socket.AF_INET6 if is_ipv6 else socket.AF_INET
        route_target: tuple[str, int] | tuple[str, int, int, int] = (
            (target_ip, 80, 0, 0) if is_ipv6 else (target_ip, 80)
        )
        with socket.socket(family, socket.SOCK_DGRAM) as sock:
            try:
                sock.settimeout(1.0)
                sock.connect(route_target)
                routed_ip = str(sock.getsockname()[0])
                if routed_ip and routed_ip not in ("0.0.0.0", "::", ""):
                    return routed_ip
            except OSError:
                pass
        return ""

    if routed := await asyncio.to_thread(_routing_lookup):
        return routed
    return str(mass.streams.publish_ip or "") or bind_ip


class WiimProvider(PlayerProvider):
    """
    WiiM player provider.

    This provides a WiiM player implementation for Music Assistant.
    """

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            logging.getLogger("wiim").setLevel(logging.DEBUG)
        else:
            logging.getLogger("wiim").setLevel(self.logger.level + 10)

        self.wiim_controller = WiimController(self.mass.http_session_no_ssl)

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        self.logger.info("WiimProvider loaded")

        manual_ip_config: list[str] = cast(
            "list[str]", self.config.get_value(CONF_ENTRY_MANUAL_DISCOVERY_IPS.key)
        )

        for ip_address in manual_ip_config:
            stripped_ip_address = ip_address.strip()
            potential_locations = (
                f"http://{stripped_ip_address}:49152/description.xml",
                f"http://{stripped_ip_address}/description.xml",
            )

            matched_location = None
            upnp_device = None
            for location in potential_locations:
                upnp_device = await verify_wiim_device(location, self.mass.http_session_no_ssl)
                if upnp_device:
                    matched_location = location
                    break

            if not upnp_device or not matched_location:
                continue

            player_id = f"{PLAYER_ID_PREFIX}{upnp_device.udn}"
            await self.try_add_player(player_id, stripped_ip_address, "Unknown", matched_location)

    async def on_mdns_service_state_change(
        self, name: str, state_change: ServiceStateChange, info: AsyncServiceInfo | None
    ) -> None:
        """Handle MDNS service state callback."""
        if not info:
            self.logger.debug("mDNS callback: no info, returning")
            return

        if state_change == ServiceStateChange.Removed:
            return  # ignore, rely on SDK availability polling

        cur_address = get_primary_ip_address_from_zeroconf(info)
        self.logger.debug("mDNS callback: cur_address=%s", cur_address)
        if cur_address is None:
            return

        # Try to get player_id from mDNS properties first (avoids network call)
        udn = info.decoded_properties.get("uuid") if info.decoded_properties else None
        wiim_player_id = f"{PLAYER_ID_PREFIX}{udn}" if udn else None

        # Check for existing player before hitting the network
        if wiim_player_id and (mass_player := self.mass.players.get_player(wiim_player_id)):
            assert isinstance(mass_player, WiimPlayer), (
                "Player ID already exists but is not a WiimPlayer"
            )
            if cur_address and cur_address != mass_player.device_info.ip_address:
                self.logger.debug(
                    "Address updated to %s for player %s", cur_address, mass_player.display_name
                )
                mass_player.device_info.add_identifier(IdentifierType.IP_ADDRESS, cur_address)
            self.mass.players.trigger_player_update(wiim_player_id)
            return

        # New device -- verify it's a WiiM and set up
        self.logger.debug("mDNS callback: new device, verifying at %s", cur_address)
        potential_locations = (
            f"http://{cur_address}:{get_port_from_zeroconf(info)}/description.xml",
            f"http://{cur_address}/description.xml",
            f"http://{cur_address}:49152/description.xml",
        )

        matched_location = None
        upnp_device = None
        for location in potential_locations:
            upnp_device = await verify_wiim_device(location, self.mass.http_session_no_ssl)
            if upnp_device:
                matched_location = location
                break

        if not upnp_device or not matched_location:
            self.logger.debug("mDNS callback: verify_wiim_device failed for %s", cur_address)
            return

        player_id = f"{PLAYER_ID_PREFIX}{upnp_device.udn}"

        # Extract MAC address from mDNS properties for protocol linking
        mac_address: str | None = None
        if info.decoded_properties:
            mac_address = info.decoded_properties.get("MAC")

        self.logger.debug("Discovered device %s on %s (MAC: %s)", name, cur_address, mac_address)
        task_id = f"setup_wiim_{player_id}"
        self.mass.call_later(
            5,
            self.try_add_player,
            player_id,
            cur_address,
            name,
            matched_location,
            mac_address,
            task_id=task_id,
        )

    async def try_add_player(
        self,
        player_id: str,
        ip_address: str,
        name: str,
        upnp_location: str,
        mac_address: str | None = None,
    ) -> None:
        """Try to add a WiiM device as a player."""
        try:
            wiim_dev = await async_create_wiim_device(
                upnp_location,
                self.mass.http_session_no_ssl,
                host=ip_address,
                local_host=await _resolve_callback_host(self.mass, ip_address),
                polling_interval=60,
            )
        except (WiimRequestException, WiimDeviceException) as err:
            self.logger.warning("Failed to initialize WiiM device at %s: %s", ip_address, err)
            return
        except Exception:
            self.logger.exception("Unexpected error initializing WiiM device at %s", ip_address)
            return

        self.logger.debug("try_add_player: device created: %s (%s)", wiim_dev.name, wiim_dev.udn)
        await self.wiim_controller.add_device(wiim_dev)
        try:
            player = WiimPlayer(
                provider=self,
                player_id=player_id,
                device=wiim_dev,
                mac_address=mac_address,
            )
            await player.setup()
            await self.mass.players.register_or_update(player)
            self.logger.info("WiiM player registered: %s (%s)", wiim_dev.name, player_id)
        except Exception:
            self.logger.exception("Failed to register WiiM player %s", wiim_dev.name)
            await self.wiim_controller.remove_device(wiim_dev.udn)
            await wiim_dev.disconnect()
