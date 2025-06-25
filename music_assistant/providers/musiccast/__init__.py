"""MusicCast for MusicAssistant."""

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from aiohttp.client import ClientError
from aiomusiccast.exceptions import MusicCastGroupException
from aiomusiccast.musiccast_device import MusicCastDevice
from aiomusiccast.pyamaha import MusicCastConnectionException
from music_assistant_models.config_entries import ConfigEntry, ConfigValueType, ProviderConfig
from music_assistant_models.enums import PlayerFeature, ProviderFeature
from music_assistant_models.player import PlayerMedia
from music_assistant_models.provider import ProviderManifest
from zeroconf import ServiceStateChange
from zeroconf.asyncio import AsyncServiceInfo

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.mass import MusicAssistant
from music_assistant.models import ProviderInstanceType
from music_assistant.models.player import Player
from music_assistant.models.player_provider import PlayerProvider
from music_assistant.providers.musiccast.avt_helpers import (
    avt_next,
    avt_pause,
    avt_play,
    avt_previous,
    avt_set_url,
    avt_stop,
)
from music_assistant.providers.musiccast.constants import (
    CONF_PLAYER_SWITCH_SOURCE_NON_NET,
    CONF_PLAYER_TURN_OFF_ON_LEAVE,
    MC_DEVICE_INFO_ENDPOINT,
    MC_DEVICE_UPNP_ENDPOINT,
    MC_DEVICE_UPNP_PORT,
    MC_NETUSB_SOURCE_IDS,
    MC_SOURCE_MAIN_SYNC,
    MC_SOURCE_MC_LINK,
    PLAYER_ZONE_SPLITTER,
)
from music_assistant.providers.musiccast.musiccast import (
    MusicCastController,
    MusicCastPhysicalDevice,
    MusicCastZoneDevice,
)
from music_assistant.providers.sonos.helpers import get_primary_ip_address


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return MusicCast(mass, manifest, config)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    # ruff: noqa: ARG001
    return ()


@dataclass(kw_only=True)
class UpnpUpdateHelper:
    """UpnpUpdateHelper.

    See _update_player_attributes.
    """

    last_poll: float  # time.time
    controlled_by_mass: bool
    current_uri: str | None


