"""Test Qobuz provider parse methods."""

from copy import deepcopy
from typing import Any, cast
from unittest.mock import Mock

import pytest
from music_assistant_models.enums import AlbumType, ImageType
from music_assistant_models.errors import LoginFailed
from music_assistant_models.media_items import Album, Artist, Playlist, Track

from music_assistant.constants import VARIOUS_ARTISTS_MBID, VARIOUS_ARTISTS_NAME
from music_assistant.providers.qobuz import SUPPORTED_FEATURES, VARIOUS_ARTISTS_ID, QobuzProvider

# ---------------------------------------------------------------------------
# Module-level fixture data — API response shapes.
# These are treated as read-only constants. Every test that mutates data
# must use deepcopy() before modifying.
# TRACK_OBJ inlines its album dict to avoid a shared mutable reference.
# ---------------------------------------------------------------------------
ARTIST_OBJ: dict[str, Any] = {
    "id": 123456,
    "name": "Test Artist",
    "image": {"large": "https://static.qobuz.com/images/artist/large.jpg"},
    "biography": {"content": "A test biography"},
}

ALBUM_OBJ: dict[str, Any] = {
    "id": 789012,
    "title": "Test Album",
    "version": "",
    "artist": {"id": 123456, "name": "Test Artist"},
    "streamable": True,
    "displayable": True,
    "maximum_sampling_rate": 96,
    "maximum_bit_depth": 24,
    "upc": "1234567890123",
    "genre": {"name": "Rock"},
    "label": {"name": "Test Label"},
    "image": {"large": "https://static.qobuz.com/images/album/large.jpg"},
    "released_at": 1625097600,  # Jul 1 2021 UTC — yields year 2021 in any timezone
    "copyright": "(c) Test",
    "product_type": "album",
}

TRACK_OBJ: dict[str, Any] = {
    "id": 345678,
    "title": "Test Track",
    "version": "",
    "duration": 240,
    "streamable": True,
    "displayable": True,
    "maximum_sampling_rate": 96,
    "maximum_bit_depth": 24,
    "isrc": "USRC17607839",
    "performer": {"id": 123456, "name": "Test Artist"},
    "performers": "Test Artist, MainArtist - Someone Else, Producer",
    "media_number": 1,
    "track_number": 3,
    "album": {
        "id": 789012,
        "title": "Test Album",
        "version": "",
        "artist": {"id": 123456, "name": "Test Artist"},
        "streamable": True,
        "displayable": True,
        "maximum_sampling_rate": 96,
        "maximum_bit_depth": 24,
        "upc": "1234567890123",
        "genre": {"name": "Rock"},
        "label": {"name": "Test Label"},
        "image": {"large": "https://static.qobuz.com/images/album/large.jpg"},
        "released_at": 1625097600,
        "copyright": "(c) Test",
        "product_type": "album",
    },
    "copyright": "(c) Test",
}

PLAYLIST_OBJ: dict[str, Any] = {
    "id": 111222,
    "name": "Test Playlist",
    "owner": {"id": 123, "name": "Test User"},
    "is_collaborative": False,
    "images300": ["https://static.qobuz.com/images/playlist/300.jpg"],
}

VARIOUS_ARTISTS_OBJ: dict[str, Any] = {
    "id": VARIOUS_ARTISTS_ID,
    "name": "Various Artists",
    "image": {"large": "https://static.qobuz.com/images/various/large.jpg"},
}


@pytest.fixture
def mock_provider() -> QobuzProvider:
    """Create a real QobuzProvider with mocked dependencies."""
    mass = Mock()
    mass.metadata.locale = "en_US"
    manifest = Mock()
    manifest.domain = "qobuz"
    config = Mock()
    config.instance_id = "qobuz_test"
    config.name = "Qobuz Test"
    config.enabled = True
    config.get_value.side_effect = lambda key, default=None: {
        "quality": "27",
        "log_level": "GLOBAL",
    }.get(key, default)
    provider = QobuzProvider(mass, manifest, config, SUPPORTED_FEATURES)
    provider._user_auth_info = {"user": {"id": 123}}
    return provider


