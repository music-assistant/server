"""Test MusicMe provider parse methods."""

from __future__ import annotations

from typing import Any

from music_assistant_models.enums import ContentType, ImageType, MediaType
from music_assistant_models.media_items import Album, Artist, ItemMapping, Radio, Track

from music_assistant.providers.musicme.constants import WEB_BASE
from music_assistant.providers.musicme.provider import MusicMeProvider

# ---------------------------------------------------------------------------
# Fixture data — MusicMe API response shapes
# ---------------------------------------------------------------------------

ARTIST_OBJ: dict[str, Any] = {
    "id": 6726,
    "name": "Daft Punk",
    "img_url": "https://mmcdn-ng3.hosting-media.net/eyestoeye/144x56/6726.jpg",
    "uri": "/Daft-Punk/",
    "style": "7",
}

ALBUM_OBJ: dict[str, Any] = {
    "barcode": "5034644330297",
    "name": "Veridis Quo",
    "artists": [
        {"id": 6726, "name": "Daft Punk", "uri": "/Daft-Punk/"},
    ],
    "ntracks": 2,
    "duration": 510,
    "streetdate": "2024-12-13",
    "cover_url": "https://covers-ng2.hosting-media.net/jpg343/u5034644330297.jpg",
    "streamable": 2,
    "opencatalog": 1,
    "features": 99921,
    "style": 7,
    "uri": "/Daft-Punk/albums/Veridis-Quo-5034644330297.html",
}

TRACK_OBJ: dict[str, Any] = {
    "barcode": "5034644330297-01_02",
    "title": "Veridis quo",
    "duration": 346,
    "streamable": 2,
    "opencatalog": 1,
    "features": 99921,
    "artists": [
        {"id": 6726, "name": "Daft Punk", "uri": "/Daft-Punk/"},
    ],
    "album": "Veridis Quo",
    "cover_url": "https://covers-ng2.hosting-media.net/jpgr175/u5034644330297.jpg",
    "song_id": 222002,
    "plays": 635,
}

RADIO_OBJ: dict[str, Any] = {
    "id": "rd-1163935",
    "type": "radio",
    "name": "100% love",
    "img_url": "https://covers-ng4.hosting-media.net/jpg128/u0884977006131.jpg",
    "tile_url": "https://mmcdn-ng1.hosting-media.net/pict/radio/tile_radio_1163935.jpg",
    "preview_artists": [
        {"name": "Lou Reed", "id": 4187},
        {"name": "Jason Mraz", "id": 7746},
        {"name": "Air", "id": 11401},
    ],
}


# Provider fixture is provided by conftest.py

# ---------------------------------------------------------------------------
# Artist parsing
# ---------------------------------------------------------------------------


class TestParseArtist:
    """Tests for _parse_artist."""

    def test_basic_artist(self, provider: MusicMeProvider) -> None:
        """Test parsing a standard artist object."""
        artist = provider._parse_artist(ARTIST_OBJ)
        assert isinstance(artist, Artist)
        assert artist.item_id == "6726"
        assert artist.name == "Daft Punk"
        assert artist.provider == "musicme--test123"

    def test_artist_provider_mapping(self, provider: MusicMeProvider) -> None:
        """Test that provider mapping is set correctly."""
        artist = provider._parse_artist(ARTIST_OBJ)
        mapping = next(iter(artist.provider_mappings))
        assert mapping.provider_domain == "musicme"
        assert mapping.provider_instance == "musicme--test123"
        assert mapping.url == f"{WEB_BASE}/Daft-Punk/"

    def test_artist_image_uses_square_format(self, provider: MusicMeProvider) -> None:
        """Test that artist image uses the square r288 format, not the banner."""
        artist = provider._parse_artist(ARTIST_OBJ)
        images = artist.metadata.images
        assert images is not None
        assert len(images) > 0
        img = images[0]
        assert img.type == ImageType.THUMB
        assert "r288/6726.jpg" in img.path
        assert "eyestoeye" not in img.path

    def test_artist_without_uri(self, provider: MusicMeProvider) -> None:
        """Test parsing artist with no URI."""
        obj = {"id": 999, "name": "Unknown"}
        artist = provider._parse_artist(obj)
        assert artist.name == "Unknown"
        mapping = next(iter(artist.provider_mappings))
        assert mapping.url == ""


# ---------------------------------------------------------------------------
# Album parsing
# ---------------------------------------------------------------------------


