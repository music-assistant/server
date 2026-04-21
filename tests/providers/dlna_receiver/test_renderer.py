"""Tests for the UPnP renderer SOAP handling."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
from aiohttp.test_utils import TestClient, TestServer

from music_assistant.providers.dlna_receiver.renderer import UPnPRenderer


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
) -> AsyncGenerator[TestClient[TestServer], None]:
    """Create an aiohttp test client for the renderer."""
    server = TestServer(renderer._app)
    _client = TestClient(server)
    await _client.start_server()
    yield _client
    await _client.close()


async def test_device_description(client: TestClient[TestServer]) -> None:
    """GET /description.xml returns the MediaRenderer device XML."""
    resp = await client.get("/description.xml")
    assert resp.status == 200
    text = await resp.text()
    assert "MediaRenderer" in text
    assert "Test Renderer" in text


async def test_get_transport_info(client: TestClient[TestServer]) -> None:
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


async def test_set_volume(client: TestClient[TestServer], renderer: UPnPRenderer) -> None:
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


async def test_get_protocol_info(client: TestClient[TestServer]) -> None:
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


async def test_av_transport_scpd(client: TestClient[TestServer]) -> None:
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


async def test_rendering_control_scpd(client: TestClient[TestServer]) -> None:
    """RenderingControl SCPD exposes volume/mute actions and allowed ranges."""
    resp = await client.get("/RenderingControl/description.xml")
    assert resp.status == 200
    text = await resp.text()
    assert "GetVolume" in text
    assert "SetMute" in text
    assert "Volume" in text
    assert "allowedValueRange" in text


async def test_connection_manager_scpd(client: TestClient[TestServer]) -> None:
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


async def test_play_pause_stop(client: TestClient[TestServer], renderer: UPnPRenderer) -> None:
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


async def test_seek_action(client: TestClient[TestServer]) -> None:
    """Test that Seek action returns success (no-op)."""
    resp = await client.post(
        "/AVTransport/control",
        headers={
            "SOAPACTION": '"urn:schemas-upnp-org:service:AVTransport:1#Seek"',
        },
        data="<Unit>REL_TIME</Unit><Target>00:01:30</Target>",
    )
    assert resp.status == 200


async def test_get_position_info(client: TestClient[TestServer]) -> None:
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


async def test_get_connection_info(client: TestClient[TestServer]) -> None:
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


async def test_invalid_action(client: TestClient[TestServer]) -> None:
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
    client: TestClient[TestServer],
    renderer: UPnPRenderer,
) -> None:
    """A callback that raises ValueError causes a 716 SOAP fault and no state change.

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


async def test_set_mute(client: TestClient[TestServer], renderer: UPnPRenderer) -> None:
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