class TestParseArtist:
    """Test _parse_artist method."""

    def test_parse_artist_basic(self, mock_provider: QobuzProvider) -> None:
        """Test parsing a basic artist object including biography and image."""
        result = mock_provider._parse_artist(deepcopy(ARTIST_OBJ))

        assert isinstance(result, Artist)
        assert result.item_id == "123456"
        assert result.name == "Test Artist"
        assert result.provider == "qobuz"
        assert len(result.provider_mappings) == 1
        mapping = next(iter(result.provider_mappings))
        assert mapping.item_id == "123456"
        assert mapping.provider_instance == "qobuz_test"
        # biography
        assert result.metadata.description == "A test biography"
        # image
        assert result.metadata.images is not None
        assert len(result.metadata.images) == 1
        assert result.metadata.images[0].type == ImageType.THUMB
        assert result.metadata.images[0].path == "https://static.qobuz.com/images/artist/large.jpg"

    def test_parse_various_artists(self, mock_provider: QobuzProvider) -> None:
        """Test parsing 'Various Artists' artist object - should set special MBID."""
        result = mock_provider._parse_artist(deepcopy(VARIOUS_ARTISTS_OBJ))

        assert result.item_id == VARIOUS_ARTISTS_ID
        assert result.name == VARIOUS_ARTISTS_NAME
        assert result.mbid == VARIOUS_ARTISTS_MBID

    def test_parse_artist_minimal(self, mock_provider: QobuzProvider) -> None:
        """Test parsing artist with minimal fields."""
        minimal_artist = {
            "id": 999,
            "name": "Minimal Artist",
        }
        result = mock_provider._parse_artist(minimal_artist)

        assert result.item_id == "999"
        assert result.name == "Minimal Artist"
        assert result.metadata.description is None
        assert result.metadata.images is None or len(result.metadata.images) == 0

    def test_parse_artist_missing_optional_image(self, mock_provider: QobuzProvider) -> None:
        """Test parsing artist without image."""
        artist_no_img = {
            "id": 555,
            "name": "No Image Artist",
            "biography": {"content": "Some bio"},
        }
        result = mock_provider._parse_artist(artist_no_img)

        assert result.name == "No Image Artist"
        assert result.metadata.images is None or len(result.metadata.images) == 0


class TestParseAlbum:
    """Test _parse_album method."""

    async def test_parse_album_basic(self, mock_provider: QobuzProvider) -> None:
        """Test parsing a basic album object including metadata fields."""
        result = await mock_provider._parse_album(deepcopy(ALBUM_OBJ))

        assert isinstance(result, Album)
        assert result.item_id == "789012"
        assert result.name == "Test Album"
        assert result.version == ""
        assert result.provider == "qobuz"
        assert result.year == 2021
        assert len(result.provider_mappings) == 1
        mapping = next(iter(result.provider_mappings))
        assert mapping.available is True
        # artist
        assert len(result.artists) == 1
        assert result.artists[0].name == "Test Artist"
        # UPC
        external_ids = list(result.external_ids)
        assert any(ext_id[1] == "1234567890123" for ext_id in external_ids)
        # genre
        assert result.metadata.genres is not None
        assert "Rock" in result.metadata.genres
        # label
        assert result.metadata.label == "Test Label"
        # image
        assert result.metadata.images is not None
        assert len(result.metadata.images) == 1
        assert result.metadata.images[0].path == "https://static.qobuz.com/images/album/large.jpg"
        # copyright
        assert result.metadata.copyright == "(c) Test"
        # album type
        assert result.album_type == AlbumType.ALBUM

    @pytest.mark.parametrize(
        ("product_type", "expected"),
        [
            ("album", AlbumType.ALBUM),
            ("single", AlbumType.SINGLE),
            ("compilation", AlbumType.COMPILATION),
        ],
    )
    async def test_parse_album_type(
        self, mock_provider: QobuzProvider, product_type: str, expected: AlbumType
    ) -> None:
        """Test parsing album with different product_type values."""
        album = deepcopy(ALBUM_OBJ)
        album["product_type"] = product_type

        result = await mock_provider._parse_album(album)

        assert result.album_type == expected

    async def test_parse_album_missing_upc(self, mock_provider: QobuzProvider) -> None:
        """Test parsing album without UPC doesn't raise KeyError."""
        album_no_upc = deepcopy(ALBUM_OBJ)
        del album_no_upc["upc"]

        result = await mock_provider._parse_album(album_no_upc)

        assert isinstance(result, Album)
        assert result.item_id == "789012"

    async def test_parse_album_minimal(self, mock_provider: QobuzProvider) -> None:
        """Test parsing album with minimal fields."""
        minimal_album = {
            "id": 1,
            "title": "Minimal Album",
            "version": "",
            "artist": {"id": 1, "name": "Artist"},
            "streamable": True,
            "displayable": True,
            "maximum_sampling_rate": 96,
            "maximum_bit_depth": 24,
        }
        result = await mock_provider._parse_album(minimal_album)

        assert result.item_id == "1"
        assert result.name == "Minimal Album"
        assert result.year is None
        assert result.metadata.genres is None or len(result.metadata.genres) == 0

    async def test_parse_album_explicit_flag(self, mock_provider: QobuzProvider) -> None:
        """Test parsing album with parental_warning sets explicit flag."""
        album = deepcopy(ALBUM_OBJ)
        album["parental_warning"] = True

        result = await mock_provider._parse_album(album)

        assert result.metadata.explicit is True

    async def test_parse_album_placeholder_image_filtered(
        self, mock_provider: QobuzProvider
    ) -> None:
        """Test that Qobuz placeholder image hash is filtered out."""
        album = deepcopy(ALBUM_OBJ)
        album["image"] = {
            "large": "https://static.qobuz.com/images/2a96cbd8b46e442fc41c2b86b821562f/large.jpg"
        }

        result = await mock_provider._parse_album(album)

        # Placeholder image should be filtered — no images on album itself
        # (may still get artist image via fallback, but the placeholder URL should not appear)
        for img in result.metadata.images or []:
            assert "2a96cbd8b46e442fc41c2b86b821562f" not in img.path


