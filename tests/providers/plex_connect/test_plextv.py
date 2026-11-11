"""Tests for the plex.tv device registration client of the Plex Connect plugin."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import ClientResponse

from music_assistant.providers.plex_connect.plextv import (
    PlexPin,
    PlexTvAuthError,
    PlexTvClient,
    PlexTvError,
    PlexTvIdentity,
    PlexTvPinExpiredError,
    build_version,
    compute_client_id,
)

CLIENT_ID = compute_client_id("plexprov1", "player1")

DEVICES_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<MediaContainer publicAddress="1.2.3.4">
  <Device id="111" name="Other Player" clientIdentifier="some-other-client"/>
  <Device id="222" name="Living Room" clientIdentifier="{CLIENT_ID}"/>
</MediaContainer>
"""


def _response(
    status: int = 200,
    json_data: dict[str, object] | None = None,
    text_data: str = "",
) -> AsyncMock:
    """Return a mocked aiohttp response."""
    response = AsyncMock(spec=ClientResponse)
    response.status = status
    response.json.return_value = json_data
    response.text.return_value = text_data
    return response


def _mock_session(*responses: AsyncMock) -> MagicMock:
    """Return a mocked aiohttp session that yields the given responses in order."""
    session = MagicMock()
    contexts = []
    for response in responses:
        context = AsyncMock()
        context.__aenter__.return_value = response
        contexts.append(context)
    session.request = MagicMock(side_effect=contexts)
    return session


@pytest.fixture
def identity() -> PlexTvIdentity:
    """Return a player identity for testing."""
    return PlexTvIdentity(client_id=CLIENT_ID, name="Living Room", version="2.10.0")


def test_compute_client_id_matches_uuid5_formula() -> None:
    """The client id must match the uuid5 formula used by the companion server."""
    expected = str(uuid.uuid5(uuid.NAMESPACE_DNS, "music-assistant-plex-plexprov1-player1"))
    assert compute_client_id("plexprov1", "player1") == expected
    # deterministic across calls
    assert compute_client_id("plexprov1", "player1") == expected


def test_build_version_fallback() -> None:
    """Dev builds ("0.0.0") must advertise the "1.0.0" fallback."""
    assert build_version("2.10.0") == "2.10.0"
    assert build_version("0.0.0") == "1.0.0"


def test_identity_headers_complete(identity: PlexTvIdentity) -> None:
    """All identity headers must be present with the expected values."""
    headers = identity.headers
    assert headers["X-Plex-Client-Identifier"] == CLIENT_ID
    assert headers["X-Plex-Product"] == "Music Assistant"
    assert headers["X-Plex-Version"] == "2.10.0"
    assert headers["X-Plex-Device-Name"] == "Living Room"
    assert headers["X-Plex-Device"] == "Music Assistant"
    assert headers["X-Plex-Model"] == "standalone"
    assert headers["X-Plex-Provides"] == "client,player,pubsub-player"
    assert headers["X-Plex-Platform"]


