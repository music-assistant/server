"""Tests for the UPnP event subscriptions to Raumfeld renderers."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from async_upnp_client.const import HttpRequest

from music_assistant.providers.raumfeld.events import ROOM_EVENTS_PATH, RendererEvents
from music_assistant.providers.raumfeld.upnp import SERVICE_RENDERING_CONTROL

_DESCRIPTION = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
 <specVersion><major>1</major><minor>0</minor></specVersion>
 <device>
  <deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
  <friendlyName>Speaker Bar</friendlyName>
  <manufacturer>Raumfeld</manufacturer>
  <modelName>Raumfeld One S</modelName>
  <UDN>uuid:bar-renderer</UDN>
  <serviceList>
   <service>
    <serviceType>urn:schemas-upnp-org:service:RenderingControl:1</serviceType>
    <serviceId>urn:upnp-org:serviceId:RenderingControl</serviceId>
    <SCPDURL>/rc.xml</SCPDURL>
    <controlURL>/rc/control</controlURL>
    <eventSubURL>/rc/event</eventSubURL>
   </service>
  </serviceList>
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
    "&lt;Volume val=&quot;44&quot;/&gt;&lt;Mute val=&quot;0&quot;/&gt;"
    "&lt;/InstanceID&gt;&lt;/Event&gt;"
    "</LastChange></e:property></e:propertyset>"
)


class _Renderer:
    """A renderer that serves its description and takes subscriptions."""

    def __init__(self) -> None:
        """Accept subscriptions until told otherwise."""
        self.subscribe_status = 200
        self.requests: list[tuple[str, dict[str, str]]] = []

    async def handle(self, request: web.Request) -> web.Response:
        """Answer like a Raumfeld renderer."""
        if request.path == "/description.xml":
            return web.Response(text=_DESCRIPTION, content_type="text/xml")
        if request.path == "/rc.xml":
            return web.Response(text=_SCPD, content_type="text/xml")
        self.requests.append((request.method, dict(request.headers)))
        if request.method == "SUBSCRIBE":
            if self.subscribe_status != 200:
                return web.Response(status=self.subscribe_status)
            return web.Response(headers={"SID": "uuid:sid-1", "TIMEOUT": "Second-300"})
        return web.Response()


@pytest.fixture
async def served() -> AsyncIterator[Callable[[_Renderer], Awaitable[str]]]:
    """Return a function that serves a renderer and gives its description URL."""
    servers: list[TestServer] = []

    async def start(renderer: _Renderer) -> str:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", renderer.handle)
        server = TestServer(app)
        await server.start_server()
        servers.append(server)
        return str(server.make_url("/description.xml"))

    yield start
    for server in servers:
        await server.close()


def _mass(session: aiohttp.ClientSession) -> MagicMock:
    """Build a Music Assistant whose stream server takes the event route."""
    mass = MagicMock()
    mass.http_session = session
    mass.streams.base_url = "http://192.168.1.124:8097"
    return mass


async def _notify(events: RendererEvents, body: str = _EVENT) -> None:
    """Deliver an event as the renderer would."""
    await events._handler.handle_notify(
        HttpRequest(
            "NOTIFY",
            f"http://192.168.1.124:8097{ROOM_EVENTS_PATH}",
            {"NT": "upnp:event", "NTS": "upnp:propchange", "SID": "uuid:sid-1"},
            body,
        )
    )


async def test_a_renderer_is_followed_once_its_first_event_came_in(
    served: Callable[[_Renderer], Awaitable[str]],
) -> None:
    """The subscription asks for events at MA's stream server, and the first event proves it."""
    renderer = _Renderer()
    location = await served(renderer)
    changes = MagicMock()
    async with aiohttp.ClientSession() as session:
        events = RendererEvents(
            _mass(session), ROOM_EVENTS_PATH, SERVICE_RENDERING_CONTROL, changes
        )
        await events.refresh([location])
        method, headers = renderer.requests[0]
        assert method == "SUBSCRIBE"
        assert headers["CALLBACK"] == f"<http://192.168.1.124:8097{ROOM_EVENTS_PATH}>"
        # subscribed, but nothing came in yet: events may not reach MA at all
        assert not events.follows(location)
        await _notify(events)
    assert events.follows(location)
    changes.assert_called_once_with(location, {"InstanceID": "0", "Volume": "44", "Mute": "0"})


