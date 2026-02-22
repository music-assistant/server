"""Tests for parsing audio file tags (ID3, MP4/AAC, Vorbis, APEv2, etc.)."""

import pathlib
from unittest.mock import MagicMock

from music_assistant.constants import UNKNOWN_ARTIST
from music_assistant.helpers import tags
from music_assistant.helpers.tags import (
    _parse_apev2_tags,
    _parse_vorbis_tags,
    parse_tags_mutagen,
    split_artists,
)

RESOURCES_DIR = pathlib.Path(__file__).parent.parent.resolve().joinpath("fixtures")

FILE_MP3 = str(RESOURCES_DIR.joinpath("MyArtist - MyTitle.mp3"))
FILE_M4A = str(RESOURCES_DIR.joinpath("MyArtist - MyTitle.m4a"))
FILE_FLAC = str(RESOURCES_DIR.joinpath("MultipleArtists.flac"))
FILE_WV = str(RESOURCES_DIR.joinpath("MyArtist - MyTitle.wv"))


async def test_parse_metadata_from_id3tags() -> None:
    """Test parsing of parsing metadata from ID3 tags."""
    filename = str(RESOURCES_DIR.joinpath("MyArtist - MyTitle.mp3"))
    _tags = await tags.async_parse_tags(filename)
    assert _tags.album == "MyAlbum"
    assert _tags.title == "MyTitle"
    assert _tags.duration == 1.032
    assert _tags.album_artists == ("MyArtist",)
    assert _tags.artists == ("MyArtist", "MyArtist2")
    assert _tags.genres == ("Genre1", "Genre2")
    assert _tags.musicbrainz_albumartistids == ("abcdefg",)
    assert _tags.musicbrainz_artistids == ("abcdefg",)
    assert _tags.musicbrainz_releasegroupid == "abcdefg"
    assert _tags.musicbrainz_recordingid == "abcdefg"
    # test parsing disc/track number
    _tags.tags["disc"] = ""
    assert _tags.disc is None
    _tags.tags["disc"] = "1"
    assert _tags.disc == 1
    _tags.tags["disc"] = "1/1"
    assert _tags.disc == 1
    # test parsing album year
    _tags.tags["date"] = "blah"
    assert _tags.year is None
    _tags.tags.pop("date", None)
    assert _tags.year is None
    _tags.tags["date"] = "2022"
    assert _tags.year == 2022
    _tags.tags["date"] = "2022-05-05"
    assert _tags.year == 2022
    _tags.tags["date"] = ""
    assert _tags.year is None


async def test_parse_metadata_from_mp4tags() -> None:
    """Test parsing of metadata from MP4/AAC tags."""
    filename = FILE_M4A
    _tags = await tags.async_parse_tags(filename)
    assert _tags.album == "MyAlbum"
    assert _tags.title == "MyTitle"
    assert _tags.album_artists == ("MyArtist",)
    assert _tags.artists == ("MyArtist", "MyArtist2")
    assert _tags.genres == ("Genre1", "Genre2")
    assert _tags.musicbrainz_albumartistids == ("abcdefg",)
    assert _tags.musicbrainz_artistids == ("abcdefg",)
    assert _tags.musicbrainz_releasegroupid == "abcdefg"
    assert _tags.musicbrainz_recordingid == "abcdefg"
    # test track/disc from MP4 tuples
    assert _tags.track == 5
    assert _tags.disc == 1
    # test total track/disc
    assert _tags.tags.get("tracktotal") == "12"
    assert _tags.tags.get("disctotal") == "2"
    # test year
    assert _tags.year == 2022
    # test sort tags (artistsort/albumartistsort returned as lists to match ID3 behavior)
    assert _tags.tags.get("titlesort") == "MyTitle Sort"
    assert _tags.tags.get("artistsort") == ["MyArtist Sort"]  # type: ignore[comparison-overlap]
    assert _tags.tags.get("albumsort") == "MyAlbum Sort"
    assert _tags.tags.get("albumartistsort") == ["MyAlbumArtist Sort"]  # type: ignore[comparison-overlap]


