"""Tests for the UPnP renderer SOAP handling."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

import pytest
from aiohttp.test_utils import TestClient, TestServer

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


async def test_play_callback_receives_prior_transport_state(
    client: TestClient[Request, Application],
    renderer: UPnPRenderer,
) -> None:
    """Play exposes the prior state before committing the PLAYING transition."""
    renderer.transport_state = "PAUSED_PLAYBACK"
    received_states: list[tuple[str, str]] = []

    async def _on_play(previous_state: str) -> None:
        received_states.append((previous_state, renderer.transport_state))

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
    """Test that Seek action returns success (no-op)."""
    resp = await client.post(
        "/AVTransport/control",
        headers={
            "SOAPACTION": '"urn:schemas-upnp-org:service:AVTransport:1#Seek"',
        },
        data="<Unit>REL_TIME</Unit><Target>00:01:30</Target>",
    )
    assert resp.status == 200


async def test_get_position_info(client: TestClient[Request, Application]) -> None:
    """GetPositionInfo returns a SOAP response containing RelTime."""
    resp = await client.post(
        "/AVTransport/control",
        headers={
            "SOAPACTION": '"urn:schemas-upnp-org:service:AVTransport:1#GetPositionInfo"',
        },
        data="<dummy/>",
    )
    assert resp.status == 200
    text = await resp.text()
    assert "RelTime" in text


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
