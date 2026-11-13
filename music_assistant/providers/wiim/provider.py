"""WiiM/LinkPlay Player Provider implementation."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

from async_upnp_client.aiohttp import AiohttpSessionRequester
from async_upnp_client.client_factory import UpnpFactory
from async_upnp_client.exceptions import UpnpError
from pywiim import WiiMClient, WiiMError
from wiim import WiimController
from zeroconf import ServiceStateChange

from music_assistant.constants import CONF_ENTRY_MANUAL_DISCOVERY_IPS, VERBOSE_LOG_LEVEL
from music_assistant.helpers.util import (
    get_port_from_zeroconf,
    get_primary_ip_address_from_zeroconf,
)
from music_assistant.models.player_provider import PlayerProvider

from .constants import PLAYER_ID_PREFIX
from .grouping import NativeGroupCoordinator
from .helpers import is_official_device
from .linkplay_player import LinkPlayPlayer
from .player import WiimPlayer

if TYPE_CHECKING:
    from async_upnp_client.client import UpnpDevice
    from music_assistant_models.config_entries import ConfigEntry
    from zeroconf.asyncio import AsyncServiceInfo

    from music_assistant.models.player_protocol import PlayerProtocol

# UPnP description.xml ports used by LinkPlay devices, tried in addition to the
# mDNS-advertised port: 49152 serves the description, 59152 is the advertised UPnP port.
LINKPLAY_UPNP_PORTS = (49152, 59152)


class WiimProvider(PlayerProvider):
    """
    WiiM/LinkPlay player provider.

    Official WiiM and Audio Pro speakers are driven by the official WiiM SDK,
    while other compatible LinkPlay speakers (e.g. Edifier) are driven natively
    through the public pywiim API within this same provider instance.
    """

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to setup this provider."""
        return (CONF_ENTRY_MANUAL_DISCOVERY_IPS,)

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        # the sdk logs routine keep-alive chatter at INFO
        if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            logging.getLogger("wiim").setLevel(logging.DEBUG)
        else:
            logging.getLogger("wiim").setLevel(max(self.logger.level + 10, logging.WARNING))

        self.wiim_controller = WiimController(self.mass.http_session_no_ssl)
        # The single native multiroom topology authority shared by both backends.
        self.native_groups = NativeGroupCoordinator(self)
        # UPnP identity probe used to classify a discovered device (official WiiM/Audio Pro
        # vs a generic LinkPlay device such as Edifier). The description.xml is served over
        # plain HTTP; no UPnP eventing is used by the generic backend.
        requester = AiohttpSessionRequester(self.mass.http_session_no_ssl, with_sleep=True)
        self.upnp_factory = UpnpFactory(requester, non_strict=True)

    async def discover_players(self) -> None:
        """Discover the manually configured devices; mDNS discovers the others."""
        manual_ip_config: list[str] = cast(
            "list[str]", self.config.get_value(CONF_ENTRY_MANUAL_DISCOVERY_IPS.key)
        )
        for ip_address in manual_ip_config:
            stripped_ip_address = ip_address.strip()
            await self._discover_device(
                stripped_ip_address, self._candidate_locations(stripped_ip_address)
            )

    async def on_mdns_service_state_change(
        self, name: str, state_change: ServiceStateChange, info: AsyncServiceInfo | None
    ) -> None:
        """Handle MDNS service state callback."""
        if not info:
            return
        if state_change == ServiceStateChange.Removed:
            return  # ignore, rely on availability polling

        cur_address = get_primary_ip_address_from_zeroconf(info)
        if cur_address is None:
            return

        locations = self._candidate_locations(cur_address, get_port_from_zeroconf(info))

        # Try to get the protocol id from mDNS properties first (avoids a network call)
        udn = info.decoded_properties.get("uuid") if info.decoded_properties else None
        protocol_id = f"{PLAYER_ID_PREFIX}{udn}" if udn else None
        if protocol_id and (mass_player := self.get_protocol(protocol_id)):
            await self._handle_known_player_address(mass_player, cur_address, locations)
            mass_player.update_state()
            return
        if protocol_id and not self.mass.players.wants_protocol(protocol_id):
            return

        mac_address = info.decoded_properties.get("MAC") if info.decoded_properties else None
        # debounce: mDNS can fire several times in quick succession for one device
        self.mass.call_later(
            5,
            self._discover_device,
            cur_address,
            locations,
            mac_address,
            task_id=f"setup_wiim_{cur_address}",
        )

    async def try_add_player(
        self,
        protocol_id: str,
        ip_address: str,
        upnp_device: UpnpDevice,
        upnp_location: str,
        mac_address: str | None = None,
    ) -> None:
        """Add an official WiiM/Audio Pro device; it connects through the official SDK."""
        if not self.mass.players.wants_protocol(protocol_id):
            return
        try:
            player = WiimPlayer(
                provider=self,
                protocol_id=protocol_id,
                upnp_device=upnp_device,
                description_url=upnp_location,
                ip_address=ip_address,
                mac_address=mac_address,
            )
            await self.mass.players.register(player)
            if self.get_protocol(protocol_id) is not player:
                # the user disabled the player meanwhile
                return
            self.logger.info("WiiM player registered: %s (%s)", player.name, protocol_id)
        except Exception:
            self.logger.exception("Failed to register WiiM player %s", protocol_id)

    async def try_add_linkplay_player(
        self,
        protocol_id: str,
        ip_address: str,
        upnp_device: UpnpDevice,
        description_url: str,
        mac_address: str | None = None,
    ) -> None:
        """Add a generic LinkPlay device as the control protocol of its player."""
        if not self.mass.players.wants_protocol(protocol_id):
            return
        client = WiiMClient(ip_address, session=self.mass.http_session)
        try:
            # A successful call confirms the device speaks the LinkPlay API and yields the
            # device info primed on the protocol (used for native group join-mode selection),
            # so this stays the single authoritative probe done at discovery.
            device_info = await client.get_device_info_model()
        except WiiMError as err:
            self.logger.warning(
                "Device at %s is not a controllable LinkPlay device: %s", ip_address, err
            )
            return

        player = LinkPlayPlayer(
            provider=self,
            protocol_id=protocol_id,
            client=client,
            upnp_device=upnp_device,
            description_url=description_url,
            mac_address=mac_address,
            device_info=device_info,
        )
        await player.setup()
        await self.mass.players.register(player)
        # read the live topology now the protocol is attached (setup runs before the
        # attach, so the coordinator would discard a read taken there), then
        # reconcile so any leader that already lists this device, or that it leads,
        # self-heals regardless of discovery order.
        await self.native_groups.refresh_leader(player, force=True)
        self.native_groups.schedule_reconcile()
        self.logger.info("LinkPlay player registered: %s (%s)", player.name, protocol_id)

    def _candidate_locations(
        self, ip_address: str, advertised_port: int | None = None
    ) -> tuple[str, ...]:
        """Build the ordered, de-duplicated list of description.xml URLs to probe."""
        ports: list[int] = []
        if advertised_port:
            ports.append(advertised_port)
        ports.extend(LINKPLAY_UPNP_PORTS)
        locations: list[str] = []
        for port in ports:
            location = f"http://{ip_address}:{port}/description.xml"
            if location not in locations:
                locations.append(location)
        root = f"http://{ip_address}/description.xml"
        if root not in locations:
            locations.append(root)
        return tuple(locations)

    async def _probe_locations(
        self, locations: tuple[str, ...]
    ) -> tuple[UpnpDevice, str] | tuple[None, None]:
        """Probe candidate description URLs once and return the first reachable device."""
        for location in locations:
            try:
                upnp_device = await self.upnp_factory.async_create_device(location)
            except UpnpError:
                # transient/unreachable or wrong port; try the next candidate
                continue
            return upnp_device, location
        return None, None

    async def _discover_device(
        self,
        ip_address: str,
        locations: tuple[str, ...],
        mac_address: str | None = None,
    ) -> None:
        """Probe a device's UPnP identity once and route it to the right backend."""
        upnp_device, matched_location = await self._probe_locations(locations)
        if upnp_device is None or matched_location is None:
            # No reachable UPnP description; leave the backend undecided and retry later.
            return

        protocol_id = f"{PLAYER_ID_PREFIX}{upnp_device.udn}"
        if (existing := self.get_protocol(protocol_id)) is not None:
            # Already attached; the fast path may have missed, so still reconcile a
            # moved device here using the description we just probed.
            await self._reconcile_player_address(
                existing, ip_address, upnp_device, matched_location
            )
            return

        if is_official_device(upnp_device.manufacturer, upnp_device.model_name):
            await self.try_add_player(
                protocol_id, ip_address, upnp_device, matched_location, mac_address
            )
        else:
            await self.try_add_linkplay_player(
                protocol_id, ip_address, upnp_device, matched_location, mac_address
            )

    async def _handle_known_player_address(
        self, mass_player: PlayerProtocol, cur_address: str, locations: tuple[str, ...]
    ) -> None:
        """Reconcile an attached player with its current mDNS address."""
        if cur_address == mass_player.device_info.ip_address:
            return
        # Probe the new address so the shared reconciler can verify the device identity
        # before touching the player (works for both backends).
        upnp_device, matched_location = await self._probe_locations(locations)
        if upnp_device is not None and matched_location is not None:
            await self._reconcile_player_address(
                mass_player, cur_address, upnp_device, matched_location
            )

    async def _reconcile_player_address(
        self,
        mass_player: PlayerProtocol,
        cur_address: str,
        upnp_device: UpnpDevice,
        matched_location: str,
    ) -> None:
        """Apply an address change to an attached player."""
        if cur_address == mass_player.device_info.ip_address:
            return
        # Guard against a stale mDNS/DHCP update where the address now hosts a
        # different speaker: never bind this player to another device's UPnP identity.
        if f"{PLAYER_ID_PREFIX}{upnp_device.udn}" != mass_player.protocol_id:
            self.logger.warning(
                "Ignoring address update for %s: %s now hosts a different device (udn=%s)",
                mass_player.protocol_id,
                cur_address,
                upnp_device.udn,
            )
            return
        if isinstance(mass_player, LinkPlayPlayer):
            # The generic backend binds its address at construction, so a moved device
            # needs its HTTP + UPnP resources rebuilt against the new location.
            await mass_player.async_handle_address_change(
                cur_address, upnp_device, matched_location
            )
        elif isinstance(mass_player, WiimPlayer):
            # Official players self-heal a live connection; one still to set up (or set up
            # again) uses the new location.
            mass_player.set_location(cur_address, matched_location)
