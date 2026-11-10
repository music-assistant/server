"""Tests for the model of a Teufel Raumfeld host."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from unittest.mock import MagicMock, patch

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from music_assistant.providers.raumfeld import host as host_module
from music_assistant.providers.raumfeld.constants import LONG_POLL_FAILURES
from music_assistant.providers.raumfeld.host import RaumfeldHost, parse_devices, parse_zone_config

_ZONES = """<?xml version='1.0' encoding='UTF-8'?>
<zoneConfig numRooms='4' spotifyMode='singleRoom'>
 <zones>
  <zone udn='uuid:zone-1'>
   <room name='Sofa' udn='uuid:sofa' powerState='ACTIVE'>
    <renderer udn='uuid:sofa-renderer' name='Speaker Sofa'></renderer>
   </room>
   <room name='Bar' udn='uuid:bar' powerState='ACTIVE'>
    <renderer udn='uuid:bar-renderer' name='Speaker Bar'></renderer>
   </room>
  </zone>
 </zones>
 <unassignedRooms>
  <room name='Kitchen' udn='uuid:kitchen' powerState='MANUAL_STANDBY'>
   <renderer udn='uuid:kitchen-renderer' name='Connector Kitchen'></renderer>
  </room>
  <room name='Office' udn='uuid:office'>
   <renderer udn='uuid:office-renderer' name='Speaker Office' spotifyConnect='active'></renderer>
  </room>
 </unassignedRooms>
</zoneConfig>"""

_DEVICES = """<?xml version='1.0' encoding='UTF-8'?>
<devices>
 <device udn='uuid:zone-1' type='urn:schemas-upnp-org:device:MediaRenderer:1'
  location='http://192.168.1.41:56766/zone-1.xml'>Bar,Sofa</device>
 <device udn='uuid:bar-renderer' type='urn:schemas-upnp-org:device:MediaRenderer:1'
  location='http://192.168.1.42:60778/bar.xml'>Speaker Bar</device>
 <device udn='uuid:bar-device' type='urn:schemas-raumfeld-com:device:RaumfeldDevice:1'
  location='http://192.168.1.42:57793/bar-device.xml'>Teufel Raumfeld Device</device>
 <device udn='uuid:media-server' type='urn:schemas-upnp-org:device:MediaServer:1'
  location='http://192.168.1.41:57132/ms.xml'>Raumfeld MediaServer</device>
</devices>"""


def _loaded_host() -> RaumfeldHost:
    """Build a host model that has read the rooms, zones and devices above."""
    host = RaumfeldHost(MagicMock(), "http://192.168.1.41:47365")
    host._apply_zone_config(_ZONES)
    host._apply_devices(_DEVICES)
    return host


def test_rooms_and_zones_are_read_as_the_host_lists_them() -> None:
    """Every room is known, a zone keeps the host's order, and the zones list sorts it."""
    host = _loaded_host()
    assert host.get_rooms() == ["Sofa", "Bar", "Kitchen", "Office"]
    assert host.get_zones() == [["Bar", "Sofa"]]
    assert host.zone_udn(["Bar", "Sofa"]) == "uuid:zone-1"
    # the coordinator first, as the host lists it
    assert host.zone_rooms("uuid:zone-1") == ["Sofa", "Bar"]


def test_a_zone_is_found_only_by_all_its_rooms() -> None:
    """A part of a zone, or more rooms than it has, is not that zone."""
    host = _loaded_host()
    assert host.zone_udn(["Bar"]) is None
    assert host.zone_udn(["Bar", "Sofa", "Kitchen"]) is None
    assert host.zone_rooms("uuid:gone") == []


def test_room_details_are_read_from_their_elements() -> None:
    """UDNs, power state and Spotify Connect come from the room and its renderer."""
    host = _loaded_host()
    assert host.room_udn("Kitchen") == "uuid:kitchen"
    assert host.renderer_udn("Kitchen") == "uuid:kitchen-renderer"
    assert host.power_state("Kitchen") == "MANUAL_STANDBY"
    # a room without a power state (a Gen 1 speaker) reports none
    assert host.power_state("Office") is None
    assert host.plays_spotify_connect("Office")
    assert not host.plays_spotify_connect("Kitchen")