def test_parse_metadata_from_apev2tags() -> None:
    """Test parsing of metadata from APEv2 tags (WavPack).

    Uses parse_tags_mutagen directly since the minimal WavPack fixture
    does not contain valid audio data for ffprobe to parse.
    """
    result = parse_tags_mutagen(FILE_WV)
    assert result.get("album") == "MyAlbum"
    assert result.get("title") == "MyTitle"
    assert result.get("albumartist") == "MyArtist"
    assert result.get("artist") == "MyArtist"
    assert result.get("artists") == ["MyArtist", "MyArtist2"]
    assert result.get("genre") == ["Genre1", "Genre2"]
    assert result.get("musicbrainzalbumartistid") == ["abcdefg"]
    assert result.get("musicbrainzartistid") == ["abcdefg"]
    assert result.get("musicbrainzreleasegroupid") == "abcdefg"
    assert result.get("musicbrainzrecordingid") == "abcdefg"
    # test track/disc (APEv2 uses "5/12" format like ID3)
    assert result.get("track") == "5/12"
    assert result.get("disc") == "1/2"
    # test year
    assert result.get("date") == "2022"
    # test sort tags (artistsort/albumartistsort returned as lists to match ID3 behavior)
    assert result.get("titlesort") == "MyTitle Sort"
    assert result.get("artistsort") == ["MyArtist Sort"]
    assert result.get("albumsort") == "MyAlbum Sort"
    assert result.get("albumartistsort") == ["MyAlbumArtist Sort"]


async def test_parse_metadata_from_flac_with_multiple_artist_fields() -> None:
    """Test parsing of FLAC file with multiple ARTIST fields (per Vorbis spec)."""
    _tags = await tags.async_parse_tags(FILE_FLAC)
    assert _tags.album == "Test Album"
    assert _tags.title == "Test Track"
    # Multiple ARTIST fields should be treated as authoritative list
    assert _tags.artists == ("Artist One", "Artist Two", "Artist Three")
    # Multiple ALBUMARTIST fields should be treated as authoritative list
    assert _tags.album_artists == ("Album Artist 1", "Album Artist 2")
    assert _tags.genres == ("Rock", "Pop")
    assert _tags.year == 2024
    # MusicBrainz IDs
    assert _tags.musicbrainz_artistids == ("mb-artist-id-1", "mb-artist-id-2", "mb-artist-id-3")
    assert _tags.musicbrainz_albumartistids == ("mb-albumartist-id-1", "mb-albumartist-id-2")
    assert _tags.musicbrainz_recordingid == "mb-track-id"
    # Track/disc from Vorbis comments
    assert _tags.track == 5
    assert _tags.disc == 1


async def test_parse_metadata_from_filename() -> None:
    """Test parsing of parsing metadata from filename."""
    filename = str(RESOURCES_DIR.joinpath("MyArtist - MyTitle without Tags.mp3"))
    _tags = await tags.async_parse_tags(filename)
    assert _tags.album is None
    assert _tags.title == "MyTitle without Tags"
    assert _tags.duration == 1.032
    assert _tags.album_artists == ()
    assert _tags.artists == ("MyArtist",)
    assert _tags.genres == ()
    assert _tags.musicbrainz_albumartistids == ()
    assert _tags.musicbrainz_artistids == ()
    assert _tags.musicbrainz_releasegroupid is None
    assert _tags.musicbrainz_recordingid is None


async def test_parse_metadata_from_invalid_filename() -> None:
    """Test parsing of parsing metadata from (invalid) filename."""
    filename = str(RESOURCES_DIR.joinpath("test.mp3"))
    _tags = await tags.async_parse_tags(filename)
    assert _tags.album is None
    assert _tags.title == "test"
    assert _tags.duration == 1.032
    assert _tags.album_artists == ()
    assert _tags.artists == (UNKNOWN_ARTIST,)
    assert _tags.genres == ()
    assert _tags.musicbrainz_albumartistids == ()
    assert _tags.musicbrainz_artistids == ()
    assert _tags.musicbrainz_releasegroupid is None
    assert _tags.musicbrainz_recordingid is None


def test_split_artists_with_expected_count() -> None:
    """Test splitting artists guided by expected count (from MB IDs)."""
    # With expected_count=3, should split on extra splitters to reach target
    result = split_artists("Shabson, Krgovich & Harris", expected_count=3)
    assert result == ("Shabson", "Krgovich", "Harris")

    # With expected_count=3, ampersands should split
    result = split_artists("Shabson & Krgovich & Harris", expected_count=3)
    assert result == ("Shabson", "Krgovich", "Harris")

    # With expected_count=3, commas should split
    result = split_artists("Shabson, Krgovich, Harris", expected_count=3)
    assert result == ("Shabson", "Krgovich", "Harris")

    # With expected_count=1, should NOT split at all
    result = split_artists("Shabson & Krgovich", expected_count=1)
    assert result == ("Shabson & Krgovich",)

    # With expected_count=None (no MB IDs), should NOT split on extra splitters
    result = split_artists("Shabson & Krgovich", expected_count=None)
    assert result == ("Shabson & Krgovich",)

    # With expected_count=0 (no MB IDs), should NOT split on extra splitters
    result = split_artists("Shabson & Krgovich", expected_count=0)
    assert result == ("Shabson & Krgovich",)


