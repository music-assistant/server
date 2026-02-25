"""Tests for Zvuk Music parsers."""

from __future__ import annotations

import contextlib
from unittest.mock import Mock

import pytest
from music_assistant_models.enums import AlbumType, ImageType

from music_assistant.providers.zvuk_music.constants import SYNTHESIS_PLAYLIST_IDS
from music_assistant.providers.zvuk_music.parsers import (
    parse_album,
    parse_artist,
    parse_playlist,
    parse_track,
)


def _create_mock_image(template: str = "https://zvuk.com/image/{width}x{height}") -> Mock:
    """Create a mock Zvuk Image object.

    :param template: URL template with {width} and {height} placeholders.
    :return: Mock Image object.
    """
    image = Mock()
    image.get_url = Mock(
        side_effect=lambda w, h: template.format(width=w, height=h) if template else None
    )
    return image


def _create_mock_artist(
    artist_id: int = 123,
    title: str | None = "Test Artist",
    image: Mock | None = None,
    description: str | None = None,
    second_image: Mock | None = None,
) -> Mock:
    """Create a mock Zvuk artist object.

    :param artist_id: Artist ID.
    :param title: Artist name.
    :param image: Optional mock image object.
    :param description: Optional artist biography (only on full Artist).
    :param second_image: Optional fanart/background image (only on full Artist).
    :return: Mock artist object.
    """
    artist = Mock()
    artist.id = artist_id
    artist.title = title
    artist.image = image

    # description and second_image are only on full Artist, not SimpleArtist
    if description is not None:
        artist.description = description
    else:
        with contextlib.suppress(AttributeError):
            del artist.description

    if second_image is not None:
        artist.second_image = second_image
    else:
        with contextlib.suppress(AttributeError):
            del artist.second_image

    return artist


def _create_mock_release(
    release_id: int = 456,
    title: str | None = "Test Album",
    artists: list[Mock] | None = None,
    release_type: str | None = None,
    date: str | None = None,
    explicit: bool = False,
    genres: list[Mock] | None = None,
    label: Mock | None = None,
    image: Mock | None = None,
) -> Mock:
    """Create a mock Zvuk release object.

    :param release_id: Release ID.
    :param title: Album title.
    :param artists: List of mock artist objects.
    :param release_type: Release type (album, single, ep, compilation).
    :param date: Release date in ISO format.
    :param explicit: Whether the release is explicit.
    :param genres: List of mock genre objects.
    :param label: Optional mock label object (only on full Release).
    :param image: Optional mock image object.
    :return: Mock release object.
    """
    release = Mock()
    release.id = release_id
    release.title = title
    release.artists = artists or []
    release.explicit = explicit
    release.image = image
    release.date = date

    # Type handling
    if release_type:
        type_mock = Mock()
        type_mock.value = release_type
        release.type = type_mock
    else:
        release.type = None

    # get_year method
    if date:
        release.get_year = Mock(return_value=int(date[:4]))
    else:
        release.get_year = Mock(return_value=None)

    # Genres (only on full Release)
    if genres is not None:
        release.genres = genres
    else:
        # SimpleRelease doesn't have genres
        with contextlib.suppress(AttributeError):
            del release.genres

    # Label (only on full Release)
    if label is not None:
        release.label = label
    else:
        with contextlib.suppress(AttributeError):
            del release.label

    return release


def _create_mock_track(
    track_id: int = 789,
    title: str | None = "Test Track",
    artists: list[Mock] | None = None,
    release: Mock | None = None,
    duration: int = 180,
    position: int | None = None,
    explicit: bool = False,
    genres: list[Mock] | None = None,
    credits_str: str | None = None,
) -> Mock:
    """Create a mock Zvuk track object.

    :param track_id: Track ID.
    :param title: Track title.
    :param artists: List of mock artist objects.
    :param release: Mock release object.
    :param duration: Track duration in seconds.
    :param position: Track position in album.
    :param explicit: Whether the track is explicit.
    :param genres: List of mock genre objects (only on full Track).
    :param credits_str: Credits string (only on full Track).
    :return: Mock track object.
    """
    track = Mock()
    track.id = track_id
    track.title = title
    track.artists = artists or []
    track.release = release
    track.duration = duration
    track.explicit = explicit

    # Position is only on full Track, not SimpleTrack
    if position is not None:
        track.position = position
    else:
        with contextlib.suppress(AttributeError):
            del track.position

    # Genres are only on full Track, not SimpleTrack
    if genres is not None:
        track.genres = genres
    else:
        with contextlib.suppress(AttributeError):
            del track.genres

    # Credits are only on full Track, not SimpleTrack
    if credits_str is not None:
        track.credits = credits_str
    else:
        with contextlib.suppress(AttributeError):
            del track.credits

    return track


