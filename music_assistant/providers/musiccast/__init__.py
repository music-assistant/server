"""MusicCast for MusicAssistant.

The devices do support gapless, however, only if the device itself is accessing
e.g. an upnp server. What the provider uses, is accessing an http stream (same
as playing from your phone to the MC device). This is not gapless, the MC App
checks roughly every 1s for playing information.

Thus we enforce queue flow mode.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from aiomusiccast.exceptions import MusicCastGroupException
from aiomusiccast.musiccast_device import MusicCastDevice
from aiomusiccast.pyamaha import MusicCastConnectionException
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    ConfigEntryType,
    PlayerFeature,
    PlayerState,
    PlayerType,
    ProviderFeature,
)
from music_assistant_models.media_items import UniqueList
from music_assistant_models.player import DeviceInfo, Player, PlayerMedia, PlayerSource

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.models.player_provider import PlayerProvider
from music_assistant.providers.sonos.helpers import get_primary_ip_address

from .constants import (
    CONF_PLAYER_SWITCH_SOURCE_NON_NET,
    CONF_PLAYER_TURN_OFF_ON_LEAVE,
    MC_CONTROL_SOURCE_IDS,
    MC_DEVICE_INFO_ENDPOINT,
    MC_DEVICE_UPNP_ENDPOINT,
    MC_DEVICE_UPNP_PORT,
    MC_PASSIVE_SOURCE_IDS,
    MC_POLL_INTERVAL,
    MC_SOURCE_MAIN_SYNC,
    PLAYER_CONFIG_ENTRIES,
    PLAYER_MAP_ZONE_SWITCH,
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
    from zeroconf import ServiceStateChange
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


class MusicCast(PlayerProvider):
    """MusicCast."""

    musiccast_players: dict[str, MusicCastPlayer] = {}

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

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        await super().loaded_in_mass()

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
                zone_entries = (
                    ConfigEntry(
                        key=CONF_PLAYER_SWITCH_SOURCE_NON_NET,
                        type=ConfigEntryType.STRING,
                        label="Switch to this non-net source on group leave.",
                        default_value=PLAYER_MAP_ZONE_SWITCH[zone_player.zone_name],
                        description="Switch to this non-net source on group leave. "
                        " This must be the source_id.",
                    ),
                    ConfigEntry(
                        key=CONF_PLAYER_TURN_OFF_ON_LEAVE,
                        type=ConfigEntryType.BOOLEAN,
                        label="Turn off zone after group is left.",
                        default_value=True,
                        description="Turn off zone after group is left.",
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

    async def _cmd_run(self, player_id: str, fun: Callable[..., Any], *args: Any) -> None:
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
            await self._cmd_run(player_id, zone_player.stop)

    async def cmd_play(self, player_id: str) -> None:
        """Send PLAY command to given player."""
        if zone_player := self._get_zone_player(player_id):
            await self._cmd_run(player_id, zone_player.play)

    async def cmd_pause(self, player_id: str) -> None:
        """Send PAUSE command to given player."""
        if zone_player := self._get_zone_player(player_id):
            await self._cmd_run(player_id, zone_player.pause)

    async def cmd_next(self, player_id: str) -> None:
        """Send NEXT.

        Only used for external source.
        """
        if zone_player := self._get_zone_player(player_id):
            await self._cmd_run(player_id, zone_player.next_track)

    async def cmd_previous(self, player_id: str) -> None:
        """Send PREVIOUS.

        Only used for external source.
        """
        if zone_player := self._get_zone_player(player_id):
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
                if server.physical_device == child.physical_device:
                    # If the zone joins a server, and the server is part of
                    # of the same device, we use main_sync as input
                    # We can only end up here if server is main, as we exclude
                    # joining otherwise in player attributes.
                    children_zones.append(child)
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

            await self._cmd_run(player_id, zone_player.play_url, media.uri)

    async def poll_player(self, player_id: str) -> None:
        """Poll player for state updates, only main zone is polled."""
        # we only poll for main, as we get zones alongside
        device_id, _ = player_id.split(PLAYER_ZONE_SPLITTER)
        mc_player = self.musiccast_players.get(device_id)
        if mc_player is None:
            return

        try:
            await mc_player.physical_device.fetch()
        except (MusicCastConnectionException, MusicCastGroupException):
            await self._set_player_unavailable(player_id)
            return

        for player in mc_player.get_all_players():
            _, zone = player.player_id.split(PLAYER_ZONE_SPLITTER)
            zone_device = mc_player.physical_device.zone_devices.get(zone)
            if zone_device is None:
                continue
            self._update_player_attributes(player, zone_device)
            player.available = True
            await self.mass.players.register_or_update(player)

    async def on_mdns_service_state_change(
        self, name: str, state_change: ServiceStateChange, info: AsyncServiceInfo | None
    ) -> None:
        """Discovery via mdns."""
        if info is None:
            return
        device_ip = get_primary_ip_address(info)
        if device_ip is None:
            return
        device_info = await self.mass.http_session.get(
            f"http://{device_ip}/{MC_DEVICE_INFO_ENDPOINT}"
        )
        if device_info.status == 404:
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
            manufacturer="Yamaha",
            model=physical_device.device.data.model_name or "unknown model",
            software_version=physical_device.device.data.system_version or "unknown version",
        )

        def get_player(zone_name: str, player_name: str) -> Player:
            # player features
            # mc supports gapless if the playback is controlled by the
            # receiver, but we need queue flow mode for the provider
            supported_features: set[PlayerFeature] = {
                PlayerFeature.VOLUME_SET,
                PlayerFeature.VOLUME_MUTE,
                PlayerFeature.PAUSE,
                PlayerFeature.POWER,
                PlayerFeature.SELECT_SOURCE,
                PlayerFeature.SET_MEMBERS,
                PlayerFeature.NEXT_PREVIOUS,  # only used for external source
            }

            return Player(
                player_id=f"{device_id}{PLAYER_ZONE_SPLITTER}{zone_name}",
                provider=self.instance_id,
                type=PlayerType.PLAYER,
                name=player_name,
                available=True,
                device_info=device_info,
                needs_poll=zone_name == "main",
                poll_interval=MC_POLL_INTERVAL,  # default
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
            self._update_player_attributes(player, zone_device)
            await self.mass.players.register_or_update(player)

        self.musiccast_players[device_id] = musiccast_player

    def _update_player_attributes(self, player: Player, device: MusicCastZoneDevice) -> None:
        # ruff: noqa: PLR0915
        zone_data = device.zone_data
        if zone_data is None:
            return

        player.name = zone_data.name or "UNKNOWN NAME"
        player.powered = zone_data.power == "on"

        player.volume_level = int(
            zone_data.current_volume / (zone_data.max_volume - zone_data.min_volume) * 100
        )
        player.volume_muted = zone_data.mute

        # STATE
        # we can only use one zone at a time for server playback
        player.elapsed_time = None
        player.elapsed_time_last_updated = None

        match device.state:
            case MusicCastPlayerState.PAUSED:
                player.state = PlayerState.PAUSED
            case MusicCastPlayerState.PLAYING:
                player.state = PlayerState.PLAYING
                player.elapsed_time = device.media_position
                player.elapsed_time_last_updated = device.media_position_updated_at
            case MusicCastPlayerState.IDLE | MusicCastPlayerState.OFF:
                player.state = PlayerState.IDLE

        # SOURCES
        player.source_list = UniqueList([])
        for source_id, source_name in device.source_mapping.items():
            control = source_id in MC_CONTROL_SOURCE_IDS
            passive = source_id in MC_PASSIVE_SOURCE_IDS
            player.source_list.append(
                # UI bug? I can't control my sources...
                PlayerSource(
                    id=source_id,
                    name=source_name,
                    passive=passive,
                    can_play_pause=control,
                    can_seek=False,
                    can_next_previous=control,
                )
            )

        # QUEUE
        # queue = self.mass.player_queues.get_active_queue(player.player_id)
        # if device.is_controlled_by_mass and queue is not None:
        # be optimistic
        if device.source_id == "server":  #  and queue is not None:
            player.active_source = None  # queue.queue_id
        else:
            player.active_source = device.source_id

        # GROUPING
        # A zone cannot be synced to another zone or main of the same device.
        # Additionally, a zone can only be synced, if main is currently not using any netusb
        # function.
        # For a Zone which will be synced to main, grouping emits a "main_sync" instead
        # of a mc link.

        # TODO: Revisit this - it produces the expected output, but the ui
        # does not update correctly
        can_group_with = set(self.mc_controller.all_zone_devices)
        if len(device.physical_device.zone_devices) > 1:
            # receiver with zones
            _main_zone = device.physical_device.zone_devices.get("main")
            if _main_zone is None:
                return
            _other_zones = set(_main_zone.other_zones)

            if device == _main_zone:
                # a main zone cannot join another zone, but a zone can join
                # main, see below.
                can_group_with.difference_update(_other_zones)
            elif device in _other_zones:
                # can_group_with.difference_update(_other_zones.difference({device}))
                # enforce a zone to be either the server, or sync to main of
                # same device. makes life much easier.
                can_group_with = {device, _main_zone}

        # player.can_group_with = {
        #     x.ma_player_id for x in can_group_with if x.ma_player_id is not None
        # }
        player.can_group_with = {self.instance_id}

        if len(device.musiccast_group) == 1:
            if device.musiccast_group[0] == device:
                # we are in a group with ourselves.
                player.group_childs = UniqueList([])
                player.synced_to = None
                player.active_group = None

        elif not device.is_client and not device.is_server:
            player.group_childs = UniqueList([])
            player.synced_to = None
            player.active_group = None

        elif device.is_client:
            _synced_to_id = self._get_player_id_from_mc_zone_player(device.group_server)
            player.group_childs = UniqueList([])
            player.synced_to = _synced_to_id
            player.active_group = _synced_to_id

        elif device.is_server:
            player.group_childs = UniqueList(
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
        self.mass.loop.create_task(self.poll_player(main_player_id))
