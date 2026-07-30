"""Tests for MSX mappers."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from music_assistant_models.media_items import Album, Track

from music_assistant.providers.msx_bridge.mappers import (
    map_album_to_msx,
    map_track_to_msx,
)
from music_assistant.providers.msx_bridge.provider import MSXBridgeProvider


def _mock_provider() -> MSXBridgeProvider:
    """Create a mock provider."""
    provider = MagicMock()
    provider.mass.metadata.get_image_url.return_value = "http://image.url"
    return provider


def test_map_track_to_msx() -> None:
    """Test mapping a track to MSX item."""
    prov = _mock_provider()
    track = MagicMock(spec=Track)
    track.name = "Test Track"
    track.uri = "library://track/1"
    track.duration = 125
    track.artist_str = "Test Artist"
    track.image = "some_image"

    item = map_track_to_msx(
        track=track,
        prefix="http://localhost",
        player_id="msx_123",
        provider=prov,
        device_param="device_id=abc",
    )

    assert item.title_header == "{txt:msx-white:Test Track}"
    assert item.title_footer == "Test Artist · 2:05"
    assert item.image == "http://image.url"
    assert item.action is not None
    assert "audio:http://localhost/msx/audio/msx_123" in item.action
    assert "uri=library%3A%2F%2Ftrack%2F1" in item.action
    assert "device_id=abc" in item.action


@pytest.mark.asyncio
async def test_map_album_to_msx() -> None:
    """Test mapping an album to MSX item."""
    prov = _mock_provider()
    album = MagicMock(spec=Album)
    album.name = "Test Album"
    album.item_id = "1"
    album.provider = "library"
    album.artist_str = "Test Artist"
    album.image = "album_image"

    item = await map_album_to_msx(
        album=album,
        prefix="http://localhost",
        provider=prov,
        device_param="device_id=abc",
    )

    assert item.title == "Test Album"
    # Mock has no year attribute set, so footer is "Artist · year" only if year exists
    assert "Test Artist" in (item.title_footer or "")
    assert item.image == "http://image.url"
    assert (
        item.action
        == "content:http://localhost/msx/albums/1/tracks.json?provider=library&device_id=abc"
    )
