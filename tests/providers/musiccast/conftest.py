"""Fixtures for the MusicCast provider tests."""

import asyncio
from collections.abc import Coroutine
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiomusiccast.exceptions import MusicCastConnectionException

from music_assistant.providers.musiccast.constants import (
    CONF_PLAYER_HANDLE_SOURCE_DISABLED,
    CONF_PLAYER_SWITCH_SOURCE_NON_NET,
    CONF_PLAYER_TURN_OFF_ON_LEAVE,
    MC_NULL_GROUP,
    MC_SOURCE_MC_LINK,
    PLAYER_ZONE_SPLITTER,
)
from music_assistant.providers.musiccast.musiccast import (
    MusicCastController,
    MusicCastPhysicalDevice,
    MusicCastZoneDevice,
)
from music_assistant.providers.musiccast.player import MusicCastPlayer
from music_assistant.providers.musiccast.provider import MusicCastPlayerHelper, MusicCastProvider

INPUT_LIST = ["server", "spotify", "audio1", "audio2", "main_sync", "mc_link"]


class FakeMusicCastDevice:
    """State-keeping stand-in for aiomusiccast's MusicCastDevice and its AsyncDevice."""

    fail_fetch = False

    def __init__(self, ip: str, device_id: str, upnp_description: str = "") -> None:
        """Init a powered device with zones main, zone2 and zone3 on audio1, audio2 and audio3."""
        self.ip = ip
        self.upnp_description = upnp_description
        self.transport: object | None = None
        self.fetch_count = 0
        self.group_reduce_by_source = False
        self.media_image_url = None
        self.device = self
        self.build_capabilities = MagicMock()
        self.register_callback = MagicMock()
        self.register_group_update_callback = MagicMock()
        self.data = SimpleNamespace(
            device_id=device_id,
            model_name="RX-V6A",
            system_version="1.0",
            zones={
                "main": self._zone("Living", "audio1"),
                "zone2": self._zone("Patio", "audio2"),
                "zone3": self._zone("Garage", "audio3"),
            },
            input_names={},
            netusb_input="server",
            netusb_playback="play",
            netusb_track=None,
            group_role="none",
            group_id=MC_NULL_GROUP,
            group_client_list=[],
            group_server_zone="main",
            group_update_lock=asyncio.Lock(),
        )

    @staticmethod
    def _zone(name: str, input_: str) -> SimpleNamespace:
        return SimpleNamespace(
            name=name,
            power="on",
            input=input_,
            input_list=list(INPUT_LIST),
            current_volume=40,
            min_volume=0,
            max_volume=100,
            mute=False,
            sound_program=None,
            sound_program_list=[],
            capabilities=[],
        )

    async def fetch(self) -> None:
        """Fetch the device state."""
        if self.fail_fetch:
            raise MusicCastConnectionException
        self.fetch_count += 1

    async def enable_polling(self) -> None:
        """Open the UDP transport."""
        self.transport = object()

    def disable_polling(self) -> None:
        """Close the UDP transport."""
        self.transport = None

    async def turn_on(self, zone: str) -> None:
        """Power the zone on."""
        self.data.zones[zone].power = "on"

    async def turn_off(self, zone: str) -> None:
        """Put the zone in standby."""
        self.data.zones[zone].power = "standby"

    async def select_source(self, zone: str, source: str, mode: str = "") -> None:
        """Select the zone input."""
        self.data.zones[zone].input = source

    async def zone_join(self, zone: str) -> None:
        """Join the zone to the group the device is a client of."""
        self.data.zones[zone].input = MC_SOURCE_MC_LINK

    async def zone_unjoin(self, zone: str) -> None:
        """Switch the zone to a non-net input and put it in standby."""
        self.data.zones[zone].input = "audio1"
        self.data.zones[zone].power = "standby"

    async def mc_client_unjoin(self) -> None:
        """Leave the group the device is a client of."""
        self.data.group_role = "none"
        self.data.group_id = MC_NULL_GROUP

    async def mc_server_group_reduce(
        self, zone: str, client_ips: list[str], distribution_num: int
    ) -> None:
        """Remove the clients from the group served by the zone."""
        for client_ip in client_ips:
            self.data.group_client_list.remove(client_ip)

    async def mc_server_group_close(self) -> None:
        """Close the group served by the device."""
        self.data.group_role = "none"
        self.data.group_id = MC_NULL_GROUP
        self.data.group_client_list.clear()

    async def mc_client_join(self, server_ip: str, group_id: str, zone: str) -> None:
        """Join the zone to a group as client."""
        self.data.group_role = "client"
        self.data.group_id = group_id
        self.data.zones[zone].input = MC_SOURCE_MC_LINK

    async def mc_server_group_extend(
        self, zone: str, client_ips: list[str], group_id: str, distribution_num: int
    ) -> None:
        """Serve a group from the zone and add the clients to it."""
        self.data.group_role = "server"
        self.data.group_id = group_id
        self.data.group_server_zone = zone
        self.data.group_client_list.extend(client_ips)


