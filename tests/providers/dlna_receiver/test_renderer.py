"""Tests for the UPnP renderer SOAP handling."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from music_assistant.providers.dlna_receiver.metadata import parse_didl_metadata
from music_assistant.providers.dlna_receiver.renderer import UPnPRenderer

if TYPE_CHECKING:
    from aiohttp.web import Application, Request


@pytest.fixture
def renderer() -> UPnPRenderer:
    """Create a test renderer instance."""
    return UPnPRenderer(
        friendly_name="Test Renderer",
        bind_ip="127.0.0.1",
        http_port=0,
    )


@pytest.fixture
async def client(
    renderer: UPnPRenderer,
) -> AsyncGenerator[TestClient[Request, Application]]:
    """Create an aiohttp test client for the renderer."""
    server = TestServer(renderer._app)
    _client = TestClient(server)
    await _client.start_server()
    yield _client
    await _client.close()


async def test_device_description(client: TestClient[Request, Application]) -> None:
    """GET /description.xml returns the MediaRenderer device XML."""
    resp = await client.get("/description.xml")
    assert resp.status == 200
    text = await resp.text()
    assert "MediaRenderer" in text
    assert "Test Renderer" in text


async def test_get_transport_info(client: TestClient[Request, Application]) -> None:
    """GetTransportInfo returns NO_MEDIA_PRESENT before any URI is set."""
    resp = await client.post(
        "/AVTransport/control",
        headers={
            "SOAPACTION": '"urn:schemas-upnp-org:service:AVTransport:1#GetTransportInfo"',
        },
        data="<dummy/>",
    )
    assert resp.status == 200
    text = await resp.text()
    assert "NO_MEDIA_PRESENT" in text


async def test_set_volume(client: TestClient[Request, Application], renderer: UPnPRenderer) -> None:
    """SetVolume updates renderer state and invokes the on_set_volume callback."""
    volume_received: list[int] = []

    async def _on_volume(v: int) -> None:
        volume_received.append(v)

    renderer.on_set_volume = _on_volume

    resp = await client.post(
        "/RenderingControl/control",
        headers={
            "SOAPACTION": '"urn:schemas-upnp-org:service:RenderingControl:1#SetVolume"',
        },
        data="<DesiredVolume>75</DesiredVolume>",
    )
    assert resp.status == 200
    assert renderer.volume == 75
    assert volume_received == [75]


async def test_get_protocol_info(client: TestClient[Request, Application]) -> None:
    """GetProtocolInfo advertises supported sink audio mime types."""
    resp = await client.post(
        "/ConnectionManager/control",
        headers={
            "SOAPACTION": '"urn:schemas-upnp-org:service:ConnectionManager:1#GetProtocolInfo"',
        },
        data="<dummy/>",
    )
    assert resp.status == 200
    text = await resp.text()
    assert "audio/flac" in text


# ------------------------------------------------------------------
# SCPD tests — verify full service descriptions are served
# ------------------------------------------------------------------


async def test_av_transport_scpd(client: TestClient[Request, Application]) -> None:
    """AVTransport SCPD exposes the expected actions and state variables."""
    resp = await client.get("/AVTransport/description.xml")
    assert resp.status == 200
    text = await resp.text()
    assert "SetAVTransportURI" in text
    assert "Play" in text
    assert "Seek" in text
    # Verify state variables are present (not just action names)
    assert "TransportState" in text
    assert "serviceStateTable" in text
    assert "argumentList" in text


async def test_rendering_control_scpd(client: TestClient[Request, Application]) -> None:
    """RenderingControl SCPD exposes volume/mute actions and allowed ranges."""
    resp = await client.get("/RenderingControl/description.xml")
    assert resp.status == 200
    text = await resp.text()
    assert "GetVolume" in text
    assert "SetMute" in text
    assert "Volume" in text
    assert "allowedValueRange" in text


async def test_connection_manager_scpd(client: TestClient[Request, Application]) -> None:
    """ConnectionManager SCPD exposes GetProtocolInfo and connection info."""
    resp = await client.get("/ConnectionManager/description.xml")
    assert resp.status == 200
    text = await resp.text()
    assert "GetProtocolInfo" in text
    assert "GetCurrentConnectionInfo" in text
    assert "SinkProtocolInfo" in text


# ------------------------------------------------------------------
# SOAP action tests
# ------------------------------------------------------------------


async def test_play_pause_stop(
    client: TestClient[Request, Application], renderer: UPnPRenderer
) -> None:
    """Test transport state transitions via SOAP actions."""
    # SetAVTransportURI
    resp = await client.post(
        "/AVTransport/control",
        headers={
            "SOAPACTION": '"urn:schemas-upnp-org:service:AVTransport:1#SetAVTransportURI"',
        },
        data="<CurrentURI>http://example.com/stream.flac</CurrentURI>",
    )
    assert resp.status == 200
    assert renderer.current_uri == "http://example.com/stream.flac"
    assert renderer.transport_state == "STOPPED"

    # Play
    resp = await client.post(
        "/AVTransport/control",
        headers={
            "SOAPACTION": '"urn:schemas-upnp-org:service:AVTransport:1#Play"',
        },
        data="<dummy/>",
    )
    assert resp.status == 200
    assert renderer.transport_state == "PLAYING"

    # Pause
    resp = await client.post(
        "/AVTransport/control",
        headers={
            "SOAPACTION": '"urn:schemas-upnp-org:service:AVTransport:1#Pause"',
        },
        data="<dummy/>",
    )
    assert resp.status == 200
    assert renderer.transport_state == "PAUSED_PLAYBACK"

    # Stop
    resp = await client.post(
        "/AVTransport/control",
        headers={
            "SOAPACTION": '"urn:schemas-upnp-org:service:AVTransport:1#Stop"',
        },
        data="<dummy/>",
    )
    assert resp.status == 200
    assert renderer.transport_state == "STOPPED"


async def test_set_av_transport_uri_preserves_escaped_didl_metadata(
    client: TestClient[Request, Application], renderer: UPnPRenderer
) -> None:
    """SOAP decoding leaves DIDL entities for the DIDL parser to decode once."""
    received: list[dict[str, str | None]] = []

    async def _capture(_uri: str, metadata: str | None) -> None:
        received.append(parse_didl_metadata(metadata))

    renderer.on_set_av_transport_uri = _capture
    body = """\
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">
  <s:Body>
    <u:SetAVTransportURI xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">
      <InstanceID>0</InstanceID>
      <CurrentURI>http://example.com/stream.flac</CurrentURI>
      <CurrentURIMetaData>&lt;DIDL-Lite xmlns=&quot;urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/&quot; xmlns:dc=&quot;http://purl.org/dc/elements/1.1/&quot;&gt;&lt;item&gt;&lt;dc:title&gt;Simon &amp;amp; Garfunkel&lt;/dc:title&gt;&lt;/item&gt;&lt;/DIDL-Lite&gt;</CurrentURIMetaData>
    </u:SetAVTransportURI>
  </s:Body>
