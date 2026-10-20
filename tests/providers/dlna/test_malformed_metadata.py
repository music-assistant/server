"""
Repro test for support#5787.

Some DLNA devices (e.g. Bose SoundTouch) return malformed DIDL-Lite XML in
their track metadata. Accessing the metadata properties on the underlying
DmrDevice then raises an XML ParseError, which must not propagate out of
set_dynamic_attributes (it would otherwise kill the player update task).
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, PropertyMock
from xml.etree.ElementTree import ParseError

import pytest
from async_upnp_client.profiles.dlna import TransportState

from music_assistant.providers.dlna.player import DLNAPlayer
from tests.common import MockProvider


@pytest.mark.asyncio
async def test_set_dynamic_attributes_survives_malformed_device_metadata() -> None:
    """Malformed DIDL metadata from the device must not crash the player update."""
    provider = MockProvider("dlna", instance_id="dlna_test")
    provider.mass.streams.base_url = "http://192.168.1.2:8097"

    # subclass so the PropertyMocks stay local and don't leak into MagicMock itself
    class MockDevice(MagicMock):
        pass

    device = MockDevice()
    device.profile_device.available = True
    device.name = "Bose SoundTouch"
    device.volume_level = 0.5
    device.is_volume_muted = False
    device.transport_state = TransportState.PLAYING
    device.current_track_uri = "http://192.168.1.10/stream.mp3"
    device.media_position = None
    # malformed XML in CurrentTrackMetaData raises on property access
    for prop in ("media_title", "media_artist", "media_album_name", "media_image_url"):
        setattr(
            MockDevice,
            prop,
            PropertyMock(side_effect=ParseError("not well-formed (invalid token)")),
        )

    player = DLNAPlayer(
        provider,  # type: ignore[arg-type]
        "uuid:bose-player",
        "http://192.168.1.10/description.xml",
        device=device,
    )

    await player.set_dynamic_attributes()

    # metadata is dropped, but the update must complete and keep core attributes
    assert player.available is True
    assert player.current_media is not None
    assert player.current_media.uri == "http://192.168.1.10/stream.mp3"
    assert player.current_media.title is None


@pytest.mark.asyncio
async def test_poll_survives_malformed_device_metadata() -> None:
    """A ParseError raised while polling the device must not crash the poll."""
    provider = MockProvider("dlna", instance_id="dlna_test")
    provider.mass.streams.base_url = "http://192.168.1.2:8097"

    device = MagicMock()
    device.profile_device.available = True
    device.name = "Bose SoundTouch"
    # async_update parses CurrentTrackMetaData eagerly and raises on bad XML
    device.async_update = AsyncMock(side_effect=ParseError("not well-formed (invalid token)"))

    player = DLNAPlayer(
        provider,  # type: ignore[arg-type]
        "uuid:bose-player",
        "http://192.168.1.10/description.xml",
        device=device,
    )
    player.force_poll = True

    await player.poll()

    device.async_update.assert_awaited_once()


@pytest.mark.asyncio
async def test_set_dynamic_attributes_propagates_valid_device_metadata() -> None:
    """Valid device metadata must be propagated into the player's current media."""
    provider = MockProvider("dlna", instance_id="dlna_test")
    provider.mass.streams.base_url = "http://192.168.1.2:8097"

    device = MagicMock()
    device.profile_device.available = True
    device.name = "Bose SoundTouch"
    device.volume_level = 0.5
    device.is_volume_muted = False
    device.transport_state = TransportState.PLAYING
    device.current_track_uri = "http://192.168.1.10/stream.mp3"
    device.media_position = 42
    device.media_position_updated_at = datetime(2026, 7, 9, 12, 0, 0, tzinfo=UTC)
    device.media_title = "Test Title"
    device.media_artist = "Test Artist"
    device.media_album_name = "Test Album"
    device.media_image_url = "http://192.168.1.10/cover.jpg"
    device.media_duration = 180

    player = DLNAPlayer(
        provider,  # type: ignore[arg-type]
        "uuid:bose-player",
        "http://192.168.1.10/description.xml",
        device=device,
    )

    await player.set_dynamic_attributes()

    assert player.current_media is not None
    assert player.current_media.uri == "http://192.168.1.10/stream.mp3"
    assert player.current_media.title == "Test Title"
    assert player.current_media.artist == "Test Artist"
    assert player.current_media.album == "Test Album"
    assert player.current_media.image_url == "http://192.168.1.10/cover.jpg"
    assert player.current_media.duration == 180
    assert player.elapsed_time == 42.0
