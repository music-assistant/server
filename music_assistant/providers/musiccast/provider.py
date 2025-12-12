"""MusicCast Provider."""

import asyncio
import logging
from dataclasses import dataclass

from aiohttp.client_exceptions import ClientError
from aiomusiccast.musiccast_device import MusicCastDevice
from music_assistant_models.config_entries import ProviderConfig
from music_assistant_models.enums import ProviderFeature
from music_assistant_models.provider import ProviderManifest
from zeroconf import ServiceStateChange
from zeroconf.asyncio import AsyncServiceInfo

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.mass import MusicAssistant
from music_assistant.models.player_provider import PlayerProvider
from music_assistant.providers.musiccast.constants import (
    MC_DEVICE_INFO_ENDPOINT,
    MC_DEVICE_UPNP_ENDPOINT,
    MC_DEVICE_UPNP_PORT,
    PLAYER_ZONE_SPLITTER,
)
from music_assistant.providers.sonos.helpers import get_primary_ip_address

from .musiccast import MusicCastController, MusicCastPhysicalDevice, MusicCastZoneDevice
from .player import MusicCastPlayer, UpnpUpdateHelper


@dataclass(kw_only=True)
class MusicCastPlayerHelper:
    """MusicCastPlayerHelper.

    Helper class to store MA player alongside physical device.
    """

    device_id: str  # device_id without ZONE_SPLITTER zone
    player_main: MusicCastPlayer | None = None  # mass player
    player_zone2: MusicCastPlayer | None = None  # mass player
    # I can only test up to zone 2
    player_zone3: MusicCastPlayer | None = None  # mass player
    player_zone4: MusicCastPlayer | None = None  # mass player

    # log allowed sources for a device with multiple sources once. see "_handle_zone_grouping"
    _log_allowed_sources: bool = True

    physical_device: MusicCastPhysicalDevice

    def get_player(self, zone: str) -> MusicCastPlayer | None:
        """Get Player by zone name."""
        match zone:
            case "main":
                return self.player_main
            case "zone2":
                return self.player_zone2
            case "zone3":
                return self.player_zone3
            case "zone4":
                return self.player_zone4
        raise RuntimeError(f"Zone {zone} is unknown.")

    def get_all_players(self) -> list[MusicCastPlayer]:
        """Get all players."""
        assert self.player_main is not None  # we always have main
        players = [self.player_main]
        if self.player_zone2 is not None:
            players.append(self.player_zone2)
        if self.player_zone3 is not None:
            players.append(self.player_zone3)
        if self.player_zone4 is not None:
            players.append(self.player_zone4)
        return players


