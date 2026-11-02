"""MusicCast Provider."""

import asyncio
import logging
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

from aiohttp.client_exceptions import ClientError
from aiomusiccast.musiccast_device import MusicCastDevice
from zeroconf import ServiceStateChange

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.helpers.util import format_ip_for_url
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
from .player import MusicCastPlayer

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.provider import ProviderManifest
    from zeroconf.asyncio import AsyncServiceInfo


@dataclass(kw_only=True)
class MusicCastPlayerHelper:
    """
    MusicCastPlayerHelper.

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


class MusicCastProvider(PlayerProvider):
    """MusicCast Player Provider."""

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
        # per device_id, serializes device (re)registration with re-enabling single players
        self._registration_locks: dict[str, asyncio.Lock] = {}

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider."""
        return ()

    async def unload(self, is_removed: bool = False) -> None:
        """Call on unload."""
        # all devices of the controller, also those still being registered
        for physical_device in list(self.mc_controller.physical_devices):
            physical_device.remove()

    def on_player_enabled(self, player_id: str) -> None:
        """Call (by config manager) when a player gets enabled."""
        self.mass.create_task(self._register_enabled_player(player_id))

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
            async with self.mass.http_session.get(
                f"http://{format_ip_for_url(device_ip)}/{MC_DEVICE_INFO_ENDPOINT}",
                raise_for_status=True,
            ) as device_info:
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
        description_url = (
            f"http://{format_ip_for_url(device_ip)}:{MC_DEVICE_UPNP_PORT}/{MC_DEVICE_UPNP_ENDPOINT}"
        )

        try:
            async with self.mass.http_session.get(description_url) as _check:
                if _check.status == 404:
                    self.logger.debug("Missing description url for Yamaha device at %s", device_ip)
                    return
        except ClientError:
            return
        await self._device_discovered(
            device_id=device_id, device_ip=device_ip, description_url=description_url
        )

    async def _device_discovered(
        self, device_id: str, device_ip: str, description_url: str
    ) -> None:
        """Handle discovered MusicCast player."""
        # verify that this is a MusicCast player
        try:
            check: bool = await MusicCastDevice.check_yamaha_ssdp(
                description_url, self.mass.http_session
            )
        except ClientError:
            return
        if not check:
            return

        async with self._registration_locks.setdefault(device_id, asyncio.Lock()):
            mc_player_known = self.musiccast_player_helpers.get(device_id)
            if mc_player_known is not None and (
                mc_player_known.player_main is not None
                and mc_player_known.physical_device.device.device.upnp_description
                == description_url
                and mc_player_known.player_main.available
            ):
                # nothing to do, device is already connected
                return
            # new or updated player detected
            physical_device = MusicCastPhysicalDevice(
                device=MusicCastDevice(
                    client=self.mass.http_session,
                    ip=device_ip,
                    upnp_description=description_url,
                ),
                controller=self.mc_controller,
            )
            try:
                success = await physical_device.async_init()  # fetch + polling
                if success:
                    await self._register_player(physical_device, device_id)
            except BaseException:
                physical_device.remove()
                raise
            if not success:
                physical_device.remove()
                self.logger.debug(
                    "Had trouble setting up device at %s. Will be retried on next discovery.",
                    device_ip,
                )

    async def _register_enabled_player(self, player_id: str) -> None:
        device_id, _, zone_name = player_id.partition(PLAYER_ZONE_SPLITTER)
        async with self._registration_locks.setdefault(device_id, asyncio.Lock()):
            # a re-enable still queued must not register players for an unloaded instance
            if self.unloading:
                return
            # a known device only lacks the player, which got unregistered when disabled
            musiccast_player_helper = self.musiccast_player_helpers.get(device_id)
            zone_device = (
                musiccast_player_helper.physical_device.zone_devices.get(zone_name)
                if musiccast_player_helper is not None
                else None
            )
            if (
                musiccast_player_helper is None
                or zone_device is None
                or zone_device.zone_data is None
                or zone_device.zone_data.name is None
            ):
                super().on_player_enabled(player_id)
                return
            if self.mass.players.get_player(player_id) is None:
                player = await self._setup_zone_player(musiccast_player_helper, zone_device)
                await self.mass.players.register_or_update(player)
                setattr(musiccast_player_helper, f"player_{zone_device.zone_name}", player)

    async def _register_player(
        self, physical_device: MusicCastPhysicalDevice, device_id: str
    ) -> None:
        """Register player including zones."""
        main_device = physical_device.zone_devices.get("main")
        if (
            main_device is None
            or main_device.zone_data is None
            or main_device.zone_data.name is None
        ):
            physical_device.remove()
            return

        musiccast_player_helper = MusicCastPlayerHelper(
            device_id=device_id,
            physical_device=physical_device,
        )

        # set up every zone before registering any, so a failing zone leaves no player
        # registered on this device
        players = [
            await self._setup_zone_player(musiccast_player_helper, zone_device)
            for zone_device in physical_device.zone_devices.values()
            if zone_device.zone_data is not None and zone_device.zone_data.name is not None
        ]
        for player in players:
            await self.mass.players.register_or_update(player)
            setattr(musiccast_player_helper, f"player_{player.zone_device.zone_name}", player)
            physical_device.register_callback(partial(self._on_udp_update, player.player_id))

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

        if (mc_player_known := self.musiccast_player_helpers.get(device_id)) is not None:
            # the stale device must not stay in the controller next to its replacement
            mc_player_known.physical_device.remove()
        self.musiccast_player_helpers[device_id] = musiccast_player_helper

    async def _setup_zone_player(
        self, musiccast_player_helper: MusicCastPlayerHelper, zone_device: MusicCastZoneDevice
    ) -> MusicCastPlayer:
        # player features
        # NOTE: There is seek in the upnp desc
        # http://{ip}:49154/AVTransport/desc.xml
        # however, it appears not to work as it should, so we remain at MA's own
        # seek implementation
        player = MusicCastPlayer(
            provider=self,
            player_id=f"{musiccast_player_helper.device_id}{PLAYER_ZONE_SPLITTER}{zone_device.zone_name}",
            physical_device=musiccast_player_helper.physical_device,
            zone_device=zone_device,
        )
        await player.setup()
        return player

    def _on_udp_update(self, player_id: str, physical_device: MusicCastPhysicalDevice) -> None:
        # resolved per update: a re-enabled player replaces the disabled one without a new callback
        player = self.mass.players.get_player(player_id)
        if isinstance(player, MusicCastPlayer) and player.physical_device is physical_device:
            player._non_async_udp_callback(physical_device)