def test_an_unknown_room_has_no_details() -> None:
    """A room the host does not list gives nothing, rather than raising."""
    host = _loaded_host()
    assert host.room_udn("Attic") is None
    assert host.renderer_udn("Attic") is None
    assert host.power_state("Attic") is None
    assert not host.plays_spotify_connect("Attic")


def test_devices_are_read_with_the_media_server() -> None:
    """Each device is found by its UDN, and the media server is recognised by its type."""
    host = _loaded_host()
    assert host.location("uuid:bar-renderer") == "http://192.168.1.42:60778/bar.xml"
    assert host.location("uuid:unknown") is None
    assert host.media_server_udn == "uuid:media-server"
    assert (
        host.raumfeld_device_location("192.168.1.42") == "http://192.168.1.42:57793/bar-device.xml"
    )
    assert host.raumfeld_device_location("192.168.1.41") is None


def test_spotify_connect_ends_with_the_session() -> None:
    """A renderer the host no longer flags no longer plays Spotify Connect."""
    host = _loaded_host()
    host._apply_zone_config(_ZONES.replace(" spotifyConnect='active'", ""))
    assert not host.plays_spotify_connect("Office")


def test_a_change_of_power_or_spotify_connect_is_reported() -> None:
    """The rooms whose power state or Spotify session changed are named, once read before."""
    changed = MagicMock()
    host = RaumfeldHost(MagicMock(), "http://192.168.1.41:47365", changed)
    host._apply_zone_config(_ZONES)
    # the first read is no change
    changed.assert_not_called()
    host._apply_zone_config(
        _ZONES.replace("MANUAL_STANDBY", "ACTIVE").replace(" spotifyConnect='active'", "")
    )
    changed.assert_called_once_with({"Kitchen", "Office"})
    # the same rooms again: nothing to report
    changed.reset_mock()
    host._apply_zone_config(
        _ZONES.replace("MANUAL_STANDBY", "ACTIVE").replace(" spotifyConnect='active'", "")
    )
    changed.assert_not_called()


def test_incomplete_elements_are_skipped() -> None:
    """A room or device without its name, UDN or location is left out, not half-read."""
    config = parse_zone_config(
        "<zoneConfig><unassignedRooms><room name='Attic'/>"
        "<room name='Cellar' udn='uuid:cellar'/></unassignedRooms></zoneConfig>"
    )
    assert list(config.rooms) == ["Cellar"]
    assert config.rooms["Cellar"].renderer_udn is None
    devices = parse_devices("<devices><device udn='uuid:x'>No location</device></devices>")
    assert devices.locations == {}
    assert devices.media_server_udn is None


class _Host:
    """A web service that holds the zones like the host, behind its redirect."""

    def __init__(self) -> None:
        """Answer with the zones above until told otherwise."""
        self.zones = _ZONES
        self.update_id = "1"
        self.status = 200
        self.seen_update_ids: list[str | None] = []
        self.changed = asyncio.Event()

    async def handle(self, request: web.Request) -> web.StreamResponse:
        """Redirect like the host, then answer at once or hold until the zones change."""
        if not request.path.startswith("/session"):
            raise web.HTTPTemporaryRedirect(f"/session{request.path}")
        if self.status != 200:
            return web.Response(status=self.status)
        if request.path.endswith("/listDevices"):
            return web.Response(text=_DEVICES, headers={"updateId": "d1"})
        self.seen_update_ids.append(request.headers.get("updateID"))
        if request.headers.get("updateID") == self.update_id:
            try:
                await asyncio.wait_for(self.changed.wait(), 5)
            except TimeoutError:
                return web.Response(status=304)
            self.changed.clear()
        return web.Response(text=self.zones, headers={"updateId": self.update_id})

    def change(self, zones: str, update_id: str) -> None:
        """Have the host report new zones."""
        self.zones, self.update_id = zones, update_id
        self.changed.set()


