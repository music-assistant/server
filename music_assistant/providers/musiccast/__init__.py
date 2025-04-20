"""MusicCast for MusicAssistant."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from ipaddress import IPv4Address
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from aiomusiccast.exceptions import MusicCastGroupException
from aiomusiccast.musiccast_device import MusicCastDevice
from aiomusiccast.pyamaha import MusicCastConnectionException
from async_upnp_client.search import async_search
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

from music_assistant.constants import (
    CONF_PLAYERS,
)
from music_assistant.models.player_provider import PlayerProvider

from .constants import (
    CONF_NETWORK_SCAN,
    MC_CONTROL_SOURCE_IDS,
    MC_PASSIVE_SOURCE_IDS,
    PLAYER_CONFIG_ENTRIES,
    POLL_INTERVAL,
    ZONE_SPLITTER,
)
from .musiccast import (
    MusicCastController,
    MusicCastPhysicalDevice,
    MusicCastPlayerState,
    MusicCastZoneDevice,
)

if TYPE_CHECKING:
    from async_upnp_client.utils import CaseInsensitiveDict
    from music_assistant_models.config_entries import (
        ConfigValueType,
        ProviderConfig,
    )
    from music_assistant_models.provider import ProviderManifest

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
    return (
        ConfigEntry(
            key=CONF_NETWORK_SCAN,
            type=ConfigEntryType.BOOLEAN,
            label="Allow network scan for discovery",
            default_value=False,
            description="Enable network scan for discovery of players. \n"
            "Can be used if (some of) your players are not automatically discovered.",
        ),
    )


@dataclass(kw_only=True)
class MusicCastPlayer:
    """MusicCastPlayer."""

    udn: str  # = player_id
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

    _discovery_running: bool = False
    musiccast_players: dict[str, MusicCastPlayer] = {}
    lock: asyncio.Lock = asyncio.Lock()

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        return {ProviderFeature.SYNC_PLAYERS}

    async def handle_async_init(self) -> None:
        """Async init."""
        self.mc_controller = MusicCastController()

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
        return base_entries + PLAYER_CONFIG_ENTRIES

    def _get_zone_player(self, player_id: str) -> MusicCastZoneDevice | None:
        udn, zone = player_id.split(ZONE_SPLITTER)
        mc_player = self.musiccast_players.get(udn)
        if mc_player is None:
            return None
        return mc_player.physical_device.zone_devices.get(zone)

    async def _set_player_unavailable(self, player_id: str) -> None:
        udn, zone = player_id.split(ZONE_SPLITTER)
        mc_player = self.musiccast_players.get(udn)
        if mc_player is None:
            return
        player = mc_player.get_player(zone)
        if player is None:
            return
        player.available = False
        async with self.lock:
            await self.mass.players.register_or_update(player)

    async def run_cmd(self, player_id: str, fun: Callable[..., Any], *args: Any) -> None:
        """Run cmd if possible."""
        try:
            await fun(*args)
        except MusicCastConnectionException:
            await self._set_player_unavailable(player_id)
            self.logger.debug("Player became unavailable.")
        except MusicCastGroupException:
            # can happen, user shall try again.
            ...

    async def cmd_stop(self, player_id: str) -> None:
        """Send STOP command to given player."""
        if zone_player := self._get_zone_player(player_id):
            await self.run_cmd(player_id, zone_player.stop)

    async def cmd_play(self, player_id: str) -> None:
        """Send PLAY command to given player."""
        if zone_player := self._get_zone_player(player_id):
            await self.run_cmd(player_id, zone_player.play)

    async def cmd_pause(self, player_id: str) -> None:
        """Send PAUSE command to given player."""
        if zone_player := self._get_zone_player(player_id):
            await self.run_cmd(player_id, zone_player.pause)

    async def cmd_next(self, player_id: str) -> None:
        """Send NEXT.

        Only used for external source.
        """
        if zone_player := self._get_zone_player(player_id):
            await self.run_cmd(player_id, zone_player.next_track)

    async def cmd_previous(self, player_id: str) -> None:
        """Send PREVIOUS.

        Only used for external source.
        """
        if zone_player := self._get_zone_player(player_id):
            await self.run_cmd(player_id, zone_player.previous_track)

    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""
        if zone_player := self._get_zone_player(player_id):
            await self.run_cmd(player_id, zone_player.volume_set, volume_level)

    async def cmd_volume_mute(self, player_id: str, muted: bool) -> None:
        """Send VOLUME MUTE command to given player."""
        if zone_player := self._get_zone_player(player_id):
            await self.run_cmd(player_id, zone_player.volume_mute, muted)

    async def cmd_power(self, player_id: str, powered: bool) -> None:
        """Send POWER command to given player."""
        if zone_player := self._get_zone_player(player_id):
            if powered:
                await self.run_cmd(player_id, zone_player.turn_on)
            else:
                await self.run_cmd(player_id, zone_player.turn_off)

    async def cmd_group(self, player_id: str, target_player: str) -> None:
        """Handle GROUP command for given player."""
        await self.cmd_group_many(target_player=target_player, child_player_ids=[player_id])

    async def cmd_ungroup(self, player_id: str) -> None:
        """Handle UNGROUP command for given player."""
        if zone_player := self._get_zone_player(player_id):
            await self.run_cmd(player_id, zone_player.unjoin_player)

    async def cmd_group_many(self, target_player: str, child_player_ids: list[str]) -> None:
        """Create temporary sync group by joining given players to target player."""
        udn, zone_server = target_player.split(ZONE_SPLITTER)
        server = self._get_zone_player(target_player)
        if server is None:
            return
        children = []
        for child_id in child_player_ids:
            if child := self._get_zone_player(child_id):
                children.append(child)
        if not children:
            return

        await self.run_cmd(target_player, server.join_players, children)

    async def cmd_ungroup_member(self, player_id: str, target_player: str) -> None:
        """Handle UNGROUP command for given player."""
        await self.cmd_ungroup(player_id)

    async def select_source(self, player_id: str, source: str) -> None:
        """Handle SELECT SOURCE command on given player."""
        if zone_player := self._get_zone_player(player_id):
            await self.run_cmd(player_id, zone_player.select_source, source)

    async def play_media(
        self,
        player_id: str,
        media: PlayerMedia,
    ) -> None:
        """Handle PLAY MEDIA on given player."""
        if zone_player := self._get_zone_player(player_id):
            await self.run_cmd(player_id, zone_player.play_url, media.uri)

    async def poll_player(self, player_id: str) -> None:
        """Poll player for state updates, only main zone is polled."""
        # we only poll for main, as we get zones alongside
        async with self.lock:
            udn, _ = player_id.split(ZONE_SPLITTER)
            mc_player = self.musiccast_players.get(udn)
            if mc_player is None:
                return

            players_available = True
            try:
                await mc_player.physical_device.fetch()
            except (MusicCastConnectionException, MusicCastGroupException):
                players_available = False

            for zone_name in ["main", "zone2", "zone3", "zone4"]:
                player = mc_player.get_player(zone_name)
                if player is None:
                    continue
                if not players_available:
                    player.available = False
                    await self.mass.players.register_or_update(player)
                    continue

                zone_device = mc_player.physical_device.zone_devices.get(zone_name)
                if zone_device is None:
                    player.available = False
                else:
                    self._update_player_attributes(player, zone_device)
                await self.mass.players.register_or_update(player)

    # The discovery methods are copied over from the DLNAPlayerProvider and adjusted for MusicCast.

    async def discover_players(self, use_multicast: bool = False) -> None:
        """Discover MusicCast players on the network.

        Method is called once when provider is loaded.
        """
        if self._discovery_running:
            return
        try:
            self._discovery_running = True
            self.logger.debug("MusicCast discovery started...")
            allow_network_scan = self.config.get_value(CONF_NETWORK_SCAN)
            discovered_devices: set[str] = set()

            async def on_response(discovery_info: CaseInsensitiveDict) -> None:
                """Process discovered device from ssdp search."""
                ssdp_st: str = discovery_info.get("st", discovery_info.get("nt"))
                if not ssdp_st:
                    return

                if "MediaRenderer" not in ssdp_st:
                    # we're only interested in MediaRenderer devices
                    return

                ssdp_usn: str = discovery_info["usn"]
                ssdp_udn: str | None = discovery_info.get("_udn")
                if not ssdp_udn and ssdp_usn.startswith("uuid:"):
                    ssdp_udn = ssdp_usn.split("::")[0]

                assert ssdp_udn is not None  # for type checking

                if ssdp_udn in discovered_devices:
                    # already processed this device
                    return
                if "rincon" in ssdp_udn.lower():
                    # ignore Sonos devices
                    return

                discovered_devices.add(ssdp_udn)

                await self._device_discovered(ssdp_udn, discovery_info["location"])

            # we iterate between using a regular and multicast search (if enabled)
            if allow_network_scan and use_multicast:
                await async_search(on_response, target=(str(IPv4Address("255.255.255.255")), 1900))
            else:
                await async_search(on_response)

        finally:
            self._discovery_running = False

        def reschedule() -> None:
            self.mass.create_task(self.discover_players(use_multicast=not use_multicast))

        # reschedule self once finished
        self.mass.loop.call_later(300, reschedule)

    async def _device_discovered(self, udn: str, description_url: str) -> None:
        """Handle discovered MusicCast player."""
        # verify that this is a MusicCast player
        check: bool = await MusicCastDevice.check_yamaha_ssdp(
            description_url, self.mass.http_session
        )
        if not check:
            return

        async with self.lock:
            ip = urlparse(description_url).netloc.split(":")[0]
            if musiccast_player := self.musiccast_players.get(udn):
                # existing player
                if musiccast_player.player_main is None:
                    return
                if (
                    musiccast_player.physical_device.device.device.upnp_description
                    == description_url
                    and musiccast_player.player_main.available
                ):
                    # nothing to do, device is already connected
                    return
                # update description url to newly discovered one
                musiccast_player.physical_device = MusicCastPhysicalDevice(
                    device=MusicCastDevice(
                        client=self.mass.http_session, ip=ip, upnp_description=description_url
                    ),
                    controller=self.mc_controller,
                    udn=udn,
                )
            else:
                # new player detected
                conf_key = f"{CONF_PLAYERS}/{udn}/enabled"
                enabled = self.mass.config.get(conf_key, True)
                # ignore disabled players
                if not enabled:
                    self.logger.debug("Ignoring disabled player: %s", udn)
                    return
                physical_device = MusicCastPhysicalDevice(
                    device=MusicCastDevice(
                        client=self.mass.http_session, ip=ip, upnp_description=description_url
                    ),
                    controller=self.mc_controller,
                    udn=udn,
                )
                await physical_device.async_init()  # fetch + polling
                physical_device.register_callback(self.update_callback)
                await self._register_player(physical_device, udn)

    async def _register_player(self, physical_device: MusicCastPhysicalDevice, udn: str) -> None:
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
                player_id=f"{udn}{ZONE_SPLITTER}{zone_name}",
                provider=self.instance_id,
                type=PlayerType.PLAYER,
                name=player_name,
                available=True,
                device_info=device_info,
                needs_poll=zone_name == "main",
                poll_interval=POLL_INTERVAL,  # default
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
            udn=udn,
            physical_device=physical_device,
        )

        for zone_name, zone_device in physical_device.zone_devices.items():
            if zone_device.zone_data is None or zone_device.zone_data.name is None:
                continue
            player = get_player(zone_name, zone_device.zone_data.name)
            setattr(musiccast_player, f"player_{zone_device.zone_name}", player)
            self._update_player_attributes(player, zone_device)
            await self.mass.players.register_or_update(player)

        self.musiccast_players[udn] = musiccast_player

    def _update_player_attributes(self, player: Player, device: MusicCastZoneDevice) -> None:
        zone_data = device.zone_data
        if zone_data is None:
            return

        player.name = zone_data.name or "UNKNOWN NAME"
        player.powered = zone_data.power == "on"

        player.volume_level = int(
            zone_data.current_volume / (zone_data.max_volume - zone_data.min_volume) * 100
        )
        player.volume_muted = zone_data.mute

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

        player.source_list = UniqueList([])
        for source_id, source_name in device.source_mapping.items():
            control = source_id in MC_CONTROL_SOURCE_IDS
            passive = source_id in MC_PASSIVE_SOURCE_IDS
            player.source_list.append(
                PlayerSource(
                    id=source_id,
                    name=source_name,
                    passive=passive,
                    can_play_pause=control,
                    can_seek=False,
                    can_next_previous=control,
                )
            )

        queue = self.mass.player_queues.get_active_queue(player.player_id)
        if device.is_controlled_by_mass and queue is not None:
            player.active_source = queue.queue_id
        else:
            player.active_source = device.source_id

        # grouping - should be last for return
        # officially they need netusb, let's ignore this for now
        player.can_group_with = {self.instance_id}  # we can group with all musiccast devices

        if len(device.musiccast_group) == 1:
            if device.musiccast_group[0] == device:
                # we are in a group with ourselves.
                player.group_childs = UniqueList([])
                player.synced_to = None
                player.active_group = None
                return

        if not device.is_client and not device.is_server:
            player.group_childs = UniqueList([])
            player.synced_to = None
            player.active_group = None
            return

        if device.is_client:
            player.group_childs = UniqueList([])
            player.synced_to = device.group_server.ma_player_id
            player.active_group = device.group_server.ma_player_id

        if device.is_server:
            player.group_childs = UniqueList(
                [x.ma_player_id for x in device.musiccast_group if x.ma_player_id is not None]
            )
            player.synced_to = None
            player.active_group = None

    def update_callback(self, mc_physical_device: MusicCastPhysicalDevice) -> None:
        """Update callback."""
        mc_player: MusicCastPlayer | None = None
        for mc_player in self.musiccast_players.values():
            if mc_player.physical_device == mc_physical_device:
                break
        assert mc_player is not None  # for type checking
        if mc_player.player_main is None:
            return
        main_player_id = mc_player.player_main.player_id
        self.mass.loop.create_task(self.poll_player(main_player_id))