async def test_a_renderer_that_refuses_is_not_followed(
    served: Callable[[_Renderer], Awaitable[str]],
) -> None:
    """A refused subscription leaves the renderer to be read as before, and is tried again."""
    renderer = _Renderer()
    renderer.subscribe_status = 500
    location = await served(renderer)
    async with aiohttp.ClientSession() as session:
        events = RendererEvents(
            _mass(session), ROOM_EVENTS_PATH, SERVICE_RENDERING_CONTROL, MagicMock()
        )
        await events.refresh([location])
        assert not events.follows(location)
        renderer.subscribe_status = 200
        await events.refresh([location])
    assert [method for method, _ in renderer.requests] == ["SUBSCRIBE", "SUBSCRIBE"]


async def test_a_lost_subscription_stops_the_following(
    served: Callable[[_Renderer], Awaitable[str]],
) -> None:
    """A renewal the renderer refuses (it restarted, say) drops it until it subscribes again."""
    renderer = _Renderer()
    location = await served(renderer)
    async with aiohttp.ClientSession() as session:
        events = RendererEvents(
            _mass(session), ROOM_EVENTS_PATH, SERVICE_RENDERING_CONTROL, MagicMock()
        )
        await events.refresh([location])
        await _notify(events)
        assert events.follows(location)
        # renewed halfway through the 300 seconds the renderer granted
        assert events._subscriptions[location].renew_at == pytest.approx(time.time() + 150, abs=5)
        events._subscriptions[location].renew_at = 0.0
        renderer.subscribe_status = 412
        await events.refresh([location])
    assert not events.follows(location)


async def test_a_renderer_no_longer_wanted_is_unsubscribed(
    served: Callable[[_Renderer], Awaitable[str]],
) -> None:
    """A renderer that left the rooms is told to stop sending events."""
    renderer = _Renderer()
    location = await served(renderer)
    async with aiohttp.ClientSession() as session:
        events = RendererEvents(
            _mass(session), ROOM_EVENTS_PATH, SERVICE_RENDERING_CONTROL, MagicMock()
        )
        await events.refresh([location])
        await events.refresh([])
    assert [method for method, _ in renderer.requests] == ["SUBSCRIBE", "UNSUBSCRIBE"]
    assert not events.follows(location)


async def test_close_ends_the_subscriptions_and_the_route(
    served: Callable[[_Renderer], Awaitable[str]],
) -> None:
    """Closing unsubscribes and takes the event route off MA's stream server."""
    renderer = _Renderer()
    location = await served(renderer)
    async with aiohttp.ClientSession() as session:
        mass = _mass(session)
        events = RendererEvents(mass, ROOM_EVENTS_PATH, SERVICE_RENDERING_CONTROL, MagicMock())
        await events.refresh([location])
        await events.close()
    assert renderer.requests[-1][0] == "UNSUBSCRIBE"
    mass.streams.register_dynamic_route.return_value.assert_called_once_with()


async def test_the_route_is_removed_even_when_an_unsubscribe_hangs(
    served: Callable[[_Renderer], Awaitable[str]],
) -> None:
    """A renderer that no longer answers cannot keep the event route on the stream server."""
    renderer = _Renderer()
    location = await served(renderer)
    async with aiohttp.ClientSession() as session:
        mass = _mass(session)
        events = RendererEvents(mass, ROOM_EVENTS_PATH, SERVICE_RENDERING_CONTROL, MagicMock())
        await events.refresh([location])
        hung = asyncio.Event()

        async def hang(*_: object, **__: object) -> None:
            await hung.wait()

        with (
            patch.object(events._handler, "async_unsubscribe", hang),
            pytest.raises(TimeoutError),
        ):
            # the unload's time limit runs out while the renderer does not answer
            await asyncio.wait_for(events.close(), 0.1)
    mass.streams.register_dynamic_route.return_value.assert_called_once_with()


async def test_an_unreadable_event_is_refused() -> None:
    """An event that is not XML is answered with an error, not raised."""
    mass = MagicMock()
    events = RendererEvents(mass, ROOM_EVENTS_PATH, SERVICE_RENDERING_CONTROL, MagicMock())
    request = MagicMock()
    request.method = "NOTIFY"
    request.url = f"http://192.168.1.124:8097{ROOM_EVENTS_PATH}"
    request.headers = {"NT": "upnp:event", "NTS": "upnp:propchange", "SID": "uuid:unknown"}
    request.read = AsyncMock(return_value=b"<not xml")
    # an unknown SID is kept for a subscription that is still being made, so give it one
    service = MagicMock()
    events._handler._subscriptions["uuid:unknown"] = service
    response = await events._handle_request(request)
    assert response.status == 400
    service.notify_changed_state_variables.assert_not_called()