@pytest.fixture
async def served() -> AsyncIterator[Callable[[_Host], Awaitable[str]]]:
    """Return a function that serves a host and gives its base URL."""
    servers: list[TestServer] = []

    async def start(host: _Host) -> str:
        app = web.Application()
        app.router.add_route("GET", "/{tail:.*}", host.handle)
        server = TestServer(app)
        await server.start_server()
        servers.append(server)
        return str(server.make_url("")).rstrip("/")

    yield start
    for server in servers:
        await server.close()


async def test_changes_on_the_host_are_followed(
    served: Callable[[_Host], Awaitable[str]],
) -> None:
    """The model is ready after the first answers, and takes over every change after that."""
    fake = _Host()
    url = await served(fake)
    async with aiohttp.ClientSession() as session:
        host = RaumfeldHost(session, url)
        task = asyncio.create_task(host.run())
        try:
            await asyncio.wait_for(host.wait_ready(), 5)
            assert host.get_zones() == [["Bar", "Sofa"]]
            fake.change(_ZONES.replace("<room name='Bar'", "<room name='Hall'"), "2")
            async with asyncio.timeout(5):
                while host.get_zones() != [["Hall", "Sofa"]]:
                    await asyncio.sleep(0.01)
        finally:
            task.cancel()
    # the first request asks for the zones as they are, the next only for a change
    assert fake.seen_update_ids[:2] == [None, "1"]


async def test_a_host_that_keeps_failing_ends_the_run(
    served: Callable[[_Host], Awaitable[str]],
) -> None:
    """A few failed reads in a row end the run, so the connection is set up again."""
    fake = _Host()
    fake.status = 500
    url = await served(fake)
    async with aiohttp.ClientSession() as session:
        host = RaumfeldHost(session, url)
        with (
            patch.object(host_module, "LONG_POLL_RETRY_DELAY", 0),
            pytest.raises(ExceptionGroup) as raised,
        ):
            await asyncio.wait_for(host.run(), 5)
    assert raised.group_contains(aiohttp.ClientResponseError)


async def test_a_request_held_past_the_limit_is_asked_again(
    served: Callable[[_Host], Awaitable[str]],
) -> None:
    """A host that holds a request longer than allowed has nothing new; that is no failure."""
    fake = _Host()
    url = await served(fake)
    async with aiohttp.ClientSession() as session:
        host = RaumfeldHost(session, url)
        with patch.object(host_module, "LONG_POLL_TIMEOUT", 0.1):
            task = asyncio.create_task(host.run())
            try:
                await asyncio.wait_for(host.wait_ready(), 5)
                async with asyncio.timeout(5):
                    while len(fake.seen_update_ids) < 1 + LONG_POLL_FAILURES + 1:
                        await asyncio.sleep(0.01)
                assert not task.done()
            finally:
                task.cancel()
    assert host.get_zones() == [["Bar", "Sofa"]]


class _FlakyHost(_Host):
    """A host whose reads after the first fail and are held past the limit in turn."""

    def __init__(self) -> None:
        """Answer the first read; after that, fail every other one."""
        super().__init__()
        self.reads = 0

    async def handle(self, request: web.Request) -> web.StreamResponse:
        """Fail every other read of the zones once they were read."""
        if request.path.startswith("/session") and request.path.endswith("/getZones"):
            self.reads += 1
            if self.reads > 1 and self.reads % 2 == 0:
                self.seen_update_ids.append("failed")
                return web.Response(status=500)
        return await super().handle(request)


async def test_failures_apart_from_each_other_do_not_end_the_run(
    served: Callable[[_FlakyHost], Awaitable[str]],
) -> None:
    """A failed read now and then, with quiet reads in between, is not a host that is gone."""
    fake = _FlakyHost()
    url = await served(fake)
    async with aiohttp.ClientSession() as session:
        host = RaumfeldHost(session, url)
        with (
            patch.object(host_module, "LONG_POLL_TIMEOUT", 0.1),
            patch.object(host_module, "LONG_POLL_RETRY_DELAY", 0),
        ):
            task = asyncio.create_task(host.run())
            try:
                await asyncio.wait_for(host.wait_ready(), 5)
                async with asyncio.timeout(5):
                    while fake.seen_update_ids.count("failed") < LONG_POLL_FAILURES + 1:
                        await asyncio.sleep(0.01)
                assert not task.done()
            finally:
                task.cancel()
