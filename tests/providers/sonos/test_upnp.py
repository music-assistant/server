"""Tests for the UPnP of Sonos speakers: action calls and RenderingControl events."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from async_upnp_client.const import HttpRequest
from async_upnp_client.exceptions import UpnpError

from music_assistant.providers.sonos.const import (
    EVENTS_PATH,
    OPTIONS_EVENTS_POLL_INTERVAL,
    OPTIONS_POLL_INTERVAL,
    UPNP_TIMEOUT_LIMIT,
)
from music_assistant.providers.sonos.upnp import SonosSpeakerUpnp, SonosUpnp
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
 <actionList>
  <action>
   <name>GetBass</name>
   <argumentList>
    <argument>
     <name>InstanceID</name><direction>in</direction>
     <relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable>
    </argument>
    <argument>
     <name>CurrentBass</name><direction>out</direction>
     <relatedStateVariable>Bass</relatedStateVariable>
    </argument>
   </argumentList>
  </action>
 </actionList>
 <serviceStateTable>
  <stateVariable sendEvents="yes"><name>LastChange</name><dataType>string</dataType></stateVariable>
  <stateVariable sendEvents="no"><name>A_ARG_TYPE_InstanceID</name><dataType>ui4</dataType></stateVariable>
  <stateVariable sendEvents="no"><name>Bass</name><dataType>i2</dataType></stateVariable>
 </serviceStateTable>
</scpd>"""

_GET_BASS_REPLY = (
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
    '<u:GetBassResponse xmlns:u="urn:schemas-upnp-org:service:RenderingControl:1">'
    "<CurrentBass>3</CurrentBass></u:GetBassResponse></s:Body></s:Envelope>"
)

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


@dataclass
class FakeSpeaker:
    """A speaker that serves its description, answers GetBass and takes subscriptions."""

    upnp: SonosSpeakerUpnp | None = None
    mass: MagicMock = field(default_factory=MagicMock)
    subscribe_status: int = 200
    methods: list[str] = field(default_factory=list)
    callbacks: list[str] = field(default_factory=list)
    soap_bodies: list[str] = field(default_factory=list)

    @property
    def speaker(self) -> SonosSpeakerUpnp:
        """Return the speaker's UPnP."""
        assert self.upnp is not None
        return self.upnp

    async def handle(self, request: web.Request) -> web.Response:
        """Answer like a Sonos speaker on port 1400."""
        if request.path == "/xml/device_description.xml":
            return web.Response(text=_DESCRIPTION, content_type="text/xml")
        if request.path == "/xml/RenderingControl1.xml":
            return web.Response(text=_SCPD, content_type="text/xml")
        self.methods.append(request.method)
        if request.method == "POST":
            self.soap_bodies.append(await request.text())
            return web.Response(text=_GET_BASS_REPLY, content_type="text/xml")
        if request.method == "SUBSCRIBE":
            self.callbacks.append(request.headers.get("CALLBACK", ""))
            if self.subscribe_status != 200:
                return web.Response(status=self.subscribe_status)
            return web.Response(headers={"SID": "uuid:sid-1", "TIMEOUT": "Second-1800"})
        return web.Response()


@pytest.fixture
async def speaker(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FakeSpeaker]:
    """Serve a fake speaker and return it with its UPnP."""
    fake = FakeSpeaker()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", fake.handle)
    server = TestServer(app)
    await server.start_server()
    monkeypatch.setattr("music_assistant.providers.sonos.upnp.UPNP_PORT", server.port)
    async with aiohttp.ClientSession() as session:
        fake.mass.http_session_no_ssl = session
        fake.mass.streams.base_url = "http://192.168.1.10:8097"
        logger = logging.getLogger("test.sonos.upnp")
        fake.upnp = SonosSpeakerUpnp(SonosUpnp(fake.mass, logger), "127.0.0.1", logger)
        yield fake
    await server.close()


async def _notify(upnp: SonosSpeakerUpnp) -> None:
    """Deliver an event as the speaker would."""
    await upnp._upnp.event_handler.handle_notify(
        HttpRequest(
            "NOTIFY",
            f"http://192.168.1.10:8097{EVENTS_PATH}",
            {"NT": "upnp:event", "NTS": "upnp:propchange", "SID": "uuid:sid-1"},
            _EVENT,
        )
    )