def _create_mock_playlist(
    playlist_id: int = 999,
    title: str | None = "Test Playlist",
    description: str | None = None,
    user_id: int | None = None,
    image: Mock | None = None,
) -> Mock:
    """Create a mock Zvuk playlist object.

    :param playlist_id: Playlist ID.
    :param title: Playlist title.
    :param description: Playlist description.
    :param user_id: User ID (owner).
    :param image: Optional mock image object.
    :return: Mock playlist object.
    """
    playlist = Mock()
    playlist.id = playlist_id
    playlist.title = title
    playlist.description = description
    playlist.image = image

    # user_id is only on full Playlist, not SimplePlaylist
    if user_id is not None:
        playlist.user_id = user_id
    else:
        with contextlib.suppress(AttributeError):
            del playlist.user_id

    return playlist


@pytest.fixture
def mock_provider() -> Mock:
    """Create a mock ZvukMusicProvider."""
    provider = Mock()
    provider.instance_id = "zvuk_music_test"
    provider.domain = "zvuk_music"
    provider.client = Mock()
    provider.client.user_id = 12345

    def mock_get_item_mapping(
        media_type: str,  # noqa: ARG001
        key: str,
        name: str,
    ) -> Mock:
        mapping = Mock()
        mapping.item_id = key
        mapping.name = name
        return mapping

    provider.get_item_mapping = Mock(side_effect=mock_get_item_mapping)
    return provider


class TestParseArtist:
    """Tests for parse_artist function."""

    def test_parse_artist_basic(self, mock_provider: Mock) -> None:
        """Test parsing a basic artist without image."""
        artist_obj = _create_mock_artist(artist_id=123, title="Test Artist")

        result = parse_artist(mock_provider, artist_obj)

        assert result.item_id == "123"
        assert result.name == "Test Artist"
        assert result.provider == "zvuk_music_test"
        assert len(result.provider_mappings) == 1

        mapping = next(iter(result.provider_mappings))
        assert mapping.item_id == "123"
        assert mapping.provider_domain == "zvuk_music"
        assert mapping.provider_instance == "zvuk_music_test"
        assert mapping.url == "https://zvuk.com/artist/123"

    def test_parse_artist_with_image(self, mock_provider: Mock) -> None:
        """Test parsing an artist with image."""
        image = _create_mock_image("https://zvuk.com/img/{width}x{height}.jpg")
        artist_obj = _create_mock_artist(artist_id=456, title="Artist With Image", image=image)

        result = parse_artist(mock_provider, artist_obj)

        assert result.item_id == "456"
        assert result.name == "Artist With Image"
        assert result.metadata.images is not None
        assert len(result.metadata.images) == 1
        assert result.metadata.images[0].type == ImageType.THUMB
        assert result.metadata.images[0].path == "https://zvuk.com/img/600x600.jpg"
        assert result.metadata.images[0].remotely_accessible is True

    def test_parse_artist_unknown_name(self, mock_provider: Mock) -> None:
        """Test parsing an artist with missing title defaults to Unknown Artist."""
        artist_obj = _create_mock_artist(artist_id=789, title=None)

        result = parse_artist(mock_provider, artist_obj)

        assert result.name == "Unknown Artist"

    def test_parse_artist_with_description(self, mock_provider: Mock) -> None:
        """Test parsing a full artist with biography/description."""
        artist_obj = _create_mock_artist(
            artist_id=123, title="Artist Bio", description="A great musician."
        )

        result = parse_artist(mock_provider, artist_obj)

        assert result.metadata.description == "A great musician."

    def test_parse_artist_without_description(self, mock_provider: Mock) -> None:
        """Test parsing a SimpleArtist without description (attribute absent)."""
        artist_obj = _create_mock_artist(artist_id=123, title="Simple Artist")

        result = parse_artist(mock_provider, artist_obj)

        assert result.metadata.description is None

    def test_parse_artist_with_fanart(self, mock_provider: Mock) -> None:
        """Test parsing an artist with second_image mapped as FANART."""
        second_image = _create_mock_image("https://zvuk.com/fanart/{width}x{height}.jpg")
        artist_obj = _create_mock_artist(
            artist_id=123, title="Artist With Fanart", second_image=second_image
        )

        result = parse_artist(mock_provider, artist_obj)

        assert result.metadata.images is not None
        fanart_images = [i for i in result.metadata.images if i.type == ImageType.FANART]
        assert len(fanart_images) == 1
        assert fanart_images[0].path == "https://zvuk.com/fanart/600x600.jpg"

    def test_parse_artist_with_thumb_and_fanart(self, mock_provider: Mock) -> None:
        """Test parsing an artist with both thumb and fanart images."""
        image = _create_mock_image("https://zvuk.com/img/{width}x{height}.jpg")
        second_image = _create_mock_image("https://zvuk.com/fanart/{width}x{height}.jpg")
        artist_obj = _create_mock_artist(
            artist_id=123,
            title="Artist Both Images",
            image=image,
            second_image=second_image,
        )

        result = parse_artist(mock_provider, artist_obj)

        assert result.metadata.images is not None
        assert len(result.metadata.images) == 2
        types = {i.type for i in result.metadata.images}
        assert ImageType.THUMB in types
        assert ImageType.FANART in types