async def test_create_pin_success(identity: PlexTvIdentity) -> None:
    """A PIN is created via POST with the identity headers and strong=false body."""
    session = _mock_session(_response(201, json_data={"id": 12345, "code": "ABCD"}))
    client = PlexTvClient(session, identity)

    pin = await client.create_pin()

    assert pin == PlexPin(id=12345, code="ABCD")
    call = session.request.call_args
    assert call.args[0] == "POST"
    assert str(call.args[1]) == "https://plex.tv/api/v2/pins"
    assert call.kwargs["data"] == b"strong=false"
    assert call.kwargs["headers"]["Accept"] == "application/json"
    assert call.kwargs["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert call.kwargs["headers"]["X-Plex-Client-Identifier"] == CLIENT_ID


async def test_create_pin_http_error_raises(identity: PlexTvIdentity) -> None:
    """A non-2xx response on PIN creation raises PlexTvError."""
    session = _mock_session(_response(500, json_data=None))
    client = PlexTvClient(session, identity)

    with pytest.raises(PlexTvError):
        await client.create_pin()


async def test_check_pin_pending_returns_none(identity: PlexTvIdentity) -> None:
    """A PIN without authToken (null or empty) is still pending."""
    session = _mock_session(
        _response(200, json_data={"id": 12345, "authToken": None}),
        _response(200, json_data={"id": 12345, "authToken": ""}),
    )
    client = PlexTvClient(session, identity)

    assert await client.check_pin(12345) is None
    assert await client.check_pin(12345) is None
    call = session.request.call_args
    assert call.args[0] == "GET"
    assert str(call.args[1]) == "https://plex.tv/api/v2/pins/12345"


async def test_check_pin_linked_returns_token(identity: PlexTvIdentity) -> None:
    """A confirmed PIN returns the device token."""
    session = _mock_session(_response(200, json_data={"id": 12345, "authToken": "devtoken"}))
    client = PlexTvClient(session, identity)

    assert await client.check_pin(12345) == "devtoken"


async def test_check_pin_expired_raises(identity: PlexTvIdentity) -> None:
    """An expired (404) PIN raises PlexTvPinExpiredError."""
    session = _mock_session(_response(404, json_data=None))
    client = PlexTvClient(session, identity)

    with pytest.raises(PlexTvPinExpiredError):
        await client.check_pin(12345)


async def test_get_device_id_parses_devices_xml(identity: PlexTvIdentity) -> None:
    """The device id is looked up by clientIdentifier in devices.xml."""
    session = _mock_session(_response(200, text_data=DEVICES_XML))
    client = PlexTvClient(session, identity)

    assert await client.get_device_id("devtoken") == "222"
    call = session.request.call_args
    assert call.args[0] == "GET"
    assert str(call.args[1]) == "https://plex.tv/devices.xml"
    assert call.kwargs["headers"]["X-Plex-Token"] == "devtoken"


async def test_get_device_id_not_found_returns_none(identity: PlexTvIdentity) -> None:
    """A missing registration returns None instead of raising."""
    xml = '<MediaContainer><Device id="111" clientIdentifier="other"/></MediaContainer>'
    session = _mock_session(_response(200, text_data=xml))
    client = PlexTvClient(session, identity)

    assert await client.get_device_id("devtoken") is None


async def test_get_device_id_401_raises_auth_error(identity: PlexTvIdentity) -> None:
    """A revoked token (401) raises PlexTvAuthError."""
    session = _mock_session(_response(401))
    client = PlexTvClient(session, identity)

    with pytest.raises(PlexTvAuthError):
        await client.get_device_id("devtoken")


async def test_publish_connection_url_exact(identity: PlexTvIdentity) -> None:
    """The connection URI is published with the exact encoding plex.tv expects."""
    session = _mock_session(_response(200))
    client = PlexTvClient(session, identity)

    await client.publish_connection("devtoken", "222", "http://192.168.1.10:32500")

    call = session.request.call_args
    assert call.args[0] == "PUT"
    assert str(call.args[1]) == (
        "https://plex.tv/devices/222?Connection%5B%5D%5Buri%5D=http%3A%2F%2F192.168.1.10%3A32500"
    )
    assert call.kwargs["headers"]["X-Plex-Token"] == "devtoken"


async def test_publish_connection_401_raises_auth_error(identity: PlexTvIdentity) -> None:
    """A revoked token (401) on publish raises PlexTvAuthError."""
    session = _mock_session(_response(401))
    client = PlexTvClient(session, identity)

    with pytest.raises(PlexTvAuthError):
        await client.publish_connection("devtoken", "222", "http://192.168.1.10:32500")


async def test_delete_device_calls_delete_xml_url(identity: PlexTvIdentity) -> None:
    """Device removal targets the /devices/{id}.xml endpoint with the token."""
    session = _mock_session(_response(200))
    client = PlexTvClient(session, identity)

    await client.delete_device("devtoken", "222")

    call = session.request.call_args
    assert call.args[0] == "DELETE"
    assert str(call.args[1]) == "https://plex.tv/devices/222.xml"
    assert call.kwargs["headers"]["X-Plex-Token"] == "devtoken"