</s:Envelope>
"""

    resp = await client.post(
        "/AVTransport/control",
        headers={
            "SOAPACTION": '"urn:schemas-upnp-org:service:AVTransport:1#SetAVTransportURI"',
        },
        data=body,
    )

    assert resp.status == 200
    assert received[0]["title"] == "Simon & Garfunkel"
    assert "Simon &amp; Garfunkel" in renderer.current_uri_metadata


async def test_play_callback_receives_prior_transport_state(
    client: TestClient[Request, Application],
    renderer: UPnPRenderer,
) -> None:
    """Play exposes the prior state before committing the PLAYING transition."""
    renderer.transport_state = "PAUSED_PLAYBACK"
    received_states: list[tuple[str, str]] = []

    async def _on_play(previous_state: str) -> bool:
        received_states.append((previous_state, renderer.transport_state))
        return True

    renderer.on_play = _on_play

    resp = await client.post(
        "/AVTransport/control",
        headers={
            "SOAPACTION": '"urn:schemas-upnp-org:service:AVTransport:1#Play"',
        },
        data="<dummy/>",
    )

    assert resp.status == 200
    assert received_states == [("PAUSED_PLAYBACK", "PAUSED_PLAYBACK")]
    assert renderer.transport_state == "PLAYING"


async def test_seek_action(client: TestClient[Request, Application]) -> None:
    """Unsupported Seek returns a standards-compliant SOAP fault."""
    resp = await client.post(
        "/AVTransport/control",
        headers={
            "SOAPACTION": '"urn:schemas-upnp-org:service:AVTransport:1#Seek"',
        },
        data="<Unit>REL_TIME</Unit><Target>00:01:30</Target>",
    )
    assert resp.status == 500
    text = await resp.text()
    assert "<errorCode>710</errorCode>" in text
    assert "Seek mode not supported" in text


async def test_get_position_info(
    client: TestClient[Request, Application],
    renderer: UPnPRenderer,
) -> None:
    """GetPositionInfo reports the provider's current duration and elapsed time."""
    renderer.on_get_position = lambda: (65, 245)
    resp = await client.post(
        "/AVTransport/control",
        headers={
            "SOAPACTION": '"urn:schemas-upnp-org:service:AVTransport:1#GetPositionInfo"',
        },
        data="<dummy/>",
    )
    assert resp.status == 200
    text = await resp.text()
    assert "<TrackDuration>00:04:05</TrackDuration>" in text
    assert "<RelTime>00:01:05</RelTime>" in text
    assert "<AbsTime>00:01:05</AbsTime>" in text