async def test_an_action_of_the_embedded_service_is_called(speaker: FakeSpeaker) -> None:
    """RenderingControl is found in the embedded device; arguments and replies are converted."""
    result = await speaker.speaker.rendering_control("GetBass")

    assert result["CurrentBass"] == 3
    assert "<InstanceID>0</InstanceID>" in speaker.soap_bodies[0]
    # a missing service or action is a UpnpError, so a KeyError still means a bug
    with pytest.raises(UpnpError):
        await speaker.speaker.call("urn:schemas-upnp-org:service:HTControl:1", "GetIRRepeaterState")
    with pytest.raises(UpnpError):
        await speaker.speaker.rendering_control("GetTreble")


async def test_requests_stop_after_timeouts_in_a_row(speaker: FakeSpeaker) -> None:
    """After UPNP_TIMEOUT_LIMIT timeouts nothing is sent until the timeouts are reset."""
    upnp = speaker.speaker
    create_device = AsyncMock(side_effect=TimeoutError)
    with patch.object(upnp._upnp.factory, "async_create_device", create_device):
        for _ in range(UPNP_TIMEOUT_LIMIT):
            with pytest.raises(TimeoutError):
                await upnp.rendering_control("GetBass")
        with pytest.raises(UpnpError):
            await upnp.rendering_control("GetBass")
        await upnp.follow(MagicMock())
        answers_before_reset = upnp.answers
        upnp.reset_timeouts()

    assert not answers_before_reset
    assert create_device.await_count == UPNP_TIMEOUT_LIMIT
    assert (await upnp.rendering_control("GetBass"))["CurrentBass"] == 3
    assert upnp.answers


async def test_a_speaker_is_followed_once_its_first_event_came_in(speaker: FakeSpeaker) -> None:
    """The events go to MA's stream server; an event names the channel volumes apart."""
    on_change = MagicMock()

    await speaker.speaker.follow(on_change)
    # subscribed, but nothing came in yet: events may not reach MA at all
    followed_before_event = speaker.speaker.follows
    await _notify(speaker.speaker)

    assert speaker.callbacks == [f"<http://192.168.1.10:8097{EVENTS_PATH}>"]
    assert not followed_before_event
    assert speaker.speaker.follows
    on_change.assert_called_once_with(
        {"InstanceID": "0", "Volume": "40", "Volume_LF": "100", "Volume_RF": "80", "Bass": "3"}
    )


async def test_a_refused_or_lost_subscription_is_made_again(speaker: FakeSpeaker) -> None:
    """A refused subscribe or renewal leaves the speaker unfollowed until the next follow."""
    upnp = speaker.speaker
    speaker.subscribe_status = 500
    await upnp.follow(MagicMock())
    refused_subscription = upnp._events_service

    speaker.subscribe_status = 200
    await upnp.follow(MagicMock())
    await _notify(upnp)
    followed_after_retry = upnp.follows
    upnp._renew_at = 0.0
    speaker.subscribe_status = 412
    await upnp.follow(MagicMock())

    assert refused_subscription is None
    assert followed_after_retry
    assert not upnp.follows
    assert upnp._events_service is None


async def test_unfollow_ends_the_subscription(speaker: FakeSpeaker) -> None:
    """Unfollowing unsubscribes; closing takes the event route off MA's stream server."""
    await speaker.speaker.follow(MagicMock())

    await speaker.speaker.unfollow()
    speaker.speaker._upnp.close()

    assert speaker.methods[-1] == "UNSUBSCRIBE"
    speaker.mass.streams.register_dynamic_route.return_value.assert_called_once_with()


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
    ("follows", "show_device_settings", "interval", "polled_keys"),
    [
        (False, False, OPTIONS_POLL_INTERVAL, "all"),
        (True, False, OPTIONS_EVENTS_POLL_INTERVAL, None),
        (
            True,
            True,
            OPTIONS_POLL_INTERVAL,
            {"status_light", "touch_controls", "trueplay", "ir_repeater", "remote_led_feedback"},
        ),
    ],
    ids=["no_events", "events", "events_and_device_settings"],
)
async def test_poll_reads_only_what_the_events_miss(
    follows: bool, show_device_settings: bool, interval: int, polled_keys: str | set[str] | None
) -> None:
    """With events, only the opted-in device settings are polled; without, everything."""
    mass = MagicMock()
    mass.config.get_protocol_value.return_value = show_device_settings
    player, _ = _bind_player(mass)
    player.connected = True
    upnp: Any = player.upnp
    upnp.follows = follows

    with patch.object(player, "_update_options", AsyncMock()) as update_options:
        await player.poll()

    assert player.poll_interval == interval
    if polled_keys is None:
        update_options.assert_not_awaited()
    elif polled_keys == "all":
        update_options.assert_awaited_once_with()
    else:
        (options_read,) = update_options.await_args_list[0].args
        assert {option.key for option in options_read} == polled_keys