class TestParseAlbum:
    """Tests for parse_album function."""

    def test_parse_album_basic(self, mock_provider: Mock) -> None:
        """Test parsing a basic album."""
        release_obj = _create_mock_release(release_id=456, title="Test Album")

        result = parse_album(mock_provider, release_obj)

        assert result.item_id == "456"
        assert result.name == "Test Album"
        assert result.provider == "zvuk_music_test"
        assert result.album_type == AlbumType.ALBUM

        mapping = next(iter(result.provider_mappings))
        assert mapping.url == "https://zvuk.com/release/456"

    def test_parse_album_type_single(self, mock_provider: Mock) -> None:
        """Test parsing an album with type single."""
        release_obj = _create_mock_release(
            release_id=1, title="Single Track", release_type="single"
        )

        result = parse_album(mock_provider, release_obj)

        assert result.album_type == AlbumType.SINGLE

    def test_parse_album_type_ep(self, mock_provider: Mock) -> None:
        """Test parsing an album with type EP."""
        release_obj = _create_mock_release(release_id=2, title="EP Release", release_type="ep")

        result = parse_album(mock_provider, release_obj)

        assert result.album_type == AlbumType.EP

    def test_parse_album_type_compilation(self, mock_provider: Mock) -> None:
        """Test parsing an album with type compilation."""
        release_obj = _create_mock_release(
            release_id=3, title="Greatest Hits", release_type="compilation"
        )

        result = parse_album(mock_provider, release_obj)

        assert result.album_type == AlbumType.COMPILATION

    def test_parse_album_with_date(self, mock_provider: Mock) -> None:
        """Test parsing an album with release date."""
        release_obj = _create_mock_release(release_id=456, title="Album 2023", date="2023-06-15")

        result = parse_album(mock_provider, release_obj)

        assert result.year == 2023
        assert result.metadata.release_date is not None
        assert result.metadata.release_date.year == 2023
        assert result.metadata.release_date.month == 6
        assert result.metadata.release_date.day == 15

    def test_parse_album_explicit(self, mock_provider: Mock) -> None:
        """Test parsing an explicit album."""
        release_obj = _create_mock_release(release_id=456, title="Explicit Album", explicit=True)

        result = parse_album(mock_provider, release_obj)

        assert result.metadata.explicit is True

    def test_parse_album_with_artists(self, mock_provider: Mock) -> None:
        """Test parsing an album with artists."""
        artists = [
            _create_mock_artist(artist_id=1, title="Artist One"),
            _create_mock_artist(artist_id=2, title="Artist Two"),
        ]
        release_obj = _create_mock_release(
            release_id=456, title="Collaboration Album", artists=artists
        )

        result = parse_album(mock_provider, release_obj)

        assert len(result.artists) == 2
        assert result.artists[0].name == "Artist One"
        assert result.artists[1].name == "Artist Two"

    def test_parse_album_with_genres(self, mock_provider: Mock) -> None:
        """Test parsing an album with genres (full Release only)."""
        genre1 = Mock()
        genre1.name = "Rock"
        genre2 = Mock()
        genre2.name = "Alternative"
        release_obj = _create_mock_release(
            release_id=456, title="Rock Album", genres=[genre1, genre2]
        )

        result = parse_album(mock_provider, release_obj)

        assert result.metadata.genres == {"Rock", "Alternative"}

    def test_parse_album_with_image(self, mock_provider: Mock) -> None:
        """Test parsing an album with cover image."""
        image = _create_mock_image("https://zvuk.com/cover/{width}x{height}.jpg")
        release_obj = _create_mock_release(release_id=456, title="Album With Cover", image=image)

        result = parse_album(mock_provider, release_obj)

        assert result.metadata.images is not None
        assert len(result.metadata.images) == 1
        assert result.metadata.images[0].path == "https://zvuk.com/cover/600x600.jpg"

    def test_parse_album_with_version_in_title(self, mock_provider: Mock) -> None:
        """Test parsing an album with version in title."""
        release_obj = _create_mock_release(release_id=456, title="Album Name (Deluxe Edition)")

        result = parse_album(mock_provider, release_obj)

        assert result.name == "Album Name"
        assert result.version == "Deluxe Edition"

    def test_parse_album_with_label(self, mock_provider: Mock) -> None:
        """Test parsing a full Release with record label."""
        label = Mock()
        label.title = "XL Recordings"
        release_obj = _create_mock_release(release_id=456, title="Labeled Album", label=label)

        result = parse_album(mock_provider, release_obj)

        assert result.metadata.label == "XL Recordings"

    def test_parse_album_without_label(self, mock_provider: Mock) -> None:
        """Test parsing a SimpleRelease without label attribute."""
        release_obj = _create_mock_release(release_id=456, title="No Label Album")

        result = parse_album(mock_provider, release_obj)

        assert result.metadata.label is None