async def test_play_rejection_returns_701_without_state_change(
    client: TestClient[Request, Application],
    renderer: UPnPRenderer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected provider callback cannot advertise playback that never started."""
    renderer.transport_state = "STOPPED"
    notifications: list[dict[str, str]] = []

    async def _reject_play(_previous_state: str) -> bool:
        return False

    async def _record_notify(variables: dict[str, str]) -> None:
        notifications.append(variables)

    renderer.on_play = _reject_play
    monkeypatch.setattr(renderer._evt_av_transport, "notify", _record_notify)

    resp = await client.post(
        "/AVTransport/control",
        headers={
            "SOAPACTION": '"urn:schemas-upnp-org:service:AVTransport:1#Play"',
        },
        data="<dummy/>",
    )

    assert resp.status == 500
    assert "<errorCode>701</errorCode>" in await resp.text()
    assert renderer.transport_state == "STOPPED"
    assert notifications == []


async def test_set_transport_state_notifies_only_on_change(
    renderer: UPnPRenderer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """External Music Assistant state changes are delivered to GENA subscribers."""
    renderer.transport_state = "PLAYING"
    notifications: list[dict[str, str]] = []

    async def _record_notify(variables: dict[str, str]) -> None:
        notifications.append(variables)

    monkeypatch.setattr(renderer._evt_av_transport, "notify", _record_notify)

    await renderer.set_transport_state("STOPPED")
    await renderer.set_transport_state("STOPPED")

    assert renderer.transport_state == "STOPPED"
    assert len(notifications) == 1
    assert "STOPPED" in notifications[0]["LastChange"]


async def test_get_media_info_reports_provider_duration(
    client: TestClient[Request, Application],
    renderer: UPnPRenderer,
) -> None:
    """GetMediaInfo and GetPositionInfo expose the same track duration."""
    renderer.on_get_position = lambda: (65, 245)

    resp = await client.post(
        "/AVTransport/control",
        headers={
            "SOAPACTION": '"urn:schemas-upnp-org:service:AVTransport:1#GetMediaInfo"',
        },
        data="<dummy/>",
    )

    assert resp.status == 200
    assert "<MediaDuration>00:04:05</MediaDuration>" in await resp.text()


async def test_get_connection_info(client: TestClient[Request, Application]) -> None:
    """Test GetCurrentConnectionInfo action."""
    resp = await client.post(
        "/ConnectionManager/control",
        headers={
            "SOAPACTION": (
                '"urn:schemas-upnp-org:service:ConnectionManager:1#GetCurrentConnectionInfo"'
            ),
        },
        data="<ConnectionID>0</ConnectionID>",
    )
    assert resp.status == 200
    text = await resp.text()
    assert "Direction" in text
    assert "Input" in text


async def test_invalid_action(client: TestClient[Request, Application]) -> None:
    """Test that unknown actions return SOAP error."""
    resp = await client.post(
        "/AVTransport/control",
        headers={
            "SOAPACTION": '"urn:schemas-upnp-org:service:AVTransport:1#NonExistentAction"',
        },
        data="<dummy/>",
    )
    assert resp.status == 500
    text = await resp.text()
    assert "Invalid Action" in text


async def test_set_av_transport_uri_rejected(
    client: TestClient[Request, Application],
    renderer: UPnPRenderer,
) -> None:
    """
    A callback that raises ValueError causes a 716 SOAP fault and no state change.

    Previously the renderer eagerly wrote ``current_uri`` and returned 200 OK
    before invoking the callback, so a silent SSRF-guard rejection in the
    provider left control points thinking the URI was accepted.
    """
    renderer.current_uri = "http://prior.example/stream.flac"

    async def _reject(_uri: str, _metadata: str | None) -> None:
        raise ValueError("unsupported URI scheme or missing host")

    renderer.on_set_av_transport_uri = _reject

    resp = await client.post(
        "/AVTransport/control",
        headers={
            "SOAPACTION": '"urn:schemas-upnp-org:service:AVTransport:1#SetAVTransportURI"',
        },
        data="<CurrentURI>file:///etc/passwd</CurrentURI>",
    )
    assert resp.status == 500
    text = await resp.text()
    assert "<errorCode>716</errorCode>" in text
    # State was NOT mutated by the rejected request.
    assert renderer.current_uri == "http://prior.example/stream.flac"


async def test_subscribe_response_completes_before_slow_initial_notify(
    client: TestClient[Request, Application],
    renderer: UPnPRenderer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow callback cannot delay the SUBSCRIBE response carrying its SID."""
    import music_assistant.providers.dlna_receiver.eventing as eventing_module  # noqa: PLC0415

    callback_entered = asyncio.Event()
    release_callback = asyncio.Event()

    async def _allow_test_server(url: str) -> str:
        return url

    async def _slow_notify(_request: Request) -> web.Response:
        callback_entered.set()
        await release_callback.wait()
        return web.Response(status=200)

    monkeypatch.setattr(eventing_module, "validate_outbound_url", _allow_test_server)
    callback_app = web.Application()
    callback_app.router.add_route("NOTIFY", "/callback", _slow_notify)
    callback_server = TestServer(callback_app)
    await callback_server.start_server()
    await renderer._evt_av_transport.start()
    response_task = asyncio.create_task(
        client.request(
            "SUBSCRIBE",
            "/AVTransport/event",
            headers={"CALLBACK": f"<{callback_server.make_url('/callback')}>"},
        )
    )
    try:
        await asyncio.wait_for(callback_entered.wait(), timeout=1)
        response = await asyncio.wait_for(asyncio.shield(response_task), timeout=0.2)
        assert response.status == 200
        assert response.headers["SID"].startswith("uuid:")
    finally:
        release_callback.set()
        await response_task
        await renderer._evt_av_transport.stop()
        await callback_server.close()


def test_description_url_brackets_ipv6() -> None:
    """IPv6 bind_ip must be wrapped in brackets per RFC 3986 §3.2.2."""
    r = UPnPRenderer("ipv6 renderer", bind_ip="::1", http_port=9999)
    assert r.description_url == "http://[::1]:9999/description.xml"


def test_description_url_ipv4_no_brackets() -> None:
    """Plain IPv4 addresses are not bracketed."""
    r = UPnPRenderer("ipv4 renderer", bind_ip="192.168.1.5", http_port=8080)
    assert r.description_url == "http://192.168.1.5:8080/description.xml"


async def test_start_learns_ephemeral_port() -> None:
    """
    Binding on http_port=0 must update self.http_port from the bound socket.

    Without this, description_url and the SSDP LOCATION header advertise
    ``:0`` and nothing can reach the renderer.
    """
    r = UPnPRenderer("ephemeral", bind_ip="127.0.0.1", http_port=0)
    try:
        await r.start()
        assert r.http_port != 0
        assert 1 <= r.http_port <= 65535
        assert f":{r.http_port}" in r.description_url
    finally:
        await r.stop()


async def test_set_mute(client: TestClient[Request, Application], renderer: UPnPRenderer) -> None:
    """SetMute updates renderer state and GetMute reflects the change."""
    resp = await client.post(
        "/RenderingControl/control",
        headers={
            "SOAPACTION": '"urn:schemas-upnp-org:service:RenderingControl:1#SetMute"',
        },
        data="<DesiredMute>1</DesiredMute>",
    )
    assert resp.status == 200
    assert renderer.mute is True

    resp = await client.post(
        "/RenderingControl/control",
        headers={
            "SOAPACTION": '"urn:schemas-upnp-org:service:RenderingControl:1#GetMute"',
        },
        data="<dummy/>",
    )
    assert resp.status == 200
    text = await resp.text()
    assert "1" in text
