"""MusicCast Handling for Music Assistant.

This is largely taken from the MusicCast integration in HomeAssistant,
https://github.com/home-assistant/core/tree/dev/homeassistant/components/yamaha_musiccast
and then adapted for MA.

We have

MusicCastController - only once, holds state information of MC network
    MusicCastPhysicalDevice - AV Receiver, Boxes
        MusicCastZoneDevice - Player entity, which can be controlled.
"""

import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime
from enum import Enum, auto
from random import getrandbits
from typing import cast

from aiomusiccast.exceptions import MusicCastConnectionException, MusicCastGroupException
from aiomusiccast.musiccast_device import MusicCastDevice

from .constants import (
    MC_DEFAULT_ZONE,
    MC_NULL_GROUP,
    MC_PLAY_TITLE,
    MC_SOURCE_MAIN_SYNC,
    MC_SOURCE_MC_LINK,
)


def random_uuid_hex() -> str:
    """Generate a random UUID hex.

    This uuid should not be used for cryptographically secure
    operations.

    Taken from HA.
    """
    return f"{getrandbits(32 * 4):032x}"


class MusicCastPlayerState(Enum):
    """MusicCastPlayerState."""

    PLAYING = auto()
    PAUSED = auto()
    IDLE = auto()
    OFF = auto()


