"""
Repro test for support#5787.

Some DLNA devices (e.g. Bose SoundTouch) return malformed DIDL-Lite XML in
their track metadata. Accessing the metadata properties on the underlying
DmrDevice then raises an XML ParseError, which must not propagate out of
set_dynamic_attributes (it would otherwise kill the player update task).
"""

from __future__ import annotations

from unittest.mock import MagicMock, PropertyMock
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

    device = MagicMock()
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
            type(device),
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