class TestParseAlbum:
    """Tests for _parse_album."""

    def test_basic_album(self, provider: MusicMeProvider) -> None:
        """Test parsing a standard album object."""
        album = provider._parse_album(ALBUM_OBJ)
        assert isinstance(album, Album)
        assert album.item_id == "5034644330297"
        assert album.name == "Veridis Quo"
        assert album.year == 2024

    def test_album_artists(self, provider: MusicMeProvider) -> None:
        """Test that album artists are parsed."""
        album = provider._parse_album(ALBUM_OBJ)
        assert len(album.artists) == 1
        assert album.artists[0].name == "Daft Punk"

    def test_album_cover(self, provider: MusicMeProvider) -> None:
        """Test album cover image."""
        album = provider._parse_album(ALBUM_OBJ)
        images = album.metadata.images
        assert images is not None
        assert len(images) > 0
        assert "u5034644330297.jpg" in images[0].path

    def test_album_streamable(self, provider: MusicMeProvider) -> None:
        """Test that streamable flag is correctly mapped."""
        album = provider._parse_album(ALBUM_OBJ)
        mapping = next(iter(album.provider_mappings))
        assert mapping.available is True

    def test_album_not_streamable(self, provider: MusicMeProvider) -> None:
        """Test album with streamable != 0 is marked unavailable."""
        obj = {**ALBUM_OBJ, "streamable": 0}
        album = provider._parse_album(obj)
        mapping = next(iter(album.provider_mappings))
        assert mapping.available is False

    def test_album_audio_format(self, provider: MusicMeProvider) -> None:
        """Test that audio format is AAC 44.1kHz."""
        album = provider._parse_album(ALBUM_OBJ)
        mapping = next(iter(album.provider_mappings))
        assert mapping.audio_format.content_type == ContentType.MP4
        assert mapping.audio_format.sample_rate == 44100

    def test_album_without_streetdate(self, provider: MusicMeProvider) -> None:
        """Test album with no streetdate."""
        obj = {k: v for k, v in ALBUM_OBJ.items() if k != "streetdate"}
        album = provider._parse_album(obj)
        assert album.year is None


# ---------------------------------------------------------------------------
# Track parsing
# ---------------------------------------------------------------------------


class TestParseTrack:
    """Tests for _parse_track."""

    def test_basic_track(self, provider: MusicMeProvider) -> None:
        """Test parsing a standard track object."""
        track = provider._parse_track(TRACK_OBJ)
        assert isinstance(track, Track)
        assert track.item_id == "5034644330297-01_02"
        assert track.name == "Veridis quo"
        assert track.duration == 346

    def test_track_disc_and_number(self, provider: MusicMeProvider) -> None:
        """Test disc and track number extraction from barcode."""
        track = provider._parse_track(TRACK_OBJ)
        assert track.disc_number == 1
        assert track.track_number == 2

    def test_track_artists(self, provider: MusicMeProvider) -> None:
        """Test that track artists are parsed."""
        track = provider._parse_track(TRACK_OBJ)
        assert len(track.artists) == 1
        assert track.artists[0].name == "Daft Punk"

    def test_track_album_mapping(self, provider: MusicMeProvider) -> None:
        """Test that track has an ItemMapping for its album."""
        track = provider._parse_track(TRACK_OBJ)
        assert track.album is not None
        assert isinstance(track.album, ItemMapping)
        assert track.album.item_id == "5034644330297"
        assert track.album.name == "Veridis Quo"
        assert track.album.media_type == MediaType.ALBUM

    def test_track_cover(self, provider: MusicMeProvider) -> None:
        """Test track cover image."""
        track = provider._parse_track(TRACK_OBJ)
        images = track.metadata.images
        assert images is not None
        assert len(images) > 0
        assert "u5034644330297.jpg" in images[0].path

    def test_track_without_album(self, provider: MusicMeProvider) -> None:
        """Test track with no album field."""
        obj = {k: v for k, v in TRACK_OBJ.items() if k != "album"}
        track = provider._parse_track(obj)
        assert track.album is None


# ---------------------------------------------------------------------------
# Radio parsing
# ---------------------------------------------------------------------------


class TestParseRadio:
    """Tests for _parse_radio."""

    def test_basic_radio(self, provider: MusicMeProvider) -> None:
        """Test parsing a standard radio object."""
        radio = provider._parse_radio(RADIO_OBJ)
        assert isinstance(radio, Radio)
        assert radio.item_id == "rd-1163935"
        assert radio.name == "100% love"

    def test_radio_prefers_tile_url(self, provider: MusicMeProvider) -> None:
        """Test that tile_url is preferred over img_url for the image."""
        radio = provider._parse_radio(RADIO_OBJ)
        images = radio.metadata.images
        assert images is not None
        assert len(images) > 0
        assert "tile_radio_1163935" in images[0].path

    def test_radio_falls_back_to_img_url(self, provider: MusicMeProvider) -> None:
        """Test fallback to img_url when tile_url is absent."""
        obj = {k: v for k, v in RADIO_OBJ.items() if k != "tile_url"}
        radio = provider._parse_radio(obj)
        images = radio.metadata.images
        assert images is not None
        assert len(images) > 0
        assert "jpg128" in images[0].path

    def test_radio_description_from_preview_artists(self, provider: MusicMeProvider) -> None:
        """Test that description is built from preview artists."""
        radio = provider._parse_radio(RADIO_OBJ)
        desc = radio.metadata.description
        assert desc is not None
        assert "Lou Reed" in desc
        assert "Jason Mraz" in desc
        assert desc.endswith("...")