class TestParseTrack:
    """Tests for parse_track function."""

    def test_parse_track_basic(self, mock_provider: Mock) -> None:
        """Test parsing a basic track."""
        track_obj = _create_mock_track(track_id=789, title="Test Track", duration=180)

        result = parse_track(mock_provider, track_obj)

        assert result.item_id == "789"
        assert result.name == "Test Track"
        assert result.duration == 180
        assert result.provider == "zvuk_music_test"

        mapping = next(iter(result.provider_mappings))
        assert mapping.url == "https://zvuk.com/track/789"

    def test_parse_track_with_artists(self, mock_provider: Mock) -> None:
        """Test parsing a track with artists."""
        artists = [
            _create_mock_artist(artist_id=1, title="Singer"),
            _create_mock_artist(artist_id=2, title="Featuring Artist"),
        ]
        track_obj = _create_mock_track(track_id=789, title="Track", artists=artists)

        result = parse_track(mock_provider, track_obj)

        assert len(result.artists) == 2
        assert result.artists[0].name == "Singer"
        assert result.artists[1].name == "Featuring Artist"

    def test_parse_track_with_release(self, mock_provider: Mock) -> None:
        """Test parsing a track with album (release) information."""
        release = Mock()
        release.id = 456
        release.title = "Test Album"
        release.image = _create_mock_image("https://zvuk.com/cover/{width}x{height}.jpg")

        track_obj = _create_mock_track(track_id=789, title="Track", release=release)

        result = parse_track(mock_provider, track_obj)

        assert result.album is not None
        assert result.album.item_id == "456"
        assert result.album.name == "Test Album"
        mock_provider.get_item_mapping.assert_called_with(
            media_type="album",
            key="456",
            name="Test Album",
        )

    def test_parse_track_with_release_image(self, mock_provider: Mock) -> None:
        """Test parsing a track gets image from release."""
        release = Mock()
        release.id = 456
        release.title = "Test Album"
        release.image = _create_mock_image("https://zvuk.com/cover/{width}x{height}.jpg")

        track_obj = _create_mock_track(track_id=789, title="Track", release=release)

        result = parse_track(mock_provider, track_obj)

        assert result.metadata.images is not None
        assert len(result.metadata.images) == 1
        assert result.metadata.images[0].path == "https://zvuk.com/cover/600x600.jpg"

    def test_parse_track_explicit(self, mock_provider: Mock) -> None:
        """Test parsing an explicit track."""
        track_obj = _create_mock_track(track_id=789, title="Explicit Track", explicit=True)

        result = parse_track(mock_provider, track_obj)

        assert result.metadata.explicit is True

    def test_parse_track_with_position(self, mock_provider: Mock) -> None:
        """Test parsing a track with position (full Track only)."""
        track_obj = _create_mock_track(track_id=789, title="Track 5", position=5)

        result = parse_track(mock_provider, track_obj)

        assert result.track_number == 5

    def test_parse_track_with_version_in_title(self, mock_provider: Mock) -> None:
        """Test parsing a track with version in title."""
        track_obj = _create_mock_track(track_id=789, title="Song Name (Acoustic Version)")

        result = parse_track(mock_provider, track_obj)

        assert result.name == "Song Name"
        assert result.version == "Acoustic Version"

    def test_parse_track_unknown_name(self, mock_provider: Mock) -> None:
        """Test parsing a track with missing title defaults to Unknown Track."""
        track_obj = _create_mock_track(track_id=789, title=None)

        result = parse_track(mock_provider, track_obj)

        assert result.name == "Unknown Track"

    def test_parse_track_with_genres(self, mock_provider: Mock) -> None:
        """Test parsing a full Track with genres."""
        g1 = Mock()
        g1.name = "Rock"
        g2 = Mock()
        g2.name = "Alternative"
        track_obj = _create_mock_track(track_id=789, title="Genres Track", genres=[g1, g2])

        result = parse_track(mock_provider, track_obj)

        assert result.metadata.genres == {"Rock", "Alternative"}

    def test_parse_track_without_genres(self, mock_provider: Mock) -> None:
        """Test parsing a SimpleTrack without genres attribute."""
        track_obj = _create_mock_track(track_id=789, title="No Genres Track")

        result = parse_track(mock_provider, track_obj)

        assert result.metadata.genres is None

    def test_parse_track_with_credits(self, mock_provider: Mock) -> None:
        """Test parsing a full Track with credits."""
        track_obj = _create_mock_track(
            track_id=789, title="Credited Track", credits_str="John Lennon; Paul McCartney"
        )

        result = parse_track(mock_provider, track_obj)

        assert result.metadata.performers == {"John Lennon; Paul McCartney"}

    def test_parse_track_without_credits(self, mock_provider: Mock) -> None:
        """Test parsing a SimpleTrack without credits attribute."""
        track_obj = _create_mock_track(track_id=789, title="No Credits Track")

        result = parse_track(mock_provider, track_obj)

        assert result.metadata.performers is None


