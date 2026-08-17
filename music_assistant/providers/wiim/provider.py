"""WiiM/LinkPlay Player Provider implementation."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

from async_upnp_client.aiohttp import AiohttpSessionRequester
from async_upnp_client.client_factory import UpnpFactory
from async_upnp_client.exceptions import UpnpError
from music_assistant_models.enums import IdentifierType
from pywiim import WiiMClient, WiiMError
from wiim import WiimController
from wiim.discovery import async_create_wiim_device
from wiim.exceptions import WiimDeviceException, WiimRequestException
from zeroconf import ServiceStateChange

from music_assistant.constants import CONF_ENTRY_MANUAL_DISCOVERY_IPS, VERBOSE_LOG_LEVEL
from music_assistant.helpers.util import (
    get_port_from_zeroconf,
    get_primary_ip_address_from_zeroconf,
)
from music_assistant.models.player_provider import PlayerProvider

from .constants import PLAYER_ID_PREFIX
from .grouping import NativeGroupCoordinator
from .helpers import is_official_manufacturer
from .linkplay_player import LinkPlayPlayer
from .player import WiimPlayer

if TYPE_CHECKING:
    from async_upnp_client.client import UpnpDevice
    from music_assistant_models.config_entries import ConfigEntry
    from zeroconf.asyncio import AsyncServiceInfo

    from music_assistant.models.player import Player

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

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        manual_ip_config: list[str] = cast(
            "list[str]", self.config.get_value(CONF_ENTRY_MANUAL_DISCOVERY_IPS.key)
        )
        for ip_address in manual_ip_config:
            stripped_ip_address = ip_address.strip()
            await self._discover_device(
                stripped_ip_address, "Unknown", self._candidate_locations(stripped_ip_address)
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

        # Try to get player_id from mDNS properties first (avoids a network call)
        udn = info.decoded_properties.get("uuid") if info.decoded_properties else None
        player_id = f"{PLAYER_ID_PREFIX}{udn}" if udn else None
        if player_id and (mass_player := self.mass.players.get_player(player_id)):
            await self._handle_known_player_address(mass_player, cur_address, locations)
            self.mass.players.trigger_player_update(player_id)
            return

        mac_address = info.decoded_properties.get("MAC") if info.decoded_properties else None
        # debounce: mDNS can fire several times in quick succession for one device
        self.mass.call_later(
            5,
            self._discover_device,
            cur_address,
            name,
            locations,
            mac_address,
            task_id=f"setup_wiim_{cur_address}",
        )

    async def try_add_player(
        self,
        player_id: str,
        ip_address: str,
        name: str,
        upnp_location: str,
        mac_address: str | None = None,
    ) -> None:
        """Add an official WiiM/Audio Pro device via the official SDK."""
        try:
            wiim_dev = await async_create_wiim_device(
                upnp_location,
                self.mass.http_session_no_ssl,
                host=ip_address,
                local_host=await self.mass.streams.get_source_ip(ip_address),
                polling_interval=60,
            )
        except (WiimRequestException, WiimDeviceException) as err:
            self.logger.warning("Failed to initialize WiiM device at %s: %s", ip_address, err)
            return
        except Exception:
            self.logger.exception("Unexpected error initializing WiiM device at %s", ip_address)
            return

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
            # a newly registered leader may already list this device, and this device may
            # lead others: reconcile so discovery-order misses on either side self-heal.
            self.native_groups.schedule_reconcile()
            self.logger.info("WiiM player registered: %s (%s)", wiim_dev.name, player_id)
        except Exception:
            self.logger.exception("Failed to register WiiM player %s", wiim_dev.name)
            await self.wiim_controller.remove_device(wiim_dev.udn)
            await wiim_dev.disconnect()

    async def try_add_linkplay_player(
        self,
        player_id: str,
        ip_address: str,
        upnp_device: UpnpDevice,
        description_url: str,
        mac_address: str | None = None,
    ) -> None:
        """Add a generic LinkPlay device as a grouping/identity shell."""
        client = WiiMClient(ip_address, session=self.mass.http_session)
        try:
            # A successful call confirms the device speaks the LinkPlay API and yields the
            # device info primed on the shell (used for native group join-mode selection),
            # so this stays the single authoritative probe done at discovery.
            device_info = await client.get_device_info_model()
        except WiiMError as err:
            self.logger.warning(
                "Device at %s is not a controllable LinkPlay device: %s", ip_address, err
            )
            return

        player = LinkPlayPlayer(
            provider=self,
            player_id=player_id,
            client=client,
            upnp_device=upnp_device,
            description_url=description_url,
            mac_address=mac_address,
            device_info=device_info,
        )
        await player.setup()
        await self.mass.players.register_or_update(player)
        # a newly registered leader may already list this device, and this device may lead
        # others: reconcile so discovery-order misses on either side self-heal.
        self.native_groups.schedule_reconcile()
        self.logger.info("LinkPlay player registered: %s (%s)", player.name, player_id)

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
        name: str,
        locations: tuple[str, ...],
        mac_address: str | None = None,
    ) -> None:
        """Probe a device's UPnP identity once and route it to the right backend."""
        upnp_device, matched_location = await self._probe_locations(locations)
        if upnp_device is None or matched_location is None:
            # No reachable UPnP description; leave the backend undecided and retry later.
            return

        player_id = f"{PLAYER_ID_PREFIX}{upnp_device.udn}"
        if (existing := self.mass.players.get_player(player_id)) is not None:
            # Already registered; the fast path may have missed, so still reconcile a
            # moved device here using the description we just probed.
            await self._reconcile_player_address(
                existing, ip_address, upnp_device, matched_location
            )
            return

        if is_official_manufacturer(upnp_device.manufacturer):
            await self.try_add_player(player_id, ip_address, name, matched_location, mac_address)
        else:
            await self.try_add_linkplay_player(
                player_id, ip_address, upnp_device, matched_location, mac_address
            )

    async def _handle_known_player_address(
        self, mass_player: Player, cur_address: str, locations: tuple[str, ...]
    ) -> None:
        """Reconcile an already-registered player with its current mDNS address."""
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
        self, mass_player: Player, cur_address: str, upnp_device: UpnpDevice, matched_location: str
    ) -> None:
        """Apply an address change to an already-registered player."""
        if cur_address == mass_player.device_info.ip_address:
            return
        # Guard against a stale mDNS/DHCP update where the address now hosts a
        # different speaker: never bind this player to another device's UPnP identity.
        if f"{PLAYER_ID_PREFIX}{upnp_device.udn}" != mass_player.player_id:
            self.logger.warning(
                "Ignoring address update for %s: %s now hosts a different device (udn=%s)",
                mass_player.player_id,
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
        else:
            # Official players self-heal their connection; only refresh their identifier.
            mass_player.device_info.add_identifier(IdentifierType.IP_ADDRESS, cur_address)
