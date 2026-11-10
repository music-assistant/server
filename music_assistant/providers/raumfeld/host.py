"""Live model of a Teufel Raumfeld host: its rooms, zones and devices."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import aiohttp
import defusedxml.ElementTree as DefusedET

from .constants import (
    LONG_POLL_FAILURES,
    LONG_POLL_RETRY_DELAY,
    LONG_POLL_TIMEOUT,
    LONG_POLL_WAIT,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from xml.etree.ElementTree import Element

LOGGER = logging.getLogger(__name__)

_MEDIA_SERVER_TYPE = "urn:schemas-upnp-org:device:MediaServer:1"
# one per speaker, holding its setup service (software version, updates)
_RAUMFELD_DEVICE_TYPE = "urn:schemas-raumfeld-com:device:RaumfeldDevice:1"
_SPOTIFY_ACTIVE = "active"


@dataclass(frozen=True)
class Room:
    """A room as the host reports it."""

    name: str
    udn: str
    # the room's own (physical) renderer; a room with more than one gives the first
    renderer_udn: str | None
    power_state: str | None
    spotify_active: bool


@dataclass(frozen=True)
class ZoneConfig:
    """The host's rooms and zones."""

    # by room name, in the order the host lists them
    rooms: dict[str, Room]
    # zone UDN -> its room names, in the order the host lists them (the coordinator first)
    zones: dict[str, list[str]]


@dataclass(frozen=True)
class Devices:
    """The UPnP devices the host knows."""

    # device UDN -> device description URL
    locations: dict[str, str]
    media_server_udn: str | None
    # speaker address -> description URL of the speaker's Raumfeld device
    raumfeld_devices: dict[str, str] = field(default_factory=dict)


class RaumfeldHost:
    """The rooms, zones and devices of a Raumfeld host, kept up to date while it runs."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        on_rooms_changed: Callable[[set[str]], None] | None = None,
    ) -> None:
        """
        Set up the model of a host; it holds nothing until it runs.

        :param session: The HTTP session to use.
        :param url: The base URL of the host's web service.
        :param on_rooms_changed: Called with the names of the rooms whose power state or
            Spotify Connect session changed, once the rooms have been read before.
        """
        self._session = session
        self._url = url
        self._on_rooms_changed = on_rooms_changed
        self._zone_config = ZoneConfig(rooms={}, zones={})
        self._devices = Devices(locations={}, media_server_udn=None)
        self._zones_read = asyncio.Event()
        self._devices_read = asyncio.Event()

    @property
    def media_server_udn(self) -> str | None:
        """Return the UDN of the host's media server, or ``None`` if it is not known."""
        return self._devices.media_server_udn

    async def run(self) -> None:
        """
        Follow the host's rooms, zones and devices until cancelled.

        :raises ExceptionGroup: When the host stops answering, with the error of the read
            that gave up (an ``aiohttp.ClientError``, ``OSError``, ``TimeoutError`` or an XML
            parse error).
        """
        async with asyncio.TaskGroup() as group:
            group.create_task(self._follow("getZones", self._apply_zone_config))
            group.create_task(self._follow("listDevices", self._apply_devices))

    async def wait_ready(self) -> None:
        """Wait until the rooms, zones and devices have been read once."""
        await self._zones_read.wait()
        await self._devices_read.wait()

    def get_rooms(self) -> list[str]:
        """Return the names of the rooms the host knows."""
        return list(self._zone_config.rooms)

    def get_zones(self) -> list[list[str]]:
        """Return the rooms of each active zone, each sorted by name."""
        return [sorted(rooms) for rooms in self._zone_config.zones.values()]

    def room_udn(self, room: str) -> str | None:
        """
        Return a room's UDN, or ``None`` if the host does not know the room.

        :param room: The room name.
        """
        return info.udn if (info := self._zone_config.rooms.get(room)) else None

    def renderer_udn(self, room: str) -> str | None:
        """
        Return the UDN of a room's own renderer, or ``None`` if it is not known.

        :param room: The room name.
        """
        return info.renderer_udn if (info := self._zone_config.rooms.get(room)) else None

    def power_state(self, room: str) -> str | None:
        """
        Return a room's power state, or ``None`` if the host reports none.

        :param room: The room name.
        """
        return info.power_state if (info := self._zone_config.rooms.get(room)) else None

    def plays_spotify_connect(self, room: str) -> bool:
        """
        Return whether Spotify Connect plays on a room's own renderer.

        :param room: The room name.
        """
        return bool((info := self._zone_config.rooms.get(room)) and info.spotify_active)

    def zone_udn(self, rooms: list[str]) -> str | None:
        """
        Return the UDN of the zone that holds exactly these rooms, or ``None``.

        :param rooms: The rooms of the zone.
        """
        wanted = set(rooms)
        for udn, zone_rooms in self._zone_config.zones.items():
            if set(zone_rooms) == wanted:
                return udn
        return None

    def zone_rooms(self, zone_udn: str) -> list[str]:
        """
        Return a zone's rooms with its coordinator first, or an empty list if it is unknown.

        :param zone_udn: The UDN of the zone.
        """
        return list(self._zone_config.zones.get(zone_udn, []))

    def location(self, udn: str) -> str | None:
        """
        Return a device's description URL, or ``None`` if the host does not list it.

        :param udn: The UDN of the device.
        """
        return self._devices.locations.get(udn)

    def raumfeld_device_location(self, address: str) -> str | None:
        """
        Return the description URL of the Raumfeld device of the speaker at an address.

        :param address: The speaker's IP address.
        """
        return self._devices.raumfeld_devices.get(address)

    def _apply_zone_config(self, xml: str) -> None:
        """Take over the rooms and zones from a getZones answer."""
        before = self._zone_config.rooms
        self._zone_config = parse_zone_config(xml)
        self._zones_read.set()
        changed = {
            name
            for name, room in self._zone_config.rooms.items()
            if (old := before.get(name)) is not None
            and (old.power_state, old.spotify_active) != (room.power_state, room.spotify_active)
        }
        if changed and self._on_rooms_changed is not None:
            self._on_rooms_changed(changed)

    def _apply_devices(self, xml: str) -> None:
        """Take over the devices from a listDevices answer."""
        self._devices = parse_devices(xml)
        self._devices_read.set()

    async def _follow(self, resource: str, apply: Callable[[str], None]) -> None:
        """Long-poll a resource of the host, applying every new version of it."""
        update_id: str | None = None
        failures = 0
        while True:
            headers = {"Prefer": f"wait={LONG_POLL_WAIT}"}
            if update_id is not None:
                headers["updateID"] = update_id
            try:
                # the host answers with a redirect to a session path first, which aiohttp
                # follows; it holds the request until the resource changes or the wait ends
                async with self._session.get(
                    f"{self._url}/{resource}",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=LONG_POLL_TIMEOUT),
                ) as response:
                    if response.status != 304:
                        response.raise_for_status()
                        apply(await response.text())
                        update_id = response.headers.get("updateID")
                        if update_id is None:
                            # without it the host answers at once, again and again
                            await asyncio.sleep(LONG_POLL_RETRY_DELAY)
            except TimeoutError:
                # nothing changed: the host can hold a request past the wait it was asked
                # for (measured), so it is asked again; a host that is gone is caught by
                # the supervisor's health check. It answered as usual, so a failure before
                # this one no longer counts toward giving up.
                failures = 0
                continue
            except (aiohttp.ClientError, OSError, DefusedET.ParseError) as err:
                failures += 1
                if failures >= LONG_POLL_FAILURES:
                    raise
                LOGGER.debug("Reading %s from the Raumfeld host failed: %r", resource, err)
                await asyncio.sleep(LONG_POLL_RETRY_DELAY)
                continue
            failures = 0


