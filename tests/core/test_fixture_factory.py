"""Tests for the fixture factory builder functions."""

from tests.support.fixture_factory import make_album, make_artist, make_playlist, make_track


def test_make_track_defaults() -> None:
    """Track builder returns sensible defaults."""
    track = make_track()
    assert track.item_id == "test-track-1"
    assert track.name == "Test Track"


def test_make_track_custom() -> None:
    """Track builder accepts custom values."""
    track = make_track(item_id="custom-1", name="My Song")
    assert track.item_id == "custom-1"
    assert track.name == "My Song"


def test_make_album_defaults() -> None:
    """Album builder returns sensible defaults."""
    album = make_album()
    assert album.item_id == "test-album-1"


def test_make_artist_defaults() -> None:
    """Artist builder returns sensible defaults."""
    artist = make_artist()
    assert artist.item_id == "test-artist-1"


def test_make_playlist_defaults() -> None:
    """Playlist builder returns sensible defaults."""
    playlist = make_playlist()
    assert playlist.item_id == "test-playlist-1"