class MusicCastProvider(PlayerProvider):
    """MusicCast Player Provider."""

    # poll upnp playback information, but not too often. see "_update_player_attributes"
    # player_id: UpnpUpdateHelper
    upnp_update_helper: dict[str, UpnpUpdateHelper] = {}

    # str here is the device id, NOT the player_id
    update_player_locks: dict[str, asyncio.Lock] = {}

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        supported_features: set[ProviderFeature],
    ) -> None:
        """Init."""
        super().__init__(mass, manifest, config, supported_features)
        # str is device_id here:
        self.musiccast_player_helpers: dict[str, MusicCastPlayerHelper] = {}

    async def unload(self, is_removed: bool = False) -> None:
        """Call on unload."""
        for mc_player in self.mass.players.all(provider_filter=self.instance_id):
            assert isinstance(mc_player, MusicCastPlayer)  # for type checking
            mc_player.physical_device.remove()

    async def handle_async_init(self) -> None:
        """Async init."""
        self.mc_controller = MusicCastController(logger=self.logger)
        # aiomusiccast logs all fetch requests after udp message as debug.
        # same approach as in upnp
        if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            logging.getLogger("aiomusiccast").setLevel(logging.DEBUG)
        else:
            logging.getLogger("aiomusiccast").setLevel(self.logger.level + 10)

    async def on_mdns_service_state_change(
        self, name: str, state_change: ServiceStateChange, info: AsyncServiceInfo | None
    ) -> None:
        """Discovery via mdns."""
        if state_change == ServiceStateChange.Removed:
            # Wait for connection to fail, same as sonos.
            return
        if info is None:
            return
        device_ip = get_primary_ip_address(info)
        if device_ip is None:
            return
        try:
            device_info = await self.mass.http_session.get(
                f"http://{device_ip}/{MC_DEVICE_INFO_ENDPOINT}", raise_for_status=True
            )
            device_info_json = await device_info.json()
        except ClientError:
            # typical Errors are
            # ClientResponseError -> raise_for_status
            # ClientConnectorError -> unable to connect/ not existing/ timeout
            # ContentTypeError -> device returns something, but is not json
            # but we can use the base exception class, as we only check
            # if the device is suitable
            return
        device_id = device_info_json.get("device_id")
        if device_id is None:
            return
        description_url = f"http://{device_ip}:{MC_DEVICE_UPNP_PORT}/{MC_DEVICE_UPNP_ENDPOINT}"

        _check = await self.mass.http_session.get(description_url)
        if _check.status == 404:
            self.logger.debug("Missing description url for Yamaha device at %s", device_ip)
            return
        await self._device_discovered(
            device_id=device_id, device_ip=device_ip, description_url=description_url
        )

    async def _device_discovered(
        self, device_id: str, device_ip: str, description_url: str
    ) -> None:
        """Handle discovered MusicCast player."""
        # verify that this is a MusicCast player
        check: bool = await MusicCastDevice.check_yamaha_ssdp(
            description_url, self.mass.http_session
        )
        if not check:
            return

        if self.mass.players.get(device_id) is not None:
            return
        mc_player_known = self.musiccast_player_helpers.get(device_id)
        if mc_player_known is not None and (
            mc_player_known.player_main is not None
            and mc_player_known.physical_device.device.device.upnp_description == description_url
            and mc_player_known.player_main.available
        ):
            # nothing to do, device is already connected
            return
        else:
            # new or updated player detected
            physical_device = MusicCastPhysicalDevice(
                device=MusicCastDevice(
                    client=self.mass.http_session,
                    ip=device_ip,
                    upnp_description=description_url,
                ),
                controller=self.mc_controller,
            )
            self.update_player_locks[device_id] = asyncio.Lock()
            success = await physical_device.async_init()  # fetch + polling
            if not success:
                self.logger.debug(
                    "Had trouble setting up device at %s. Will be retried on next discovery.",
                    device_ip,
                )
                return
            await self._register_player(physical_device, device_id)

    async def _register_player(
        self, physical_device: MusicCastPhysicalDevice, device_id: str
    ) -> None:
        """Register player including zones."""

        # player features
        # NOTE: There is seek in the upnp desc
        # http://{ip}:49154/AVTransport/desc.xml
        # however, it appears not to work as it should, so we remain at MA's own
        # seek implementation
        def get_player(zone_name: str, zone_device: MusicCastZoneDevice) -> MusicCastPlayer:
            return MusicCastPlayer(
                provider=self,
                player_id=f"{device_id}{PLAYER_ZONE_SPLITTER}{zone_name}",
                physical_device=physical_device,
                zone_device=zone_device,
            )

        main_device = physical_device.zone_devices.get("main")
        if (
            main_device is None
            or main_device.zone_data is None
            or main_device.zone_data.name is None
        ):
            return

        musiccast_player_helper = MusicCastPlayerHelper(
            device_id=device_id,
            physical_device=physical_device,
        )

        for zone_name, zone_device in physical_device.zone_devices.items():
            if zone_device.zone_data is None or zone_device.zone_data.name is None:
                continue
            player = get_player(zone_name, zone_device=zone_device)
            await player.setup()
            await self.mass.players.register_or_update(player)
            physical_device.register_callback(player._non_async_udp_callback)
            setattr(musiccast_player_helper, f"player_{zone_device.zone_name}", player)

        if (
            musiccast_player_helper.player_zone2 is not None
            and musiccast_player_helper._log_allowed_sources
        ):
            musiccast_player_helper._log_allowed_sources = False
            player_main = musiccast_player_helper.player_main
            assert player_main is not None
            self.logger.info(
                f"The player {player_main.display_name or player_main.name} has multiple zones. "
                "Please use the player config to configure a non-net source for grouping. "
            )

        self.musiccast_player_helpers[device_id] = musiccast_player_helper