class MusicCastPlayer(Player):
    """MusicCastPlayer in Music Assistant."""

    def __init__(
        self,
        provider: PlayerProvider,
        player_id: str,
        physical_device: MusicCastPhysicalDevice,
        zone_device: MusicCastZoneDevice,
    ) -> None:
        """Init MC Player.

        Keep reference to physical and zone device.
        """
        super().__init__(provider, player_id)
        self.physical_device = physical_device
        self.zone_device = zone_device

        # make this a property and update during normal state updates?
        # refers to being controlled by upnp.
        self.update_lock = asyncio.Lock()
        self.upnp_update_helper: UpnpUpdateHelper | None = None

    async def _cmd_run(self, fun: Callable[..., Coroutine[Any, Any, None]], *args: Any) -> None:
        """Help function for all player cmds."""
        try:
            await fun(*args)
        except MusicCastConnectionException:
            # should go to provider here.
            # await self._set_player_unavailable(player_id)
            self.logger.debug("Player became unavailable.")
        except MusicCastGroupException:
            # can happen, user shall try again.
            ...

    async def _handle_zone_grouping(self, zone_player: MusicCastZoneDevice) -> None:
        """Handle zone grouping.

        If a device has multiple zones, only a single zone can be net controlled.
        If another zone wants to join the group, the current net zone has to switch
        its input to a non-net one and optionally turn off.

        This methods targets another zone of this players physical device!
        """
        # this is not this player's id
        player_id = self._get_player_id_from_mc_zone_player(zone_player)
        assert player_id is not None  # for TYPE_CHECKING
        _source = str(
            await self.mass.config.get_player_config_value(
                player_id, CONF_PLAYER_SWITCH_SOURCE_NON_NET
            )
        )
        # verify that this source actually exists and is non net
        _allowed_sources = self._get_allowed_sources_zone_switch(zone_player)
        mass_player = self.mass.players.get(player_id)
        assert mass_player is not None
        if _source not in _allowed_sources:
            msg = (
                "The switch source you specified for "
                f"{mass_player.display_name or mass_player.name}"
                " is not allowed. "
                f"The source must be any of: {', '.join(sorted(_allowed_sources))} "
                "Will use the first available source."
            )
            self.logger.error(msg)
            _source = _allowed_sources.pop()

        await mass_player.select_source(_source)
        _turn_off = bool(
            await self.mass.config.get_player_config_value(player_id, CONF_PLAYER_TURN_OFF_ON_LEAVE)
        )
        if _turn_off:
            await asyncio.sleep(2)
            await mass_player.power(powered=False)

    def _get_player_id_from_mc_zone_player(self, zone_player: MusicCastZoneDevice) -> str:
        device_id = zone_player.physical_device.device.data.device_id
        assert device_id is not None
        return f"{device_id}{PLAYER_ZONE_SPLITTER}{zone_player.zone_name}"

    def _get_allowed_sources_zone_switch(self, zone_player: MusicCastZoneDevice) -> set[str]:
        """Return non net sources for a zone player."""
        assert zone_player.zone_data is not None, "zone data missing"
        _input_sources: set[str] = set(zone_player.zone_data.input_list)
        _net_sources = set(MC_NETUSB_SOURCE_IDS)
        _net_sources.add(MC_SOURCE_MC_LINK)  # mc grouping source
        _net_sources.add(MC_SOURCE_MAIN_SYNC)  # main zone sync
        return _input_sources.difference(_net_sources)

    async def power(self, powered: bool) -> None:
        """Power command."""
        if powered:
            await self._cmd_run(self.zone_device.turn_on)
        else:
            await self._cmd_run(self.zone_device.turn_off)

    async def volume_set(self, volume_level: int) -> None:
        """Volume set command."""
        return await super().volume_set(volume_level)

    async def volume_mute(self, muted: bool) -> None:
        """Volume mute command."""
        return await super().volume_mute(muted)

    async def play(self) -> None:
        """Play command."""
        if self.upnp_update_helper is not None and self.upnp_update_helper.controlled_by_mass:
            await avt_play(self.mass.http_session, self.physical_device)
        else:
            await self._cmd_run(self.zone_device.play)

    async def stop(self) -> None:
        """Stop command."""
        if self.upnp_update_helper is not None and self.upnp_update_helper.controlled_by_mass:
            await avt_stop(self.mass.http_session, self.physical_device)
        else:
            await self._cmd_run(self.zone_device.stop)

    async def pause(self) -> None:
        """Pause command."""
        if self.upnp_update_helper is not None and self.upnp_update_helper.controlled_by_mass:
            await avt_pause(self.mass.http_session, self.physical_device)
        else:
            await self._cmd_run(self.zone_device.pause)

    async def next_track(self) -> None:
        """Next command."""
        if self.upnp_update_helper is not None and self.upnp_update_helper.controlled_by_mass:
            await avt_next(self.mass.http_session, self.physical_device)
        else:
            await self._cmd_run(self.zone_device.next_track)

    async def previous_track(self) -> None:
        """Previous command."""
        if self.upnp_update_helper is not None and self.upnp_update_helper.controlled_by_mass:
            await avt_previous(self.mass.http_session, self.physical_device)
        else:
            await self._cmd_run(self.zone_device.previous_track)

    async def play_media(self, media: PlayerMedia) -> None:
        """Play media command."""
        if len(self.physical_device.zone_devices) > 1:
            # zone handling
            # only a single zone may have netusb capability
            for zone_name, dev in self.physical_device.zone_devices.items():
                if zone_name == self.zone_device.zone_name:
                    continue
                if dev.is_netusb:
                    await self._handle_zone_grouping(dev)
        device_id, _ = self.player_id.split(PLAYER_ZONE_SPLITTER)
        async with self.update_lock:
            # just in case
            if self.zone_device.source_id != "server":
                await self.select_source("server")
            await avt_set_url(self.mass.http_session, self.physical_device, player_media=media)
            await avt_play(self.mass.http_session, self.physical_device)

            self.upnp_update_helper = UpnpUpdateHelper(
                last_poll=time.time(),
                controlled_by_mass=True,
                current_uri=media.uri,
            )

            # do I need these two lines still?
            self.set_current_media(uri=media.uri, clear_all=True)
            await self.mass.players.register_or_update(self)

    async def enqueue_next_media(self, media: PlayerMedia) -> None:
        """Enqueue next command."""
        await avt_set_url(
            self.mass.http_session,
            self.physical_device,
            player_media=media,
            enqueue=True,
        )

    async def select_source(self, source: str) -> None:
        """Select source command."""
        await self._cmd_run(self.zone_device.select_source, source)

    async def group_with(self, target_player_id: str) -> None:
        """Group command.

        If we are a child, this is called.
        In MusicCast, we only need the call on the server.
        """
        return

    async def ungroup(self) -> None:
        """Ungroup command."""
        if self.zone_device.zone_name.startswith("zone"):
            # We are are zone.
            # We do not leave an MC group, but just change our source.
            await self._handle_zone_grouping(self.zone_device)
            return
        await self._cmd_run(self.zone_device.unjoin_player)

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Set multiple members.

        If we are a server, this is called.
        We can ignore removed devices, these are handled via ungroup individually.
        """

    async def poll(self) -> None:
        """Poll player."""
        return await super().poll()

    async def get_config_entries(self) -> list[ConfigEntry]:
        """Get player config entries."""
        return await super().get_config_entries()

    # async def on_unload(self) -> None:
    #     """Unload player."""
    #     return super().on_unload()


class MusicCast(PlayerProvider):
    """MusicCast Player Provider."""

    # poll upnp playback information, but not too often. see "_update_player_attributes"
    # player_id: UpnpUpdateHelper
    upnp_update_helper: dict[str, UpnpUpdateHelper] = {}

    # str here is the device id, NOT the player_id
    update_player_locks: dict[str, asyncio.Lock] = {}

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        return {ProviderFeature.SYNC_PLAYERS}

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
        except ClientError:
            # typical Errors are
            # ClientResponseError -> raise_for_status
            # ClientConnectorError -> unable to connect/ not existing/ timeout
            # but we can use the base exception class, as we only check
            # if the device is suitable
            return
        device_info_json = await device_info.json()
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
        # if (
        #     mc_player_known is not None
        #     and mc_player_known.player_main is not None
        #     and (
        #         mc_player_known.physical_device.device.device.upnp_description == description_url
        #         and mc_player_known.player_main.available
        #     )
        # ):
        #     # nothing to do, device is already connected
        #     return
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
            physical_device.register_callback(self._non_async_udp_callback)
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
        supported_features = {
            PlayerFeature.VOLUME_SET,
            PlayerFeature.VOLUME_MUTE,
            PlayerFeature.PAUSE,
            PlayerFeature.POWER,
            PlayerFeature.SELECT_SOURCE,
            PlayerFeature.SET_MEMBERS,
            PlayerFeature.NEXT_PREVIOUS,
            PlayerFeature.ENQUEUE,
            PlayerFeature.GAPLESS_PLAYBACK,
        }

        def get_player(zone_name: str, zone_device: MusicCastZoneDevice) -> MusicCastPlayer:
            player = MusicCastPlayer(
                provider=self,
                player_id=f"{device_id}{PLAYER_ZONE_SPLITTER}{zone_name}",
                physical_device=physical_device,
                zone_device=zone_device,
            )
            player.supported_features.update(supported_features)
            player.device_info.manufacturer = "Yamaha Corporation"
            player.device_info.model = physical_device.device.data.model_name or "unknown model"
            player.device_info.software_version = (
                physical_device.device.data.system_version or "unknown version"
            )
            return player

        main_device = physical_device.zone_devices.get("main")
        if (
            main_device is None
            or main_device.zone_data is None
            or main_device.zone_data.name is None
        ):
            return

        for zone_name, zone_device in physical_device.zone_devices.items():
            if zone_device.zone_data is None or zone_device.zone_data.name is None:
                continue
            player = get_player(zone_name, zone_device=zone_device)

            # player._attr_name = zone_device.zone_data.name
            # await self._update_player_attributes(player, zone_device)
            await self.mass.players.register_or_update(player)

        # if musiccast_player.player_zone2 is not None and musiccast_player._log_allowed_sources:
        #     musiccast_player._log_allowed_sources = False
        #     player_main = musiccast_player.player_main
        #     assert player_main is not None
        #     self.logger.info(
        #         f"The player {player_main.display_name or player_main.name} has multiple zones. "
        #         "Please use the player config to configure a non-net source for grouping. "
        #     )
        #
        # self.musiccast_players[device_id] = musiccast_player

    # async def _update_player_attributes(self, player: Player, device: MusicCastZoneDevice) ->
    # None:
    #     # ruff: noqa: PLR0915
    #     zone_data = device.zone_data
    #     if zone_data is None:
    #         return
    #
    #     player.name = zone_data.name or "UNKNOWN NAME"
    #     player.powered = zone_data.power == "on"
    #
    #     # NOTE: aiomusiccast does not type hint the volume variables, and they may
    #     # be none, and not only integers
    #     _current_volume = cast("int | None", zone_data.current_volume)
    #     _max_volume = cast("int | None", zone_data.max_volume)
    #     _min_volume = cast("int | None", zone_data.min_volume)
    #     if _current_volume is None:
    #         player.volume_level = None
    #     else:
    #         _min_volume = 0 if _min_volume is None else _min_volume
    #         _max_volume = 100 if _max_volume is None else _max_volume
    #         if _min_volume == _max_volume:
    #             _max_volume += 1
    #         player.volume_level = int(_current_volume / (_max_volume - _min_volume) * 100)
    #     player.volume_muted = zone_data.mute
    #
    #     # STATE
    #
    #     match device.state:
    #         case MusicCastPlayerState.PAUSED:
    #             player.state = PlaybackState.PAUSED
    #         case MusicCastPlayerState.PLAYING:
    #             player.state = PlaybackState.PLAYING
    #         case MusicCastPlayerState.IDLE | MusicCastPlayerState.OFF:
    #             player.state = PlaybackState.IDLE
    #     player.elapsed_time = device.media_position
    #     player.elapsed_time_last_updated = device.media_position_updated_at
    #
    #     # SOURCES
    #     source_list: list[PlayerSource] = []
    #     for source_id, source_name in device.source_mapping.items():
    #         control = source_id in MC_CONTROL_SOURCE_IDS
    #         passive = source_id in MC_PASSIVE_SOURCE_IDS
    #         source_list.append(
    #             PlayerSource(
    #                 id=source_id,
    #                 name=source_name,
    #                 passive=passive,
    #                 can_play_pause=control,
    #                 can_seek=False,
    #                 can_next_previous=control,
    #             )
    #         )
    #     player.source_list.set(source_list)
    #
    #     # UPDATE UPNP HELPER
    #     update_helper = self.upnp_update_helper.get(player.player_id)
    #     now = time.time()
    #     if update_helper is None or now - update_helper.last_poll > 5:
    #         # Let's not do this too often
    #         # Note: The devices always return the last UPnP xmls, even if
    #         # currently another source/ playback method is used
    #         try:
    #             _xml_media_info = await avt_get_media_info(
    #                 self.mass.http_session, device.physical_device
    #             )
    #         except ServerDisconnectedError:
    #             return
    #         _player_current_url = search_xml(_xml_media_info, "CurrentURI")
    #
    #         # controlled by mass is only True, if we are directly controlled
    #         # i.e. we are not a group member.
    #         # the device's source id is server, if controlled by upnp, but also, if the internal
    #         # dlna function of the device are used. As a fallback, we then
    #         # use the item's title. This can only fail, if our current and next item
    #         # has the same name as the external.
    #         controlled_by_mass = False
    #         if _player_current_url is not None:
    #             controlled_by_mass = (
    #                 player.player_id in _player_current_url
    #                 and self.mass.streams.base_url in _player_current_url
    #                 and device.source_id == "server"
    #             )
    #
    #         update_helper = UpnpUpdateHelper(
    #             last_poll=now,
    #             controlled_by_mass=controlled_by_mass,
    #             current_uri=_player_current_url,
    #         )
    #
    #         self.upnp_update_helper[player.player_id] = update_helper
    #
    #     # UPDATE PLAYBACK INFORMATION
    #     # Note to self:
    #     # player.current_media tells queue controller what is playing
    #     # and player.set_current_media is the helper function
    #     # do not access the queue controller to gain playback information here
    #     if update_helper.current_uri is not None and update_helper.controlled_by_mass:
    #         player.set_current_media(uri=update_helper.current_uri, clear_all=True)
    #     elif device.is_client:
    #         _server = device.group_server
    #         _server_id = self._get_player_id_from_mc_zone_player(_server)
    #         _server_update_helper = self.upnp_update_helper.get(_server_id)
    #         if (
    #             _server_update_helper is not None
    #             and _server_update_helper.current_uri is not None
    #             and _server_update_helper.controlled_by_mass
    #         ):
    #             player.set_current_media(
    #                 uri=_server_update_helper.current_uri,
    #             )
    #         else:
    #             player.set_current_media(
    #                 uri=f"{_server_id}_{_server.source_id}",
    #                 title=_server.media_title,
    #                 artist=_server.media_artist,
    #                 album=_server.media_album_name,
    #                 image_url=_server.media_image_url,
    #             )
    #     else:
    #         player.set_current_media(
    #             uri=f"{player.player_id}_{device.source_id}",
    #             title=device.media_title,
    #             artist=device.media_artist,
    #             album=device.media_album_name,
    #             image_url=device.media_image_url,
    #         )
    #
    #     # SOURCE
    #     player.active_source = None  # means the player controller will figure it out
    #     if not device.is_client and not update_helper.controlled_by_mass:
    #         player.active_source = device.source_id
    #     elif device.is_client:
    #         _server = device.group_server
    #         _server_id = self._get_player_id_from_mc_zone_player(_server)
    #         if _server_update_helper := self.upnp_update_helper.get(_server_id):
    #             player.active_source = (
    #                 device.source_id if not _server_update_helper.controlled_by_mass else None
    #             )
    #
    #     # GROUPING
    #     # A zone cannot be synced to another zone or main of the same device.
    #     # Additionally, a zone can only be synced, if main is currently not using any netusb
    #     # function.
    #     # For a Zone which will be synced to main, grouping emits a "main_sync" instead
    #     # of a mc link. The other way round, we log a warning.
    #     player.can_group_with = {self.instance_id}
    #
    #     if len(device.musiccast_group) == 1:
    #         if device.musiccast_group[0] == device:
    #             # we are in a group with ourselves.
    #             player.group_members.clear()
    #             player.synced_to = None
    #             player.active_group = None
    #
    #     elif not device.is_client and not device.is_server:
    #         player.group_members.clear()
    #         player.synced_to = None
    #         player.active_group = None
    #
    #     elif device.is_client:
    #         _synced_to_id = self._get_player_id_from_mc_zone_player(device.group_server)
    #         player.group_members.clear()
    #         player.synced_to = _synced_to_id
    #         player.active_group = _synced_to_id
    #
    #     elif device.is_server:
    #         player.group_members.set(
    #             [self._get_player_id_from_mc_zone_player(x) for x in device.musiccast_group]
    #         )
    #         player.synced_to = None
    #         player.active_group = None
    #
    def _non_async_udp_callback(self, mc_physical_device: MusicCastPhysicalDevice) -> None:
        """Update callback.

        This is called if there are new UDP updates. Unfortunately, aiomusiccast
        only allows a sync callback, so we schedule an async task.
        """
        return
        # mc_player: MusicCastPlayer | None = None
        # for mc_player in self.musiccast_players.values():
        #     if mc_player.physical_device == mc_physical_device:
        #         break
        # assert mc_player is not None  # for type checking
        # if mc_player.player_main is None:
        #     return
        # main_player_id = mc_player.player_main.player_id
        # # disable another fetch, these attributes were set via UDP
        # self.mass.loop.create_task(self.poll_player(main_player_id, False))