def test_split_artists_featuring() -> None:
    """Test that featuring splitters always work regardless of expected_count."""
    # "feat." should always split, even with no expected_count
    result = split_artists("John Lennon feat. Yoko Ono", expected_count=None)
    assert result == ("John Lennon", "Yoko Ono")

    # "feat." should split even with expected_count=1 (featuring overrides)
    # Actually, expected_count=1 means single artist, so we return as-is
    result = split_artists("John Lennon feat. Yoko Ono", expected_count=1)
    assert result == ("John Lennon feat. Yoko Ono",)

    # "featuring" should work
    result = split_artists("Artist A featuring Artist B", expected_count=None)
    assert result == ("Artist A", "Artist B")

    # "ft." should work
    result = split_artists("Artist A ft. Artist B", expected_count=None)
    assert result == ("Artist A", "Artist B")


def test_split_artists_no_oversplit() -> None:
    """Test that split_artists stops at expected_count and doesn't over-split."""
    # Hall & Oates is a duo, with 2 MB IDs we should split on feat. first
    # and get exactly 2 artists
    result = split_artists("Hall & Oates feat. David Ruffin", expected_count=2)
    assert result == ("Hall & Oates", "David Ruffin")

    # With 3 MB IDs, we should split further
    result = split_artists("Hall & Oates feat. David Ruffin", expected_count=3)
    assert result == ("Hall", "Oates", "David Ruffin")

    # Simon & Garfunkel with 1 MB ID (the duo) should stay as one
    result = split_artists("Simon & Garfunkel", expected_count=1)
    assert result == ("Simon & Garfunkel",)

    # Simon & Garfunkel with 2 MB IDs (Paul + Art) should split
    result = split_artists("Simon & Garfunkel", expected_count=2)
    assert result == ("Simon", "Garfunkel")


def test_split_artists_with_not_split() -> None:
    """Test that 'with' is only split when we have MB ID evidence."""
    # "with" should NOT split without expected_count (could be artist name)
    result = split_artists("Jerk With a Bomb", expected_count=None)
    assert result == ("Jerk With a Bomb",)

    # "with" should NOT split with expected_count=1
    result = split_artists("Jerk With a Bomb", expected_count=1)
    assert result == ("Jerk With a Bomb",)

    # "with" SHOULD split when expected_count=2 indicates multiple artists
    result = split_artists("Artist A with Artist B", expected_count=2)
    assert result == ("Artist A", "Artist B")


def _create_mock_vorbis_tags(tag_dict: dict[str, list[str]]) -> MagicMock:
    """Create a mock VCommentDict with the given tags.

    :param tag_dict: Dictionary mapping tag names to lists of values.
    """
    mock = MagicMock()
    mock.get = lambda key: tag_dict.get(key.upper())
    return mock


def test_parse_vorbis_tags_multiple_artist_fields() -> None:
    """Test that multiple ARTIST fields are treated as authoritative artist list."""
    # Per Vorbis spec: multiple ARTIST fields should list all artists
    mock_tags = _create_mock_vorbis_tags(
        {
            "TITLE": ["My Song"],
            "ALBUM": ["My Album"],
            "ARTIST": ["Artist 1", "Artist 2", "Artist 3"],
        }
    )

    result = _parse_vorbis_tags(mock_tags)

    # Multiple ARTIST fields should be stored as "artists" (plural)
    assert result.get("artists") == ["Artist 1", "Artist 2", "Artist 3"]
    # Single "artist" key should NOT be set when multiple artists are present
    assert "artist" not in result
    assert result.get("title") == "My Song"
    assert result.get("album") == "My Album"


def test_parse_vorbis_tags_single_artist_field() -> None:
    """Test that a single ARTIST field is stored as singular artist."""
    mock_tags = _create_mock_vorbis_tags(
        {
            "TITLE": ["My Song"],
            "ARTIST": ["Single Artist"],
        }
    )

    result = _parse_vorbis_tags(mock_tags)

    # Single ARTIST should use singular key for normal parsing logic
    assert result.get("artist") == "Single Artist"
    assert "artists" not in result


def test_parse_vorbis_tags_multiple_albumartist_fields() -> None:
    """Test that multiple ALBUMARTIST fields are treated as authoritative list."""
    mock_tags = _create_mock_vorbis_tags(
        {
            "ALBUMARTIST": ["Album Artist 1", "Album Artist 2"],
        }
    )

    result = _parse_vorbis_tags(mock_tags)

    # Multiple ALBUMARTIST fields should be stored as "albumartists" (plural)
    assert result.get("albumartists") == ["Album Artist 1", "Album Artist 2"]
    assert "albumartist" not in result


