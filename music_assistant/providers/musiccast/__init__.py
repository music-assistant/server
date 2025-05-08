"""MusicCast for MusicAssistant."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from aiohttp.client_exceptions import (
    ClientError,
    ServerDisconnectedError,
)
from aiomusiccast.exceptions import MusicCastGroupException
from aiomusiccast.musiccast_device import MusicCastDevice
from aiomusiccast.pyamaha import MusicCastConnectionException
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import (
    ConfigEntryType,
    PlayerFeature,
    PlayerState,
    PlayerType,
    ProviderFeature,
)
from music_assistant_models.player import DeviceInfo, Player, PlayerMedia, PlayerSource
from zeroconf import ServiceStateChange

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.models.player_provider import PlayerProvider
from music_assistant.providers.musiccast.avt_helpers import (
    avt_get_media_info,
    avt_next,
    avt_pause,
    avt_play,
    avt_previous,
    avt_set_url,
    avt_stop,
    search_xml,
)
from music_assistant.providers.sonos.helpers import get_primary_ip_address

from .constants import (
    CONF_PLAYER_SWITCH_SOURCE_NON_NET,
    CONF_PLAYER_TURN_OFF_ON_LEAVE,
    MC_CONTROL_SOURCE_IDS,
    MC_DEVICE_INFO_ENDPOINT,
    MC_DEVICE_UPNP_ENDPOINT,
    MC_DEVICE_UPNP_PORT,
    MC_NETUSB_SOURCE_IDS,
    MC_PASSIVE_SOURCE_IDS,
    MC_POLL_INTERVAL,
    MC_SOURCE_MAIN_SYNC,
    MC_SOURCE_MC_LINK,
    PLAYER_CONFIG_ENTRIES,
    PLAYER_ZONE_SPLITTER,
)
from .musiccast import (
    MusicCastController,
    MusicCastPhysicalDevice,
    MusicCastPlayerState,
    MusicCastZoneDevice,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import (
        ConfigValueType,
        ProviderConfig,
    )
    from music_assistant_models.provider import ProviderManifest
    from zeroconf.asyncio import AsyncServiceInfo

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


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
class MusicCastPlayer:
    """MusicCastPlayer.

    Helper class to store MA player alongside physical device.
    """

    device_id: str  # device_id without ZONE_SPLITTER zone
    player_main: Player | None = None  # mass player
    player_zone2: Player | None = None  # mass player
    # I can only test up to zone 2
    player_zone3: Player | None = None  # mass player
    player_zone4: Player | None = None  # mass player

    # log allowed sources for a device with multiple sources once. see "_handle_zone_grouping"
    _log_allowed_sources: bool = True

    physical_device: MusicCastPhysicalDevice

    def get_player(self, zone: str) -> Player | None:
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

    def get_all_players(self) -> list[Player]:
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


@dataclass(kw_only=True)
class UpnpUpdateHelper:
    """UpnpUpdateHelper.

    See _update_player_attributes.
    """

    last_poll: float  # time.time
    controlled_by_mass: bool
    current_uri: str | None


class MusicCast(PlayerProvider):
    """MusicCast."""

    musiccast_players: dict[str, MusicCastPlayer] = {}

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

    async def unload(self, is_removed: bool = False) -> None:
        """Call on unload."""
        for mc_player in self.musiccast_players.values():
            mc_player.physical_device.remove()

    async def get_player_config_entries(
        self,
        player_id: str,
    ) -> tuple[ConfigEntry, ...]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        base_entries = await super().get_player_config_entries(player_id)
        zone_entries: tuple[ConfigEntry, ...] = ()
        if zone_player := self._get_zone_player(player_id):
            if len(zone_player.physical_device.zone_devices) > 1:
                mass_player = self.mass.players.get(player_id)
                assert mass_player is not None  # for type checking
                source_options: list[ConfigValueOption] = []
                allowed_sources = self._get_allowed_sources_zone_switch(zone_player)
                for (
                    source_id,
                    source_name,
                ) in zone_player.source_mapping.items():
                    if source_id in allowed_sources:
                        source_options.append(ConfigValueOption(title=source_name, value=source_id))
                if len(source_options) == 0:
                    # this should never happen
                    self.logger.error(
                        "The player %s has multiple zones, but lacks a non-net source to switch to."
                        " Please report this on github or discord.",
                        mass_player.display_name or mass_player.name,
                    )
                    zone_entries = ()
                else:
                    zone_entries = (
                        ConfigEntry(
                            key=CONF_PLAYER_SWITCH_SOURCE_NON_NET,
                            label="Switch to this non-net source when leaving a group.",
                            type=ConfigEntryType.STRING,
                            options=source_options,
                            default_value=source_options[0].value,
                            description="The zone will switch to this source when leaving a  group."
                            " It must be an input which doesn't require network connectivity.",
                        ),
                        ConfigEntry(
                            key=CONF_PLAYER_TURN_OFF_ON_LEAVE,
                            type=ConfigEntryType.BOOLEAN,
                            label="Turn off the zone when it leaves a group.",
                            default_value=False,
                            description="Turn off the zone when it leaves a group.",
                        ),
                    )

        return base_entries + zone_entries + PLAYER_CONFIG_ENTRIES

    def _get_zone_player(self, player_id: str) -> MusicCastZoneDevice | None:
        """Get music cast zone entity based on player id."""
        device_id, zone = player_id.split(PLAYER_ZONE_SPLITTER)
        mc_player = self.musiccast_players.get(device_id)
        if mc_player is None:
            return None
        return mc_player.physical_device.zone_devices.get(zone)

    async def _set_player_unavailable(self, player_id: str) -> None:
        """Set a player unavailable, and remove it from the MC group.

        Update all clients.
        """
        device_id, _ = player_id.split(PLAYER_ZONE_SPLITTER)
        mc_player = self.musiccast_players.get(device_id)
        if mc_player is None:
            return
        mc_player.physical_device.remove()
        for player in mc_player.get_all_players():
            # disable zones as well.
            player.available = False
            await self.mass.players.register_or_update(player)

    async def _cmd_run(
        self, player_id: str, fun: Callable[..., Coroutine[Any, Any, None]], *args: Any
    ) -> None:
        """Help function for all player cmds."""
        try:
            await fun(*args)
        except MusicCastConnectionException:
            await self._set_player_unavailable(player_id)
            self.logger.debug("Player became unavailable.")
        except MusicCastGroupException:
            # can happen, user shall try again.
            ...

    def _get_player_id_from_mc_zone_player(self, zone_player: MusicCastZoneDevice) -> str:
        device_id = zone_player.physical_device.device.data.device_id
        assert device_id is not None
        return f"{device_id}{PLAYER_ZONE_SPLITTER}{zone_player.zone_name}"

    async def cmd_stop(self, player_id: str) -> None:
        """Send STOP command to given player."""
        if zone_player := self._get_zone_player(player_id):
            upnp_update_helper = self.upnp_update_helper.get(player_id)
            if upnp_update_helper is not None and upnp_update_helper.controlled_by_mass:
                await avt_stop(self.mass.http_session, zone_player.physical_device)
            else:
                await self._cmd_run(player_id, zone_player.stop)

    async def cmd_play(self, player_id: str) -> None:
        """Send PLAY command to given player."""
        if zone_player := self._get_zone_player(player_id):
            upnp_update_helper = self.upnp_update_helper.get(player_id)
            if upnp_update_helper is not None and upnp_update_helper.controlled_by_mass:
                await avt_play(self.mass.http_session, zone_player.physical_device)
            else:
                await self._cmd_run(player_id, zone_player.play)

    async def cmd_pause(self, player_id: str) -> None:
        """Send PAUSE command to given player."""
        if zone_player := self._get_zone_player(player_id):
            upnp_update_helper = self.upnp_update_helper.get(player_id)
            if upnp_update_helper is not None and upnp_update_helper.controlled_by_mass:
                await avt_pause(self.mass.http_session, zone_player.physical_device)
            else:
                await self._cmd_run(player_id, zone_player.pause)

    async def cmd_next(self, player_id: str) -> None:
        """Send NEXT."""
        if zone_player := self._get_zone_player(player_id):
            upnp_update_helper = self.upnp_update_helper.get(player_id)
            if upnp_update_helper is not None and upnp_update_helper.controlled_by_mass:
                await avt_next(self.mass.http_session, zone_player.physical_device)
            else:
                await self._cmd_run(player_id, zone_player.next_track)

    async def cmd_previous(self, player_id: str) -> None:
        """Send PREVIOUS."""
        if zone_player := self._get_zone_player(player_id):
            upnp_update_helper = self.upnp_update_helper.get(player_id)
            if upnp_update_helper is not None and upnp_update_helper.controlled_by_mass:
                await avt_previous(self.mass.http_session, zone_player.physical_device)
            else:
                await self._cmd_run(player_id, zone_player.previous_track)

    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""
        if zone_player := self._get_zone_player(player_id):
            await self._cmd_run(player_id, zone_player.volume_set, volume_level)

    async def cmd_volume_mute(self, player_id: str, muted: bool) -> None:
        """Send VOLUME MUTE command to given player."""
        if zone_player := self._get_zone_player(player_id):
            await self._cmd_run(player_id, zone_player.volume_mute, muted)

    async def cmd_power(self, player_id: str, powered: bool) -> None:
        """Send POWER command to given player."""
        if zone_player := self._get_zone_player(player_id):
            if powered:
                await self._cmd_run(player_id, zone_player.turn_on)
            else:
                await self._cmd_run(player_id, zone_player.turn_off)

    async def cmd_group(self, player_id: str, target_player: str) -> None:
        """Handle GROUP command for given player."""
        await self.cmd_group_many(target_player=target_player, child_player_ids=[player_id])

    async def cmd_ungroup(self, player_id: str) -> None:
        """Handle UNGROUP command for given player."""
        if zone_player := self._get_zone_player(player_id):
            if zone_player.zone_name.startswith("zone"):
                # We are are zone.
                # We do not leave an MC group, but just change our source.
                await self._handle_zone_grouping(zone_player)
                return
            await self._cmd_run(player_id, zone_player.unjoin_player)

    def _get_allowed_sources_zone_switch(self, zone_player: MusicCastZoneDevice) -> set[str]:
        """Return non net sources for a zone player."""
        assert zone_player.zone_data is not None, "zone data missing"
        _input_sources: set[str] = set(zone_player.zone_data.input_list)
        _net_sources = set(MC_NETUSB_SOURCE_IDS)
        _net_sources.add(MC_SOURCE_MC_LINK)  # mc grouping source
        _net_sources.add(MC_SOURCE_MAIN_SYNC)  # main zone sync
        return _input_sources.difference(_net_sources)

    async def _handle_zone_grouping(self, zone_player: MusicCastZoneDevice) -> None:
        """Handle zone grouping.

        If a device has multiple zones, only a single zone can be net controlled.
        If another zone wants to join the group, the current net zone has to switch
        its input to a non-net one and optionally turn off.
        """
        player_id = self._get_player_id_from_mc_zone_player(zone_player)
        assert player_id is not None  # for TYPE_CHECKING
        _source = str(
            await self.mass.config.get_player_config_value(
                player_id, CONF_PLAYER_SWITCH_SOURCE_NON_NET
            )
        )
        # verify that this source actually exists and is non net
        _allowed_sources = self._get_allowed_sources_zone_switch(zone_player)
        if _source not in _allowed_sources:
            mass_player = self.mass.players.get(player_id)
            assert mass_player is not None
            msg = (
                "The switch source you specified for "
                f"{mass_player.display_name or mass_player.name}"
                " is not allowed. "
                f"The source must be any of: {', '.join(sorted(_allowed_sources))} "
                "Will use the first available source."
            )
            self.logger.error(msg)
            _source = _allowed_sources.pop()

        await self._cmd_run(player_id, zone_player.select_source, _source)
        _turn_off = bool(
            await self.mass.config.get_player_config_value(player_id, CONF_PLAYER_TURN_OFF_ON_LEAVE)
        )
        if _turn_off:
            await asyncio.sleep(2)
            await self._cmd_run(player_id, zone_player.turn_off)

    async def cmd_group_many(self, target_player: str, child_player_ids: list[str]) -> None:
        """Create temporary sync group by joining given players to target player."""
        device_id, zone_server = target_player.split(PLAYER_ZONE_SPLITTER)
        server = self._get_zone_player(target_player)
        if server is None:
            return
        children: set[MusicCastZoneDevice] = set()
        children_zones: list[MusicCastZoneDevice] = []
        for child_id in child_player_ids:
            if child := self._get_zone_player(child_id):
                _other_zone_mc: MusicCastZoneDevice | None = None
                for x in child.other_zones:
                    if x.is_netusb:
                        _other_zone_mc = x
                if _other_zone_mc and _other_zone_mc != child:
                    # of the same device, we use main_sync as input
                    if _other_zone_mc.zone_name == "main":
                        children_zones.append(child)
                    else:
                        self.logger.warning(
                            "It is impossible to join as a normal zone to another zone of the same "
                            "device. Only joining to main is possible. Please refer to the docs."
                        )
                else:
                    children.add(child)

        for child in children_zones:
            child_player_id = self._get_player_id_from_mc_zone_player(child)
            if child.state == MusicCastPlayerState.OFF:
                await self._cmd_run(child_player_id, child.turn_on)
            await self.select_source(child_player_id, MC_SOURCE_MAIN_SYNC)
        if not children:
            return

        await self._cmd_run(target_player, server.join_players, list(children))

    async def cmd_ungroup_member(self, player_id: str, target_player: str) -> None:
        """Handle UNGROUP command for given player."""
        await self.cmd_ungroup(player_id)

    async def select_source(self, player_id: str, source: str) -> None:
        """Handle SELECT SOURCE command on given player."""
        if zone_player := self._get_zone_player(player_id):
            await self._cmd_run(player_id, zone_player.select_source, source)

    async def play_media(
        self,
        player_id: str,
        media: PlayerMedia,
    ) -> None:
        """Handle PLAY MEDIA on given player."""
        if zone_player := self._get_zone_player(player_id):
            if len(zone_player.physical_device.zone_devices) > 1:
                # zone handling
                # only a single zone may have netusb capability
                for zone_name, dev in zone_player.physical_device.zone_devices.items():
                    if zone_name == zone_player.zone_name:
                        continue
                    if dev.is_netusb:
                        await self._handle_zone_grouping(dev)
            device_id, _ = player_id.split(PLAYER_ZONE_SPLITTER)
            lock = self.update_player_locks.get(device_id)
            assert lock is not None  # for type checking
            async with lock:
                # just in case
                if zone_player.source_id != "server":
                    await self.select_source(player_id, "server")
                await avt_set_url(
                    self.mass.http_session, zone_player.physical_device, player_media=media
                )
                await avt_play(self.mass.http_session, zone_player.physical_device)

                self.upnp_update_helper[player_id] = UpnpUpdateHelper(
                    last_poll=time.time(),
                    controlled_by_mass=True,
                    current_uri=media.uri,
                )

                if ma_player := self.mass.players.get(player_id):
                    ma_player.current_media = media
                    await self.mass.players.register_or_update(ma_player)

    async def enqueue_next_media(self, player_id: str, media: PlayerMedia) -> None:
        """Enqueue next."""
        if zone_player := self._get_zone_player(player_id):
            await avt_set_url(
                self.mass.http_session,
                zone_player.physical_device,
                player_media=media,
                enqueue=True,
            )

    async def poll_player(self, player_id: str, fetch: bool = True) -> None:
        """Poll player for state updates, only main zone is polled."""
        # we only poll for main, as we get zones alongside
        device_id, _ = player_id.split(PLAYER_ZONE_SPLITTER)
        mc_player = self.musiccast_players.get(device_id)
        if mc_player is None:
            return

        lock = self.update_player_locks.get(device_id)
        assert lock is not None  # for type checking
        if lock.locked():
            # we are called roughly every 1s when playing on udp callback, so just discard.
            return
        async with lock:
            if fetch:  # non udp "explicit polling case"
                try:
                    await mc_player.physical_device.fetch()
                except (MusicCastConnectionException, MusicCastGroupException):
                    await self._set_player_unavailable(player_id)
                    return
                except ServerDisconnectedError:
                    return

            for player in mc_player.get_all_players():
                _, zone = player.player_id.split(PLAYER_ZONE_SPLITTER)
                zone_device = mc_player.physical_device.zone_devices.get(zone)
                if zone_device is None:
                    continue
                await self._update_player_attributes(player, zone_device)
                player.available = True
                await self.mass.players.register_or_update(player)

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

        mc_player_known = self.musiccast_players.get(device_id)
        if (
            mc_player_known is not None
            and mc_player_known.player_main is not None
            and (
                mc_player_known.physical_device.device.device.upnp_description == description_url
                and mc_player_known.player_main.available
            )
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
            physical_device.register_callback(self._non_async_udp_callback)
            await self._register_player(physical_device, device_id)

    async def _register_player(
        self, physical_device: MusicCastPhysicalDevice, device_id: str
    ) -> None:
        """Register player including zones."""
        device_info = DeviceInfo(
            manufacturer="Yamaha Corporation",
            model=physical_device.device.data.model_name or "unknown model",
            software_version=physical_device.device.data.system_version or "unknown version",
        )

        def get_player(zone_name: str, player_name: str) -> Player:
            # player features
            # TODO: There is seek in the upnp desc
            # http://{ip}:49154/AVTransport/desc.xml
            supported_features: set[PlayerFeature] = {
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

            return Player(
                player_id=f"{device_id}{PLAYER_ZONE_SPLITTER}{zone_name}",
                provider=self.instance_id,
                type=PlayerType.PLAYER,
                name=player_name,
                available=True,
                device_info=device_info,
                needs_poll=zone_name == "main",
                poll_interval=MC_POLL_INTERVAL,
                supported_features=supported_features,
            )

        main_device = physical_device.zone_devices.get("main")
        if (
            main_device is None
            or main_device.zone_data is None
            or main_device.zone_data.name is None
        ):
            return
        musiccast_player = MusicCastPlayer(
            device_id=device_id,
            physical_device=physical_device,
        )

        for zone_name, zone_device in physical_device.zone_devices.items():
            if zone_device.zone_data is None or zone_device.zone_data.name is None:
                continue
            player = get_player(zone_name, zone_device.zone_data.name)
            setattr(musiccast_player, f"player_{zone_device.zone_name}", player)
            await self._update_player_attributes(player, zone_device)
            await self.mass.players.register_or_update(player)

        if musiccast_player.player_zone2 is not None and musiccast_player._log_allowed_sources:
            musiccast_player._log_allowed_sources = False
            player_main = musiccast_player.player_main
            assert player_main is not None
            self.logger.info(
                f"The player {player_main.display_name or player_main.name} has multiple zones. "
                "Please use the player config to configure a non-net source for grouping. "
            )

        self.musiccast_players[device_id] = musiccast_player

    async def _update_player_attributes(self, player: Player, device: MusicCastZoneDevice) -> None:
        # ruff: noqa: PLR0915
        zone_data = device.zone_data
        if zone_data is None:
            return

        player.name = zone_data.name or "UNKNOWN NAME"
        player.powered = zone_data.power == "on"

        # NOTE: aiomusiccast does not type hint the volume variables, and they may
        # be none, and not only integers
        _current_volume = cast("int | None", zone_data.current_volume)
        _max_volume = cast("int | None", zone_data.max_volume)
        _min_volume = cast("int | None", zone_data.min_volume)
        if _current_volume is None:
            player.volume_level = None
        else:
            _min_volume = 0 if _min_volume is None else _min_volume
            _max_volume = 100 if _max_volume is None else _max_volume
            if _min_volume == _max_volume:
                _max_volume += 1
            player.volume_level = int(_current_volume / (_max_volume - _min_volume) * 100)
        player.volume_muted = zone_data.mute

        # STATE

        match device.state:
            case MusicCastPlayerState.PAUSED:
                player.state = PlayerState.PAUSED
            case MusicCastPlayerState.PLAYING:
                player.state = PlayerState.PLAYING
            case MusicCastPlayerState.IDLE | MusicCastPlayerState.OFF:
                player.state = PlayerState.IDLE
        player.elapsed_time = device.media_position
        player.elapsed_time_last_updated = device.media_position_updated_at

        # SOURCES
        source_list: list[PlayerSource] = []
        for source_id, source_name in device.source_mapping.items():
            control = source_id in MC_CONTROL_SOURCE_IDS
            passive = source_id in MC_PASSIVE_SOURCE_IDS
            source_list.append(
                PlayerSource(
                    id=source_id,
                    name=source_name,
                    passive=passive,
                    can_play_pause=control,
                    can_seek=False,
                    can_next_previous=control,
                )
            )
        player.source_list.set(source_list)

        # UPDATE UPNP HELPER
        update_helper = self.upnp_update_helper.get(player.player_id)
        now = time.time()
        if update_helper is None or now - update_helper.last_poll > 5:
            # Let's not do this too often
            # Note: The devices always return the last UPnP xmls, even if
            # currently another source/ playback method is used
            try:
                _xml_media_info = await avt_get_media_info(
                    self.mass.http_session, device.physical_device
                )
            except ServerDisconnectedError:
                return
            _player_current_url = search_xml(_xml_media_info, "CurrentURI")

            # controlled by mass is only True, if we are directly controlled
            # i.e. we are not a group member.
            # the device's source id is server, if controlled by upnp, but also, if the internal
            # dlna function of the device are used. As a fallback, we then
            # use the item's title. This can only fail, if our current and next item
            # has the same name as the external.
            controlled_by_mass = False
            if _player_current_url is not None:
                controlled_by_mass = (
                    player.player_id in _player_current_url
                    and self.mass.streams.base_url in _player_current_url
                    and device.source_id == "server"
                )

            update_helper = UpnpUpdateHelper(
                last_poll=now,
                controlled_by_mass=controlled_by_mass,
                current_uri=_player_current_url,
            )

            self.upnp_update_helper[player.player_id] = update_helper

        # UPDATE PLAYBACK INFORMATION
        # Note to self:
        # player.current_media tells queue controller what is playing
        # and player.set_current_media is the helper function
        # do not access the queue controller to gain playback information here
        if update_helper.current_uri is not None and update_helper.controlled_by_mass:
            player.set_current_media(uri=update_helper.current_uri, clear_all=True)
        elif device.is_client:
            _server = device.group_server
            _server_id = self._get_player_id_from_mc_zone_player(_server)
            _server_update_helper = self.upnp_update_helper.get(_server_id)
            if (
                _server_update_helper is not None
                and _server_update_helper.current_uri is not None
                and _server_update_helper.controlled_by_mass
            ):
                player.set_current_media(
                    uri=_server_update_helper.current_uri,
                )
            else:
                player.set_current_media(
                    uri=f"{_server_id}_{_server.source_id}",
                    title=_server.media_title,
                    artist=_server.media_artist,
                    album=_server.media_album_name,
                    image_url=_server.media_image_url,
                )
        else:
            player.set_current_media(
                uri=f"{player.player_id}_{device.source_id}",
                title=device.media_title,
                artist=device.media_artist,
                album=device.media_album_name,
                image_url=device.media_image_url,
            )

        # SOURCE
        player.active_source = None  # means the player controller will figure it out
        if not device.is_client and not update_helper.controlled_by_mass:
            player.active_source = device.source_id
        elif device.is_client:
            _server = device.group_server
            _server_id = self._get_player_id_from_mc_zone_player(_server)
            if _server_update_helper := self.upnp_update_helper.get(_server_id):
                player.active_source = (
                    device.source_id if not _server_update_helper.controlled_by_mass else None
                )

        # GROUPING
        # A zone cannot be synced to another zone or main of the same device.
        # Additionally, a zone can only be synced, if main is currently not using any netusb
        # function.
        # For a Zone which will be synced to main, grouping emits a "main_sync" instead
        # of a mc link. The other way round, we log a warning.
        player.can_group_with = {self.instance_id}

        if len(device.musiccast_group) == 1:
            if device.musiccast_group[0] == device:
                # we are in a group with ourselves.
                player.group_childs.clear()
                player.synced_to = None
                player.active_group = None

        elif not device.is_client and not device.is_server:
            player.group_childs.clear()
            player.synced_to = None
            player.active_group = None

        elif device.is_client:
            _synced_to_id = self._get_player_id_from_mc_zone_player(device.group_server)
            player.group_childs.clear()
            player.synced_to = _synced_to_id
            player.active_group = _synced_to_id

        elif device.is_server:
            player.group_childs.set(
                [self._get_player_id_from_mc_zone_player(x) for x in device.musiccast_group]
            )
            player.synced_to = None
            player.active_group = None

    def _non_async_udp_callback(self, mc_physical_device: MusicCastPhysicalDevice) -> None:
        """Update callback.

        This is called if there are new UDP updates. Unfortunately, aiomusiccast
        only allows a sync callback, so we schedule an async task.
        """
        mc_player: MusicCastPlayer | None = None
        for mc_player in self.musiccast_players.values():
            if mc_player.physical_device == mc_physical_device:
                break
        assert mc_player is not None  # for type checking
        if mc_player.player_main is None:
            return
        main_player_id = mc_player.player_main.player_id
        # disable another fetch, these attributes were set via UDP
        self.mass.loop.create_task(self.poll_player(main_player_id, False))