class TestParseTrack:
    """Test _parse_track method."""

    async def test_parse_track_basic(self, mock_provider: QobuzProvider) -> None:
        """Test parsing a basic track object with all standard fields."""
        result = await mock_provider._parse_track(deepcopy(TRACK_OBJ))

        assert isinstance(result, Track)
        assert result.item_id == "345678"
        assert result.name == "Test Track"
        assert result.duration == 240
        assert result.provider == "qobuz"
        assert result.track_number == 3
        assert result.disc_number == 1
        # performer artist
        assert len(result.artists) == 1
        assert result.artists[0].name == "Test Artist"
        # ISRC
        external_ids = list(result.external_ids)
        assert any(ext_id[1] == "USRC17607839" for ext_id in external_ids)
        # album
        assert result.album is not None
        assert result.album.name == "Test Album"
        # copyright
        assert result.metadata.copyright == "(c) Test"
        # performers metadata
        assert result.metadata.performers is not None
        assert len(result.metadata.performers) > 0
        # image
        assert result.metadata.images is not None
        assert len(result.metadata.images) == 1

    async def test_parse_track_performer_various_artists(
        self, mock_provider: QobuzProvider
    ) -> None:
        """Test parsing track with Various Artists performer is filtered."""
        track_with_various = deepcopy(TRACK_OBJ)
        track_with_various["performer"] = {
            "id": int(VARIOUS_ARTISTS_ID),
            "name": "Various Artists",
        }
        result = await mock_provider._parse_track(track_with_various)

        # Should not add Various Artists from performer
        assert len(result.artists) == 1
        assert result.artists[0].name == "Test Artist"

    async def test_parse_track_album_artist_various_artists(
        self, mock_provider: QobuzProvider
    ) -> None:
        """Test parsing track with Various Artists from album.artist is filtered."""
        track_with_various = deepcopy(TRACK_OBJ)
        track_with_various["performer"] = None
        album_copy = deepcopy(ALBUM_OBJ)
        album_copy["artist"] = {
            "id": int(VARIOUS_ARTISTS_ID),
            "name": "Various Artists",
        }
        track_with_various["album"] = album_copy

        result = await mock_provider._parse_track(track_with_various)

        # Should not add Various Artists from album.artist
        assert all(artist.name != "Various Artists" for artist in result.artists)
        # Guard against vacuous truth on empty list
        assert len(result.artists) > 0

    async def test_parse_track_minimal(self, mock_provider: QobuzProvider) -> None:
        """Test parsing track with minimal fields."""
        minimal_track = {
            "id": 1,
            "title": "Minimal Track",
            "version": "",
            "duration": 180,
            "streamable": True,
            "displayable": True,
            "maximum_sampling_rate": 96,
            "maximum_bit_depth": 24,
            "performers": "Unknown Artist, MainArtist",
        }
        result = await mock_provider._parse_track(minimal_track)

        assert result.item_id == "1"
        assert result.name == "Minimal Track"
        assert result.duration == 180

    async def test_parse_track_missing_performer_key(self, mock_provider: QobuzProvider) -> None:
        """Test parsing track without performer key."""
        track_no_performer = deepcopy(TRACK_OBJ)
        del track_no_performer["performer"]

        result = await mock_provider._parse_track(track_no_performer)

        assert isinstance(result, Track)
        assert result.item_id == "345678"

    async def test_parse_track_no_artists_uses_performers_string(
        self, mock_provider: QobuzProvider
    ) -> None:
        """Test parsing track falls back to performers string when no artist found."""
        track_no_artist = {
            "id": 2,
            "title": "Track with Performers String",
            "version": "",
            "duration": 200,
            "streamable": True,
            "displayable": True,
            "maximum_sampling_rate": 96,
            "maximum_bit_depth": 24,
            "performers": "Unknown Artist, MainArtist - Someone Else, Producer",
            "album": {
                "id": 1,
                "title": "Album",
                "version": "",
                "artist": {"id": 145383, "name": "Various Artists"},
                "streamable": True,
                "displayable": True,
                "maximum_sampling_rate": 96,
                "maximum_bit_depth": 24,
            },
        }
        result = await mock_provider._parse_track(track_no_artist)

        # Should add only artists with "artist" in their role, not producers
        assert len(result.artists) == 1
        assert result.artists[0].name == "Unknown Artist"

    async def test_parse_track_missing_performers_key(self, mock_provider: QobuzProvider) -> None:
        """Test parsing track with no performer, no album artist, and no performers key.

        The performers-string fallback gracefully handles missing key
        via .get("performers", "").
        """
        track = {
            "id": 99,
            "title": "No Performers Track",
            "version": "",
            "duration": 100,
            "streamable": True,
            "displayable": True,
            "maximum_sampling_rate": 96,
            "maximum_bit_depth": 24,
            # no "performer", no "performers", album artist is Various Artists
            "album": {
                "id": 1,
                "title": "VA Album",
                "version": "",
                "artist": {"id": 145383, "name": "Various Artists"},
                "streamable": True,
                "displayable": True,
                "maximum_sampling_rate": 96,
                "maximum_bit_depth": 24,
            },
        }
        result = await mock_provider._parse_track(track)

        assert isinstance(result, Track)
        assert len(result.artists) == 0

    async def test_parse_track_explicit_flag(self, mock_provider: QobuzProvider) -> None:
        """Test parsing track with parental_warning sets explicit flag."""
        track = deepcopy(TRACK_OBJ)
        track["parental_warning"] = True

        result = await mock_provider._parse_track(track)

        assert result.metadata.explicit is True