class TestParsePlaylist:
    """Tests for parse_playlist function."""

    def test_parse_playlist_basic(self, mock_provider: Mock) -> None:
        """Test parsing a basic playlist."""
        playlist_obj = _create_mock_playlist(playlist_id=999, title="Test Playlist")

        result = parse_playlist(mock_provider, playlist_obj)

        assert result.item_id == "999"
        assert result.name == "Test Playlist"
        assert result.provider == "zvuk_music_test"
        assert result.owner == "Zvuk Music"
        assert result.is_editable is False

        mapping = next(iter(result.provider_mappings))
        assert mapping.url == "https://zvuk.com/playlist/999"
        assert mapping.is_unique is False

    def test_parse_playlist_editable(self, mock_provider: Mock) -> None:
        """Test parsing a user-owned playlist (is_editable=True)."""
        playlist_obj = _create_mock_playlist(
            playlist_id=999,
            title="My Playlist",
            user_id=12345,  # Same as mock_provider.client.user_id
        )

        result = parse_playlist(mock_provider, playlist_obj)

        assert result.is_editable is True
        assert result.owner == "Me"

        mapping = next(iter(result.provider_mappings))
        assert mapping.is_unique is True

    def test_parse_playlist_not_editable(self, mock_provider: Mock) -> None:
        """Test parsing another user's playlist (is_editable=False)."""
        playlist_obj = _create_mock_playlist(
            playlist_id=999,
            title="Their Playlist",
            user_id=99999,  # Different from mock_provider.client.user_id
        )

        result = parse_playlist(mock_provider, playlist_obj)

        assert result.is_editable is False
        assert result.owner == "Zvuk Music"

    def test_parse_playlist_with_description(self, mock_provider: Mock) -> None:
        """Test parsing a playlist with description."""
        playlist_obj = _create_mock_playlist(
            playlist_id=999,
            title="Playlist",
            description="A great collection of songs",
        )

        result = parse_playlist(mock_provider, playlist_obj)

        assert result.metadata.description == "A great collection of songs"

    def test_parse_playlist_with_image(self, mock_provider: Mock) -> None:
        """Test parsing a playlist with cover image."""
        image = _create_mock_image("https://zvuk.com/playlist/{width}x{height}.jpg")
        playlist_obj = _create_mock_playlist(
            playlist_id=999,
            title="Playlist With Cover",
            image=image,
        )

        result = parse_playlist(mock_provider, playlist_obj)

        assert result.metadata.images is not None
        assert len(result.metadata.images) == 1
        assert result.metadata.images[0].path == "https://zvuk.com/playlist/600x600.jpg"

    def test_parse_playlist_unknown_name(self, mock_provider: Mock) -> None:
        """Test parsing a playlist with missing title defaults to Unknown Playlist."""
        playlist_obj = _create_mock_playlist(playlist_id=999, title=None)

        result = parse_playlist(mock_provider, playlist_obj)

        assert result.name == "Unknown Playlist"

    def test_parse_playlist_image_src_none(self, mock_provider: Mock) -> None:
        """Test parsing a playlist whose Image object has src=None does not raise.

        Regression test for AttributeError when image.get_url() is called with src=None.
        Zvuk API returns Image objects with src=None for user-created playlists without covers.
        """
        image = Mock()
        image.src = None
        image.get_url = Mock(
            side_effect=AttributeError("'NoneType' object has no attribute 'startswith'")
        )
        playlist_obj = _create_mock_playlist(
            playlist_id=999, title="Playlist No Cover", image=image
        )

        result = parse_playlist(mock_provider, playlist_obj)

        assert result.name == "Playlist No Cover"
        assert result.metadata.images is None or len(result.metadata.images) == 0

    def test_parse_playlist_simple_playlist_no_user_id(self, mock_provider: Mock) -> None:
        """Test that SimplePlaylist (no user_id) is parsed as not editable.

        Synthesis playlists (IDs 3,4,6,11,12,13,14,15) are returned as SimplePlaylist
        objects by get_short_playlist() — they have no user_id attribute.
        """
        playlist_obj = _create_mock_playlist(
            playlist_id=3,  # synthesis playlist ID
            title="Свежие релизы",
            description="Только в вашем вкусе",
            user_id=None,  # SimplePlaylist: no user_id
        )

        result = parse_playlist(mock_provider, playlist_obj)

        assert result.name == "Свежие релизы"
        assert result.metadata.description == "Только в вашем вкусе"
        assert result.is_editable is False

    def test_parse_playlist_simple_playlist_with_image(self, mock_provider: Mock) -> None:
        """Test that SimplePlaylist with image URL is parsed correctly."""
        image = Mock()
        image.src = "https://obs-image-service.example.com/abc123"
        image.get_url = Mock(return_value="https://zvuk.com/synthesis/600x600.jpg")

        playlist_obj = _create_mock_playlist(
            playlist_id=6,
            title="Когда хочется музыки",
            description="У тишины нет шансов",  # noqa: RUF001
            user_id=None,
            image=image,
        )

        result = parse_playlist(mock_provider, playlist_obj)

        assert result.name == "Когда хочется музыки"
        assert result.is_editable is False
        # Image should be mapped
        assert result.metadata.images is not None
        assert len(result.metadata.images) == 1

    def test_parse_playlist_synthesis_ids_treated_as_not_editable(
        self, mock_provider: Mock
    ) -> None:
        """Verify all synthesis playlist IDs (3,4,6,11,12,13,14,15) parse as not editable."""
        for pid in SYNTHESIS_PLAYLIST_IDS:
            playlist_obj = _create_mock_playlist(
                playlist_id=pid,
                title=f"Playlist {pid}",
                user_id=None,  # SimplePlaylist has no user_id
            )
            result = parse_playlist(mock_provider, playlist_obj)
            assert result.is_editable is False, f"Playlist {pid} should not be editable"
