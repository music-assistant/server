"""Tests for VK Music parsers."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from vkpymusic.models import Playlist as VKPlaylist
from vkpymusic.models import Song as VKSong

from music_assistant.providers.vk_music.parsers import (
    _create_synthetic_artist,
    _create_synthetic_artist_id,
    parse_artist,
    parse_playlist,
    parse_track,
)
from music_assistant.providers.vk_music.provider import VKMusicProvider

if TYPE_CHECKING:
    from .conftest import ProviderStub


class TestCreateSyntheticArtistId:
    """Tests for _create_synthetic_artist_id."""

    def test_returns_artist_name_directly(self) -> None:
        """Test that synthetic artist ID is the artist name itself."""
        assert _create_synthetic_artist_id("Test Artist") == "Test Artist"

    def test_different_names_produce_different_ids(self) -> None:
        """Test that different names produce different IDs."""
        id1 = _create_synthetic_artist_id("Artist One")
        id2 = _create_synthetic_artist_id("Artist Two")
        assert id1 != id2

    def test_same_name_produces_same_id(self) -> None:
        """Test deterministic ID generation."""
        id1 = _create_synthetic_artist_id("Same Artist")
        id2 = _create_synthetic_artist_id("Same Artist")
        assert id1 == id2

    def test_special_characters_preserved(self) -> None:
        """Test that special characters in names are preserved."""
        name = "DJ Shadow & Cut Chemist"
        assert _create_synthetic_artist_id(name) == name


class TestCreateSyntheticArtist:
    """Tests for _create_synthetic_artist."""

    def test_creates_item_mapping(self, provider_stub: ProviderStub) -> None:
        """Test that a valid ItemMapping is created."""
        result = _create_synthetic_artist(cast("VKMusicProvider", provider_stub), "Test Artist")
        assert result.item_id == "Test Artist"
        assert result.name == "Test Artist"
        assert result.provider == provider_stub.instance_id


class TestParseArtist:
    """Tests for parse_artist."""

    def test_parse_artist_basic(self, provider_stub: ProviderStub) -> None:
        """Test parsing a basic artist."""
        result = parse_artist(cast("VKMusicProvider", provider_stub), "My Artist")
        assert result.item_id == "My Artist"
        assert result.name == "My Artist"
        assert result.provider == provider_stub.instance_id
        assert len(result.provider_mappings) == 1
        mapping = next(iter(result.provider_mappings))
        assert mapping.item_id == "My Artist"
        assert mapping.provider_domain == "vk_music"
        assert mapping.provider_instance == provider_stub.instance_id


class TestParseTrack:
    """Tests for parse_track."""

    def test_parse_track_basic(self, provider_stub: ProviderStub) -> None:
        """Test parsing a basic track."""
        song = VKSong(
            title="Test Song",
            artist="Test Artist",
            duration=240,
            track_id="456",
            owner_id="123",
            url="https://example.com/audio.mp3",
        )
        result = parse_track(cast("VKMusicProvider", provider_stub), song)
        assert result.item_id == "123_456"
        assert result.name == "Test Song"
        assert result.duration == 240
        assert result.provider == provider_stub.instance_id
        assert len(result.provider_mappings) == 1
        mapping = next(iter(result.provider_mappings))
        assert mapping.item_id == "123_456"

    def test_parse_track_with_artist(self, provider_stub: ProviderStub) -> None:
        """Test parsing a track populates artist."""
        song = VKSong(
            title="Song",
            artist="Singer",
            duration=180,
            track_id="1",
            owner_id="2",
            url="",
        )
        result = parse_track(cast("VKMusicProvider", provider_stub), song)
        assert len(result.artists) == 1
        assert result.artists[0].name == "Singer"

    def test_parse_track_no_artist(self, provider_stub: ProviderStub) -> None:
        """Test parsing a track with empty artist."""
        song = VKSong(
            title="Song",
            artist="",
            duration=180,
            track_id="1",
            owner_id="2",
            url="",
        )
        result = parse_track(cast("VKMusicProvider", provider_stub), song)
        assert len(result.artists) == 0

    def test_parse_track_version_extraction(self, provider_stub: ProviderStub) -> None:
        """Test that version is extracted from title."""
        song = VKSong(
            title="Song Name (Acoustic Version)",
            artist="Artist",
            duration=200,
            track_id="1",
            owner_id="2",
            url="",
        )
        result = parse_track(cast("VKMusicProvider", provider_stub), song)
        assert result.name == "Song Name"
        assert result.version == "Acoustic Version"

    def test_parse_track_unknown_title(self, provider_stub: ProviderStub) -> None:
        """Test parsing a track with empty title defaults to Unknown Track."""
        song = VKSong(
            title="",
            artist="Artist",
            duration=180,
            track_id="1",
            owner_id="2",
            url="",
        )
        result = parse_track(cast("VKMusicProvider", provider_stub), song)
        assert result.name == "Unknown Track"

    def test_parse_track_zero_duration(self, provider_stub: ProviderStub) -> None:
        """Test parsing a track with zero duration."""
        song = VKSong(
            title="Song",
            artist="Artist",
            duration=0,
            track_id="1",
            owner_id="2",
            url="",
        )
        result = parse_track(cast("VKMusicProvider", provider_stub), song)
        assert result.duration == 0


class TestParsePlaylist:
    """Tests for parse_playlist."""

    def test_parse_playlist_basic(self, provider_stub: ProviderStub) -> None:
        """Test parsing a basic playlist."""
        playlist = VKPlaylist(
            title="My Playlist",
            description="A nice playlist",
            photo="https://example.com/photo.jpg",
            count=10,
            followers=5,
            owner_id=12345,
            playlist_id=999,
            access_key="abc123",
        )
        result = parse_playlist(cast("VKMusicProvider", provider_stub), playlist)
        assert result.item_id == "12345_999_abc123"
        assert result.name == "My Playlist"
        assert result.owner == "Me"
        assert result.is_editable is False
        assert result.metadata.description == "A nice playlist"

    def test_parse_playlist_other_owner(self, provider_stub: ProviderStub) -> None:
        """Test parsing playlist from another user."""
        playlist = VKPlaylist(
            title="Their Playlist",
            description="",
            photo="",
            count=5,
            followers=0,
            owner_id=99999,
            playlist_id=1,
            access_key="key",
        )
        result = parse_playlist(cast("VKMusicProvider", provider_stub), playlist)
        assert result.owner == "VK Music"

    def test_parse_playlist_with_cover(self, provider_stub: ProviderStub) -> None:
        """Test parsing playlist with cover image."""
        playlist = VKPlaylist(
            title="Playlist",
            description="",
            photo="https://example.com/cover.jpg",
            count=10,
            followers=0,
            owner_id=12345,
            playlist_id=1,
            access_key="",
        )
        result = parse_playlist(cast("VKMusicProvider", provider_stub), playlist)
        assert result.metadata.images is not None
        assert len(result.metadata.images) == 1
        assert result.metadata.images[0].path == "https://example.com/cover.jpg"

    def test_parse_playlist_no_cover(self, provider_stub: ProviderStub) -> None:
        """Test parsing playlist without cover image."""
        playlist = VKPlaylist(
            title="Playlist",
            description="",
            photo="",
            count=10,
            followers=0,
            owner_id=12345,
            playlist_id=1,
            access_key="",
        )
        result = parse_playlist(cast("VKMusicProvider", provider_stub), playlist)
        assert result.metadata.images is None or len(result.metadata.images) == 0

    def test_parse_playlist_no_access_key(self, provider_stub: ProviderStub) -> None:
        """Test playlist ID format without access key."""
        playlist = VKPlaylist(
            title="Playlist",
            description="",
            photo="",
            count=10,
            followers=0,
            owner_id=12345,
            playlist_id=1,
            access_key="",
        )
        result = parse_playlist(cast("VKMusicProvider", provider_stub), playlist)
        assert result.item_id == "12345_1"

    def test_parse_playlist_unknown_title(self, provider_stub: ProviderStub) -> None:
        """Test parsing playlist with empty title."""
        playlist = VKPlaylist(
            title="",
            description="",
            photo="",
            count=0,
            followers=0,
            owner_id=12345,
            playlist_id=1,
            access_key="",
        )
        result = parse_playlist(cast("VKMusicProvider", provider_stub), playlist)
        assert result.name == "Unknown Playlist"