def test_parse_vorbis_tags_single_albumartist_field() -> None:
    """Test that a single ALBUMARTIST field is stored as singular."""
    mock_tags = _create_mock_vorbis_tags(
        {
            "ALBUMARTIST": ["Single Album Artist"],
        }
    )

    result = _parse_vorbis_tags(mock_tags)

    assert result.get("albumartist") == "Single Album Artist"
    assert "albumartists" not in result


def test_parse_vorbis_tags_explicit_artists_tag_takes_precedence() -> None:
    """Test that explicit ARTISTS tag takes precedence over multiple ARTIST fields."""
    mock_tags = _create_mock_vorbis_tags(
        {
            "ARTIST": ["Artist A", "Artist B"],  # Multiple ARTIST fields
            "ARTISTS": [
                "Explicit Artist 1",
                "Explicit Artist 2",
                "Explicit Artist 3",
            ],  # Explicit tag
        }
    )

    result = _parse_vorbis_tags(mock_tags)

    # ARTISTS tag should take precedence
    assert result.get("artists") == ["Explicit Artist 1", "Explicit Artist 2", "Explicit Artist 3"]


def test_parse_vorbis_tags_musicbrainz_ids() -> None:
    """Test that MusicBrainz IDs are parsed correctly from Vorbis tags."""
    mock_tags = _create_mock_vorbis_tags(
        {
            "ARTIST": ["Artist 1", "Artist 2"],
            "MUSICBRAINZ_ARTISTID": ["mb-id-1", "mb-id-2"],
            "MUSICBRAINZ_ALBUMID": ["mb-album-id"],
            "MUSICBRAINZ_TRACKID": ["mb-track-id"],
        }
    )

    result = _parse_vorbis_tags(mock_tags)

    assert result.get("musicbrainzartistid") == ["mb-id-1", "mb-id-2"]
    assert result.get("musicbrainzalbumid") == "mb-album-id"
    assert result.get("musicbrainzrecordingid") == "mb-track-id"


def _create_mock_apev2_tags(tag_dict: dict[str, str]) -> MagicMock:
    r"""Create a mock APEv2 tags object.

    :param tag_dict: Dictionary mapping tag names to values (use \x00 for multi-value).
    """
    mock = MagicMock()
    mock.__contains__ = lambda _, key: key in tag_dict
    mock.__getitem__ = lambda _, key: tag_dict[key]
    mock.keys = lambda: tag_dict.keys()
    return mock


def test_parse_apev2_tags_multi_value_artists() -> None:
    """Test that APEv2 multi-value fields (null-separated) are parsed correctly."""
    mock_tags = _create_mock_apev2_tags(
        {
            "Title": "My Song",
            "Album": "My Album",
            "Artist": "Single Artist",
            "Artists": "Artist 1\x00Artist 2\x00Artist 3",  # Null-separated
        }
    )

    result = _parse_apev2_tags(mock_tags)

    assert result.get("title") == "My Song"
    assert result.get("album") == "My Album"
    assert result.get("artist") == "Single Artist"
    assert result.get("artists") == ["Artist 1", "Artist 2", "Artist 3"]


def test_parse_apev2_tags_musicbrainz_ids() -> None:
    """Test that MusicBrainz IDs are parsed correctly from APEv2 tags."""
    mock_tags = _create_mock_apev2_tags(
        {
            "MUSICBRAINZ_ARTISTID": "mb-id-1\x00mb-id-2",  # Multi-value
            "MUSICBRAINZ_ALBUMID": "mb-album-id",
            "MUSICBRAINZ_TRACKID": "mb-track-id",  # Recording ID in APEv2
            "MUSICBRAINZ_RELEASEGROUPID": "mb-rg-id",
        }
    )

    result = _parse_apev2_tags(mock_tags)

    assert result.get("musicbrainzartistid") == ["mb-id-1", "mb-id-2"]
    assert result.get("musicbrainzalbumid") == "mb-album-id"
    assert result.get("musicbrainzrecordingid") == "mb-track-id"
    assert result.get("musicbrainzreleasegroupid") == "mb-rg-id"


def test_parse_apev2_tags_genre_multi_value() -> None:
    """Test that APEv2 genre with multiple values is parsed correctly."""
    mock_tags = _create_mock_apev2_tags(
        {
            "Genre": "Rock\x00Pop\x00Jazz",
        }
    )

    result = _parse_apev2_tags(mock_tags)

    assert result.get("genre") == ["Rock", "Pop", "Jazz"]