class MusicCastZoneDevice:
    """Zone device.

    A physical device may have different zones, though only a single zone
    can be used for net playback (but the other ones can be synced internally).
    """

    def __init__(self, zone_name: str, physical_device: "MusicCastPhysicalDevice") -> None:
        """Init."""
        self.zone_name = zone_name  # this is not the friendly name
        self.controller = physical_device.controller
        self.device = physical_device.device
        self.zone_data = self.device.data.zones.get(self.zone_name)
        self.physical_device = physical_device

        self.physical_device.register_group_update_callback(self._group_update)

    async def _group_update(self) -> None:
        for entity in self.controller.all_server_devices:
            if self.device.group_reduce_by_source:
                await entity._check_client_list()

    @property
    def source_id(self) -> str:
        """ID of the current input source.

        Internal source name.
        """
        zone = self.device.data.zones.get(self.zone_name)
        assert zone is not None
        assert isinstance(zone.input, str)
        return zone.input

    @property
    def reverse_source_mapping(self) -> dict[str, str]:
        """Return a mapping from the source label to the source name."""
        return {v: k for k, v in self.source_mapping.items()}

    @property
    def source(self) -> str:
        """Name of the current input source."""
        return self.source_mapping.get(self.source_id, "UNKNOWN SOURCE")

    @property
    def source_mapping(self) -> dict[str, str]:
        """Return a mapping of the actual source names to their labels configured in the App."""
        assert self.zone_data is not None  # for type checking
        result = {}
        for input_ in self.zone_data.input_list:
            label = self.device.data.input_names.get(input_, "")
            if input_ != label and (
                label in self.zone_data.input_list
                or list(self.device.data.input_names.values()).count(label) > 1
            ):
                label += f" ({input_})"
            if label == "":
                label = input_
            result[input_] = label
        return result

    @property
    def is_netusb(self) -> bool:
        """Controlled by network if true."""
        return cast("bool", self.device.data.netusb_input == self.source_id)

    @property
    def is_tuner(self) -> bool:
        """Tuner if true."""
        return self.source_id == "tuner"

    @property
    def is_controlled_by_mass(self) -> bool:
        """Controlled by mass if true."""
        return self.source_id == "server" and self.media_title == MC_PLAY_TITLE

    @property
    def media_position(self) -> int | None:
        """Position of current playing media in seconds."""
        if self.is_netusb:
            return cast("int", self.device.data.netusb_play_time)
        return None

    @property
    def media_position_updated_at(self) -> datetime | None:
        """When was the position of the current playing media valid."""
        if self.is_netusb:
            return cast("datetime", self.device.data.netusb_play_time_updated)

        return None

    @property
    def is_network_server(self) -> bool:
        """Return only true if the current entity is a network server.

        I.e. not a main zone with an attached zone2.
        """
        return cast(
            "bool",
            self.device.data.group_role == "server"
            and self.device.data.group_id != MC_NULL_GROUP
            and self.zone_name == self.device.data.group_server_zone,
        )

    @property
    def other_zones(self) -> list["MusicCastZoneDevice"]:
        """Return media player entities of the other zones of this device."""
        return [
            entity
            for entity in self.physical_device.zone_devices.values()
            if entity != self and isinstance(entity, MusicCastZoneDevice)
        ]

    @property
    def state(self) -> MusicCastPlayerState:
        """Return the state of the player."""
        assert self.zone_data is not None
        if self.zone_data.power == "on":
            if self.is_netusb and self.device.data.netusb_playback == "pause":
                return MusicCastPlayerState.PAUSED
            if self.is_netusb and self.device.data.netusb_playback == "stop":
                return MusicCastPlayerState.IDLE
            return MusicCastPlayerState.PLAYING
        return MusicCastPlayerState.OFF

    @property
    def is_server(self) -> bool:
        """Return whether the media player is the server/host of the group.

        If the media player is not part of a group, False is returned.
        """
        return self.is_network_server or (
            self.zone_name == MC_DEFAULT_ZONE
            and len(
                [entity for entity in self.other_zones if entity.source_id == MC_SOURCE_MAIN_SYNC]
            )
            > 0
        )

    @property
    def is_network_client(self) -> bool:
        """Return True if the current entity is a network client and not just a main sync entity."""
        return (
            self.device.data.group_role == "client"
            and self.device.data.group_id != MC_NULL_GROUP
            and self.source_id == MC_SOURCE_MC_LINK
        )

    @property
    def is_client(self) -> bool:
        """Return whether the media player is the client of a group.

        If the media player is not part of a group, False is returned.
        """
        return self.is_network_client or self.source_id == MC_SOURCE_MAIN_SYNC

    @property
    def musiccast_zone_entity(self) -> "MusicCastZoneDevice":
        """Return the musiccast entity of the physical device.

        It is possible that multiple zones use MusicCast as client at the same time.
        In this case the first one is returned.
        """
        for entity in self.other_zones:
            if entity.is_network_server or entity.is_network_client:
                return entity

        return self

    @property
    def musiccast_group(self) -> list["MusicCastZoneDevice"]:
        """Return all media players of the current group, if the media player is server."""
        if self.is_client:
            # If we are a client we can still share group information, but we will take them from
            # the server.
            if (server := self.group_server) != self:
                return server.musiccast_group

            return [self]
        if not self.is_server:
            return [self]
        entities = self.controller.all_zone_devices
        clients = [entity for entity in entities if entity.is_part_of_group(self)]
        return [self, *clients]

    @property
    def group_server(self) -> "MusicCastZoneDevice":
        """Return the server of the own group if present, self else."""
        for entity in self.controller.all_server_devices:
            if self.is_part_of_group(entity):
                return entity
        return self

    @property
    def media_title(self) -> str | None:
        """Return the title of current playing media."""
        if self.is_netusb:
            return cast("str", self.device.data.netusb_track)
        if self.is_tuner:
            return cast("str", self.device.tuner_media_title)

        return None

    @property
    def media_image_url(self) -> str | None:
        """Return the image url of current playing media."""
        if self.is_client and self.group_server != self:
            return cast("str", self.group_server.device.media_image_url)
        return cast("str", self.device.media_image_url) if self.is_netusb else None

    @property
    def media_artist(self) -> str | None:
        """Return the artist of current playing media (Music track only)."""
        if self.is_netusb:
            return cast("str", self.device.data.netusb_artist)
        if self.is_tuner:
            return cast("str", self.device.tuner_media_artist)

        return None

    @property
    def media_album_name(self) -> str | None:
        """Return the album of current playing media (Music track only)."""
        return cast("str", self.device.data.netusb_album) if self.is_netusb else None

    async def turn_on(self) -> None:
        """Turn on."""
        await self.device.turn_on(self.zone_name)

    async def turn_off(self) -> None:
        """Turn off."""
        await self.device.turn_off(self.zone_name)

    async def volume_mute(self, mute: bool) -> None:
        """Volume mute."""
        await self.device.mute_volume(self.zone_name, mute)

    async def volume_set(self, volume_level: int) -> None:
        """Volume set."""
        await self.device.set_volume_level(self.zone_name, volume_level / 100)

    async def play(self) -> None:
        """Play."""
        if self.is_netusb:
            await self.device.netusb_play()

    async def pause(self) -> None:
        """Pause."""
        if self.is_netusb:
            await self.device.netusb_pause()

    async def stop(self) -> None:
        """Stop."""
        if self.is_netusb:
            await self.device.netusb_stop()

    async def previous_track(self) -> None:
        """Send previous track command."""
        if self.is_netusb:
            await self.device.netusb_previous_track()
        elif self.is_tuner:
            await self.device.tuner_previous_station()

    async def next_track(self) -> None:
        """Send next track command."""
        if self.is_netusb:
            await self.device.netusb_next_track()
        elif self.is_tuner:
            await self.device.tuner_next_station()

    async def play_url(self, url: str) -> None:
        """Play http url."""
        await self.device.play_url_media(self.zone_name, media_url=url, title=MC_PLAY_TITLE)

    async def select_source(self, source_id: str) -> None:
        """Select input source. Internal source name."""
        await self.device.select_source(self.zone_name, source_id)

    def is_part_of_group(self, group_server: "MusicCastZoneDevice") -> bool:
        """Return True if the given server is the server of self's group."""
        return group_server != self and (
            (
                self.device.ip in group_server.device.data.group_client_list
                and self.device.data.group_id == group_server.device.data.group_id
                and self.device.ip != group_server.device.ip
                and self.source_id == MC_SOURCE_MC_LINK
            )
            or (self.device.ip == group_server.device.ip and self.source_id == MC_SOURCE_MAIN_SYNC)
        )

    async def join_players(self, group_members: list["MusicCastZoneDevice"]) -> None:
        """Add all clients given in entities to the group of the server.

        Creates a new group if necessary. Used for join service.
        """
        assert self.zone_data is not None
        if self.state == MusicCastPlayerState.OFF:
            await self.turn_on()

        if not self.is_server and self.musiccast_zone_entity.is_server:
            # The MusicCast Distribution Module of this device is already in use. To use it as a
            # server, we first have to unjoin and wait until the servers are updated.
            await self.musiccast_zone_entity._server_close_group()
        elif self.musiccast_zone_entity.is_client:
            await self._client_leave_group(True)
        # Use existing group id if we are server, generate a new one else.
        group_id = self.device.data.group_id if self.is_server else random_uuid_hex().upper()
        assert group_id is not None  # for type checking

        ip_addresses = set()
        # First let the clients join
        for client in group_members:
            if client != self:
                try:
                    network_join = await client._client_join(group_id, self)
                except MusicCastGroupException:
                    network_join = await client._client_join(group_id, self)

                if network_join:
                    ip_addresses.add(client.device.ip)

        if ip_addresses:
            await self.device.mc_server_group_extend(
                self.zone_name,
                list(ip_addresses),
                group_id,
                self.controller.distribution_num,
            )

        await self._group_update()

    async def unjoin_player(self) -> None:
        """Leave the group.

        Stops the distribution if device is server. Used for unjoin service.
        """
        if self.is_server:
            await self._server_close_group()
        else:
            # this is not as in HA
            await self._client_leave_group(True)

    # Internal client functions

    async def _client_join(self, group_id: str, server: "MusicCastZoneDevice") -> bool:
        """Let the client join a group.

        If this client is a server, the server will stop distributing.
        If the client is part of a different group,
        it will leave that group first. Returns True, if the server has to
        add the client on his side.
        """
        # If we should join the group, which is served by the main zone,
        # we can simply select main_sync as input.
        if self.state == MusicCastPlayerState.OFF:
            await self.turn_on()
        if self.device.ip == server.device.ip:
            if server.zone_name == MC_DEFAULT_ZONE:
                await self.select_source(MC_SOURCE_MAIN_SYNC)
                return False

            # It is not possible to join a group hosted by zone2 from main zone.
            # raise?
            return False

        if self.musiccast_zone_entity.is_server:
            # If one of the zones of the device is a server, we need to unjoin first.
            await self.musiccast_zone_entity._server_close_group()

        elif self.is_client:
            if self.is_part_of_group(server):
                return False

            await self._client_leave_group()

        elif (
            self.device.ip in server.device.data.group_client_list
            and self.device.data.group_id == server.device.data.group_id
            and self.device.data.group_role == "client"
        ):
            # The device is already part of this group (e.g. main zone is also a client of this
            # group).
            # Just select mc_link as source
            await self.device.zone_join(self.zone_name)
            return False

        await self.device.mc_client_join(server.device.ip, group_id, self.zone_name)
        return True

    async def _client_leave_group(self, force: bool = False) -> None:
        """Make self leave the group.

        Should only be called for clients.
        """
        if not force and (
            self.source_id == MC_SOURCE_MAIN_SYNC
            or [entity for entity in self.other_zones if entity.source_id == MC_SOURCE_MC_LINK]
        ):
            await self.device.zone_unjoin(self.zone_name)
        else:
            servers = [
                server
                for server in self.controller.all_server_devices
                if server.device.data.group_id == self.device.data.group_id
            ]
            await self.device.mc_client_unjoin()
            if servers:
                await servers[0].device.mc_server_group_reduce(
                    servers[0].zone_name,
                    [self.device.ip],
                    self.controller.distribution_num,
                )

    # Internal server functions

    async def _server_close_group(self) -> None:
        """Close group of self.

        Should only be called for servers.
        """
        for client in self.musiccast_group:
            if client != self:
                await client._client_leave_group()
        await self.device.mc_server_group_close()

    async def _check_client_list(self) -> None:
        """Let the server check if all its clients are still part of his group."""
        if not self.is_server or self.device.data.group_update_lock.locked():
            return

        client_ips_for_removal = [
            expected_client_ip
            for expected_client_ip in self.device.data.group_client_list
            # The client is no longer part of the group. Prepare removal.
            if expected_client_ip not in [entity.device.ip for entity in self.musiccast_group]
        ]

        if client_ips_for_removal:
            await self.device.mc_server_group_reduce(
                self.zone_name, client_ips_for_removal, self.controller.distribution_num
            )
        if len(self.musiccast_group) < 2:
            # The group is empty, stop distribution.
            await self._server_close_group()


