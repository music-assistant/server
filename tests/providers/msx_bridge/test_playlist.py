"""Tests for MSX Playlist mapping."""

from __future__ import annotations

from unittest.mock import MagicMock, Mock

from music_assistant.providers.msx_bridge.mappers import map_tracks_to_msx_playlist
from music_assistant.providers.msx_bridge.provider import MSXBridgeProvider


def _mock_provider() -> MSXBridgeProvider:
    """Create a mock provider."""
    provider = MagicMock()
    provider.mass.metadata.get_image_url.return_value = "http://image.url"
    return provider


def _make_track(item_id: int, name: str, artist: str, duration: int) -> Mock:
    """Create a mock Track object."""
    track = Mock()
    track.item_id = item_id
    track.name = name
    track.artist_str = artist
    track.uri = f"library://track/{item_id}"
    track.image = f"img{item_id}"
    track.duration = duration
    return track


def test_map_tracks_to_msx_playlist_basic() -> None:
    """Test mapping tracks to MSX content page for playlist playback."""
    prov = _mock_provider()
    tracks = [
        _make_track(1, "Track 1", "Artist 1", 180),
        _make_track(2, "Track 2", "Artist 2", 200),
    ]

    content = map_tracks_to_msx_playlist(
        tracks=tracks,
        start_index=1,
        prefix="http://localhost",
        player_id="msx_1",
        provider=prov,
    )

    assert content.type == "list"
    # Auto-start at index 1
    assert content.action == "player:goto:index:1"
    assert content.items is not None
    assert len(content.items) == 2

    item0 = content.items[0]
    assert item0.title == "Track 1"
    assert item0.action is not None
    assert item0.action.startswith("audio:")
    assert "library%3A%2F%2Ftrack%2F1" in item0.action
    assert "/msx/audio/msx_1.mp3?" in item0.action
    assert "&from_playlist=1" in item0.action
    assert item0.player_label == "Track 1"
    assert item0.duration == 180
    assert item0.label is not None
    assert "3:00" in item0.label
    assert "Artist 1" in item0.label

    item1 = content.items[1]
    assert item1.title == "Track 2"
    assert item1.action is not None
    assert "library%3A%2F%2Ftrack%2F2" in item1.action


def test_map_tracks_to_msx_playlist_start_zero() -> None:
    """Test that start_index=0 uses player:play action."""
    prov = _mock_provider()
    tracks = [_make_track(1, "Track 1", "Artist 1", 120)]

    content = map_tracks_to_msx_playlist(
        tracks=tracks,
        start_index=0,
        prefix="http://localhost",
        player_id="msx_1",
        provider=prov,
    )

    assert content.action == "player:play"


def test_map_tracks_to_msx_playlist_with_device_param() -> None:
    """Test that device_param is appended to audio URLs."""
    prov = _mock_provider()
    tracks = [_make_track(1, "Track 1", "Artist 1", 120)]

    content = map_tracks_to_msx_playlist(
        tracks=tracks,
        start_index=0,
        prefix="http://localhost",
        player_id="msx_1",
        provider=prov,
        device_param="device_id=my_tv",
    )

    assert content.items is not None
    assert content.items[0].action is not None
    assert "device_id=my_tv" in content.items[0].action


def test_map_tracks_to_msx_playlist_empty() -> None:
    """Test mapping empty track list."""
    prov = _mock_provider()
    content = map_tracks_to_msx_playlist(
        tracks=[],
        start_index=0,
        prefix="http://localhost",
        player_id="msx_1",
        provider=prov,
    )

    assert content.items is not None
    assert len(content.items) == 0


def test_map_tracks_to_msx_playlist_serialization() -> None:
    """Test that content serializes correctly with aliases."""
    prov = _mock_provider()
    tracks = [_make_track(1, "Track 1", "Artist 1", 180)]

    content = map_tracks_to_msx_playlist(
        tracks=tracks,
        start_index=0,
        prefix="http://localhost",
        player_id="msx_1",
        provider=prov,
    )

    data = content.model_dump(by_alias=True, exclude_none=True)
    assert data["type"] == "list"
    assert "template" in data
    assert len(data["items"]) == 1
    assert data["items"][0]["playerLabel"] == "Track 1"
    assert data["items"][0]["action"].startswith("audio:")