def parse_zone_config(xml: str) -> ZoneConfig:
    """
    Parse the host's getZones answer.

    :param xml: The answer as the host sent it.
    :raises DefusedET.ParseError: When it is not XML.
    """
    root = DefusedET.fromstring(xml)
    rooms: dict[str, Room] = {}
    zones: dict[str, list[str]] = {}
    for zone in root.iterfind("zones/zone"):
        zone_rooms = []
        for element in zone.iterfind("room"):
            if room := _room(element):
                rooms[room.name] = room
                zone_rooms.append(room.name)
        if (zone_udn := zone.get("udn")) and zone_rooms:
            zones[zone_udn] = zone_rooms
    for element in root.iterfind("unassignedRooms/room"):
        if room := _room(element):
            rooms[room.name] = room
    return ZoneConfig(rooms=rooms, zones=zones)


def parse_devices(xml: str) -> Devices:
    """
    Parse the host's listDevices answer.

    :param xml: The answer as the host sent it.
    :raises DefusedET.ParseError: When it is not XML.
    """
    root = DefusedET.fromstring(xml)
    locations: dict[str, str] = {}
    raumfeld_devices: dict[str, str] = {}
    media_server_udn = None
    for device in root.iterfind("device"):
        udn, location = device.get("udn"), device.get("location")
        if not udn or not location:
            continue
        locations[udn] = location
        if device.get("type") == _MEDIA_SERVER_TYPE:
            media_server_udn = udn
        elif device.get("type") == _RAUMFELD_DEVICE_TYPE and (
            address := urlparse(location).hostname
        ):
            raumfeld_devices[address] = location
    return Devices(
        locations=locations, media_server_udn=media_server_udn, raumfeld_devices=raumfeld_devices
    )


def _room(element: Element) -> Room | None:
    """Return the room a getZones room element describes, or ``None`` if it is incomplete."""
    name, udn = element.get("name"), element.get("udn")
    if not name or not udn:
        return None
    renderer = element.find("renderer")
    return Room(
        name=name,
        udn=udn,
        renderer_udn=renderer.get("udn") if renderer is not None else None,
        power_state=element.get("powerState"),
        spotify_active=renderer is not None and renderer.get("spotifyConnect") == _SPOTIFY_ACTIVE,
    )