class MusicCastPhysicalDevice:
    """Physical MusicCast device.

    May contain multiple zone devices, but at least one, main.
    """

    def __init__(
        self,
        device: MusicCastDevice,
        controller: "MusicCastController",
    ):
        """Init."""
        self.device = device
        self.zone_devices: dict[str, MusicCastZoneDevice] = {}  # zone_name: device
        self.controller = controller
        self.controller.physical_devices.append(self)

    async def async_init(self) -> bool:
        """Async init.

        Returns true if initial fetch was successful.
        """
        try:
            await self.fetch()
        except (MusicCastConnectionException, MusicCastGroupException):
            return False

        self.device.build_capabilities()

        # enable udp polling
        await self.enable_polling()

        for zone_name in self.device.data.zones:
            self.zone_devices[zone_name] = MusicCastZoneDevice(zone_name, self)

        return True

    async def enable_polling(self) -> None:
        """Enable udp polling."""
        await self.device.device.enable_polling()

    def disable_polling(self) -> None:
        """Disable udp polling."""
        self.device.device.disable_polling()

    async def fetch(self) -> None:
        """Fetch device information.

        Should be called regularly, e.g. every 60s, in case some udp info
        goes missing.
        """
        await self.device.fetch()

    def register_callback(self, fun: Callable[["MusicCastPhysicalDevice"], None]) -> None:
        """Register a non-async callback."""

        def _cb() -> None:
            fun(self)

        self.device.register_callback(_cb)

    def register_group_update_callback(self, fun: Callable[[], Awaitable[None]]) -> None:
        """Register an async group update callback."""
        self.device.register_group_update_callback(fun)

    def remove(self) -> None:
        """Remove physical device."""
        with suppress(AttributeError):
            # might already be closed
            self.device.device.disable_polling()
        with suppress(ValueError):
            # might already be closed
            self.controller.physical_devices.remove(self)


class MusicCastController:
    """MusicCastController.

    Holds information of full known MC network.
    """

    def __init__(self, logger: logging.Logger) -> None:
        """Init."""
        self.physical_devices: list[MusicCastPhysicalDevice] = []
        self.logger = logger

    @property
    def distribution_num(self) -> int:
        """Return the distribution_num (number of clients in the whole musiccast system)."""
        return sum(len(x.zone_devices) for x in self.physical_devices)

    @property
    def all_zone_devices(self) -> list[MusicCastZoneDevice]:
        """Return all zone devices."""
        result = []
        for physical_device in self.physical_devices:
            result.extend(list(physical_device.zone_devices.values()))
        return result

    @property
    def all_server_devices(self) -> list[MusicCastZoneDevice]:
        """Return server devices."""
        return [x for x in self.all_zone_devices if x.is_server]