def link(server: MusicCastZoneDevice, client: MusicCastZoneDevice) -> None:
    """Put the client zone into the MusicCast group served by the server zone."""
    server.device.data.group_role = "server"
    server.device.data.group_id = "GROUP"
    server.device.data.group_server_zone = server.zone_name
    server.device.data.group_client_list.append(client.device.ip)
    server.device.data.zones[server.zone_name].input = "server"
    client.device.data.group_role = "client"
    client.device.data.group_id = "GROUP"
    client.device.data.zones[client.zone_name].input = MC_SOURCE_MC_LINK


class MusicCastNetwork:
    """Provider with fake devices, a player registry and per player config values."""

    def __init__(self) -> None:
        """Init an empty network."""
        self.registry: dict[str, MusicCastPlayer] = {}
        self.tasks: list[asyncio.Future[Any]] = []
        self.config_values: dict[str, Any] = {
            CONF_PLAYER_HANDLE_SOURCE_DISABLED: False,
            CONF_PLAYER_SWITCH_SOURCE_NON_NET: "audio1",
            CONF_PLAYER_TURN_OFF_ON_LEAVE: False,
        }
        mass = MagicMock()
        mass.players.get_player.side_effect = self.registry.get
        mass.players.register_or_update.side_effect = self._register
        mass.create_task.side_effect = self._create_task
        mass.streams.base_url = "http://ma:8097"
        mass.config.get_base_player_config.side_effect = self._player_config
        self.mass = mass
        config = MagicMock(instance_id="musiccast")
        config.get_value.return_value = None
        provider = MusicCastProvider(mass, MagicMock(domain="musiccast"), config, set())
        provider.mc_controller = MusicCastController(logger=provider.logger)
        self.provider = provider

    def add_device(self, ip: str, device_id: str) -> MusicCastPhysicalDevice:
        """Add a known device with registered players for all its zones."""
        physical_device = MusicCastPhysicalDevice(
            device=FakeMusicCastDevice(ip, device_id), controller=self.provider.mc_controller
        )
        helper = MusicCastPlayerHelper(device_id=device_id, physical_device=physical_device)
        for zone_name in physical_device.device.data.zones:
            zone_device = MusicCastZoneDevice(zone_name, physical_device)
            physical_device.zone_devices[zone_name] = zone_device
            player = MusicCastPlayer(
                provider=self.provider,
                player_id=f"{device_id}{PLAYER_ZONE_SPLITTER}{zone_name}",
                physical_device=physical_device,
                zone_device=zone_device,
            )
            self.registry[player.player_id] = player
            setattr(helper, f"player_{zone_name}", player)
        self.provider.musiccast_player_helpers[device_id] = helper
        return physical_device

    def player(self, player_id: str) -> MusicCastPlayer:
        """Return a player, registered or not."""
        device_id, _, zone_name = player_id.partition(PLAYER_ZONE_SPLITTER)
        player = getattr(self.provider.musiccast_player_helpers[device_id], f"player_{zone_name}")
        assert isinstance(player, MusicCastPlayer)
        return player

    def _create_task(self, coro: Coroutine[Any, Any, Any], **kwargs: Any) -> asyncio.Future[Any]:
        task = asyncio.ensure_future(coro)
        self.tasks.append(task)
        return task

    async def _register(self, player: MusicCastPlayer) -> None:
        self.registry[player.player_id] = player

    def _player_config(self, player_id: str, provider_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            name=None,
            default_name=None,
            enabled=True,
            get_value=lambda key, default=None: self.config_values.get(key, default),
        )


@pytest.fixture
def network() -> MusicCastNetwork:
    """Two MusicCast receivers A and B, each with a main zone, a zone2 and a zone3."""
    network = MusicCastNetwork()
    network.add_device("192.168.1.10", "A")
    network.add_device("192.168.1.20", "B")
    return network
