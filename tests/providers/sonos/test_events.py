"""Tests for the UPnP RenderingControl events of Sonos speakers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from async_upnp_client.const import HttpRequest

from music_assistant.providers.sonos.const import (
    EVENTS_PATH,
    OPTIONS_EVENTS_POLL_INTERVAL,
    OPTIONS_POLL_INTERVAL,
)
from music_assistant.providers.sonos.events import SonosEvents
from tests.providers.sonos.test_player import _bind_player

# a Sonos ZonePlayer keeps RenderingControl in its embedded MediaRenderer device
_DESCRIPTION = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
 <specVersion><major>1</major><minor>0</minor></specVersion>
 <device>
  <deviceType>urn:schemas-upnp-org:device:ZonePlayer:1</deviceType>
  <friendlyName>Ray</friendlyName>
  <manufacturer>Sonos, Inc.</manufacturer>
  <modelName>Sonos Ray</modelName>
  <UDN>uuid:RINCON_1</UDN>
  <serviceList/>
  <deviceList>
   <device>
    <deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
    <friendlyName>Ray Media Renderer</friendlyName>
    <manufacturer>Sonos, Inc.</manufacturer>
    <modelName>Sonos Ray</modelName>
    <UDN>uuid:RINCON_1_MR</UDN>
    <serviceList>
     <service>
      <serviceType>urn:schemas-upnp-org:service:RenderingControl:1</serviceType>
      <serviceId>urn:upnp-org:serviceId:RenderingControl</serviceId>
      <SCPDURL>/xml/RenderingControl1.xml</SCPDURL>
      <controlURL>/MediaRenderer/RenderingControl/Control</controlURL>
      <eventSubURL>/MediaRenderer/RenderingControl/Event</eventSubURL>
     </service>
    </serviceList>
   </device>
  </deviceList>
 </device>
</root>"""

_SCPD = """<?xml version="1.0"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
 <specVersion><major>1</major><minor>0</minor></specVersion>
 <actionList/>
 <serviceStateTable>
  <stateVariable sendEvents="yes">
   <name>LastChange</name>
   <dataType>string</dataType>
  </stateVariable>
 </serviceStateTable>
</scpd>"""

_EVENT = (
    '<e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0"><e:property><LastChange>'
    "&lt;Event xmlns=&quot;urn:schemas-upnp-org:metadata-1-0/RCS/&quot;&gt;"
    "&lt;InstanceID val=&quot;0&quot;&gt;"
    "&lt;Volume channel=&quot;Master&quot; val=&quot;40&quot;/&gt;"
    "&lt;Volume channel=&quot;LF&quot; val=&quot;100&quot;/&gt;"
    "&lt;Volume channel=&quot;RF&quot; val=&quot;80&quot;/&gt;"
    "&lt;Bass val=&quot;3&quot;/&gt;"
    "&lt;/InstanceID&gt;&lt;/Event&gt;"
    "</LastChange></e:property></e:propertyset>"
)


class _Speaker:
    """A speaker that serves its description and takes subscriptions."""

    def __init__(self) -> None:
        """Accept subscriptions until told otherwise."""
        self.subscribe_status = 200
        self.methods: list[str] = []
        self.callbacks: list[str] = []

    async def handle(self, request: web.Request) -> web.Response:
        """Answer like a Sonos speaker on port 1400."""
        if request.path == "/xml/device_description.xml":
            return web.Response(text=_DESCRIPTION, content_type="text/xml")
        if request.path == "/xml/RenderingControl1.xml":
            return web.Response(text=_SCPD, content_type="text/xml")
        self.methods.append(request.method)
        if request.method == "SUBSCRIBE":
            self.callbacks.append(request.headers.get("CALLBACK", ""))
            if self.subscribe_status != 200:
                return web.Response(status=self.subscribe_status)
            return web.Response(headers={"SID": "uuid:sid-1", "TIMEOUT": "Second-1800"})
        return web.Response()