class TestParsePlaylist:
    """Test _parse_playlist method."""

    def test_parse_playlist_basic(self, mock_provider: QobuzProvider) -> None:
        """Test parsing a basic playlist object."""
        result = mock_provider._parse_playlist(deepcopy(PLAYLIST_OBJ))

        assert isinstance(result, Playlist)
        assert result.item_id == "111222"
        assert result.name == "Test Playlist"
        assert result.owner == "Test User"
        assert result.provider == "qobuz_test"

    @pytest.mark.parametrize(
        ("owner_id", "is_collaborative", "expected_editable"),
        [
            (123, False, True),  # user-owned
            (999, True, True),  # collaborative
            (999, False, False),  # not owned, not collaborative
        ],
    )
    def test_parse_playlist_editable(
        self,
        mock_provider: QobuzProvider,
        owner_id: int,
        is_collaborative: bool,
        expected_editable: bool,
    ) -> None:
        """Test playlist editability based on ownership and collaboration."""
        playlist = deepcopy(PLAYLIST_OBJ)
        owner = cast("dict[str, Any]", playlist["owner"])
        owner["id"] = owner_id
        playlist["is_collaborative"] = is_collaborative

        result = mock_provider._parse_playlist(playlist)

        assert result.is_editable is expected_editable

    def test_parse_playlist_with_image(self, mock_provider: QobuzProvider) -> None:
        """Test parsing playlist with image."""
        result = mock_provider._parse_playlist(deepcopy(PLAYLIST_OBJ))

        assert result.metadata.images is not None
        assert len(result.metadata.images) == 1
        assert result.metadata.images[0].path == "https://static.qobuz.com/images/playlist/300.jpg"

    def test_parse_playlist_minimal(self, mock_provider: QobuzProvider) -> None:
        """Test parsing playlist with minimal fields."""
        minimal_playlist = {
            "id": 1,
            "name": "Minimal Playlist",
            "owner": {"id": 123, "name": "Owner"},
            "is_collaborative": False,
        }
        result = mock_provider._parse_playlist(minimal_playlist)

        assert result.item_id == "1"
        assert result.name == "Minimal Playlist"
        assert result.metadata.images is None or len(result.metadata.images) == 0

    def test_parse_playlist_missing_auth_info(self, mock_provider: QobuzProvider) -> None:
        """Test parsing playlist without auth info raises error."""
        mock_provider._user_auth_info = None

        with pytest.raises(LoginFailed):
            mock_provider._parse_playlist(deepcopy(PLAYLIST_OBJ))