@pytest.fixture
async def speaker() -> AsyncIterator[tuple[_Speaker, str, SonosEvents, MagicMock]]:
    """Serve a speaker; return it, its description URL, the events and their mass."""
    fake = _Speaker()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", fake.handle)
    server = TestServer(app)
    await server.start_server()
    async with aiohttp.ClientSession() as session:
        mass = MagicMock()
        mass.http_session_no_ssl = session
        mass.streams.base_url = "http://192.168.1.10:8097"
        yield fake, str(server.make_url("/xml/device_description.xml")), SonosEvents(mass), mass
    await server.close()


async def _notify(events: SonosEvents) -> None:
    """Deliver an event as the speaker would."""
    await events._handler.handle_notify(
        HttpRequest(
            "NOTIFY",
            f"http://192.168.1.10:8097{EVENTS_PATH}",
            {"NT": "upnp:event", "NTS": "upnp:propchange", "SID": "uuid:sid-1"},
            _EVENT,
        )
    )


async def test_a_speaker_is_followed_once_its_first_event_came_in(
    speaker: tuple[_Speaker, str, SonosEvents, MagicMock],
) -> None:
    """The embedded RenderingControl is subscribed to; its event names channel volumes apart."""
    fake, location, events, _ = speaker
    on_change = MagicMock()

    await events.follow(location, on_change)
    assert fake.callbacks == [f"<http://192.168.1.10:8097{EVENTS_PATH}>"]
    # subscribed, but nothing came in yet: events may not reach MA at all
    assert not events.follows(location)
    await _notify(events)

    assert events.follows(location)
    on_change.assert_called_once_with(
        {"InstanceID": "0", "Volume": "40", "Volume_LF": "100", "Volume_RF": "80", "Bass": "3"}
    )


async def test_a_refused_or_lost_subscription_is_made_again(
    speaker: tuple[_Speaker, str, SonosEvents, MagicMock],
) -> None:
    """A refused subscribe or renewal leaves the speaker unfollowed until the next follow."""
    fake, location, events, _ = speaker
    fake.subscribe_status = 500
    await events.follow(location, MagicMock())
    assert location not in events._subscriptions

    fake.subscribe_status = 200
    await events.follow(location, MagicMock())
    await _notify(events)
    events._subscriptions[location].renew_at = 0.0
    fake.subscribe_status = 412
    await events.follow(location, MagicMock())

    assert not events.follows(location)
    assert location not in events._subscriptions


async def test_close_ends_the_subscriptions_and_the_route(
    speaker: tuple[_Speaker, str, SonosEvents, MagicMock],
) -> None:
    """Closing unsubscribes and takes the event route off MA's stream server."""
    fake, location, events, mass = speaker
    await events.follow(location, MagicMock())

    await events.close()

    assert fake.methods[-1] == "UNSUBSCRIBE"
    mass.streams.register_dynamic_route.return_value.assert_called_once_with()


@pytest.mark.parametrize(
    ("changes", "read"),
    [({"Bass": "3"}, True), ({"Volume_LF": "90"}, True), ({"Volume": "40", "Mute": "0"}, False)],
)
def test_an_option_event_reads_the_options(changes: dict[str, str], read: bool) -> None:
    """Only a change of an option's state variable schedules a (debounced) read."""
    mass = MagicMock()
    player, _ = _bind_player(mass)
    player.connected = True

    player._on_rendering_change(changes)

    assert mass.call_later.called is read


@pytest.mark.parametrize(
    ("follows", "interval"), [(True, OPTIONS_EVENTS_POLL_INTERVAL), (False, OPTIONS_POLL_INTERVAL)]
)
def test_poll_interval_follows_the_events(follows: bool, interval: int) -> None:
    """The options are polled less often while the speaker's events come in."""
    player, _ = _bind_player(MagicMock())
    events: Any = player._events
    events.follows.return_value = follows

    assert player.poll_interval == interval
