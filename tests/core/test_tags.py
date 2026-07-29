"""Tests for parsing audio file tags (ID3, MP4/AAC, Vorbis, APEv2, etc.)."""

import pathlib
import shutil
from unittest.mock import MagicMock

import mutagen
import pytest

# mutagen 1.47 does not re-export UFID explicitly (fixed in 1.48, which dev pins)
from mutagen.id3 import ID3, UFID  # type: ignore[attr-defined]

from music_assistant.constants import UNKNOWN_ARTIST
from music_assistant.helpers import tags
from music_assistant.helpers.tags import (
    _parse_apev2_tags,
    _parse_id3_tags,
    _parse_mp4_tags,
    _parse_vorbis_tags,
    clean_mbid,
    parse_tags_mutagen,
    split_artists,
    write_replaygain_track_gain,
)

RESOURCES_DIR = pathlib.Path(__file__).parent.parent.resolve().joinpath("fixtures")

FILE_MP3 = str(RESOURCES_DIR.joinpath("MyArtist - MyTitle.mp3"))
FILE_MP3_ID3V24_MULTIVALUE = str(RESOURCES_DIR.joinpath("MultiArtist-ID3v24-NullSeparated.mp3"))
FILE_M4A = str(RESOURCES_DIR.joinpath("MyArtist - MyTitle.m4a"))
FILE_FLAC = str(RESOURCES_DIR.joinpath("MultipleArtists.flac"))
FILE_FLAC_SEMICOLON = str(RESOURCES_DIR.joinpath("ArtistWithSemicolon.flac"))
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
    _tags.tags["disc"] = "1/1"  # type: ignore[unreachable]
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


async def test_parse_id3v24_null_separated_artists() -> None:
    """Test parsing ID3v2.4 tags with null-separated multi-value TPE1/TPE2."""
    _tags = await tags.async_parse_tags(FILE_MP3_ID3V24_MULTIVALUE)
    # Null-separated artists in TPE1 should be parsed as multiple artists
    assert _tags.artists == ("Artist One", "Artist Two", "Artist Three")
    # Null-separated album artists in TPE2 should be parsed as multiple album artists
    assert _tags.album_artists == ("Album Artist A", "Album Artist B")
    # MB IDs should match
    assert _tags.musicbrainz_artistids == ("mb-artist-1", "mb-artist-2", "mb-artist-3")
    assert _tags.musicbrainz_albumartistids == ("mb-albumartist-1", "mb-albumartist-2")


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

    # " presents " should split without disturbing an ampersand inside an artist name
    result = split_artists("Above & Beyond presents OceanLab", expected_count=None)
    assert result == ("Above & Beyond", "OceanLab")


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


def test_parse_vorbis_multi_value_releasetype() -> None:
    """Repeated RELEASETYPE Vorbis fields are joined into a single value."""
    mock_tags = _create_mock_vorbis_tags({"RELEASETYPE": ["album", "live"]})
    result = _parse_vorbis_tags(mock_tags)
    assert result.get("musicbrainzalbumtype") == "album;live"


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


def test_parse_apev2_multi_value_musicbrainz_albumtype() -> None:
    """Null-separated MUSICBRAINZ_ALBUMTYPE values are joined into a single value."""
    mock_tags = _create_mock_apev2_tags({"MUSICBRAINZ_ALBUMTYPE": "album\x00live"})
    result = _parse_apev2_tags(mock_tags)
    assert result.get("musicbrainzalbumtype") == "album;live"


def test_parse_apev2_tags_genre_multi_value() -> None:
    """Test that APEv2 genre with multiple values is parsed correctly."""
    mock_tags = _create_mock_apev2_tags(
        {
            "Genre": "Rock\x00Pop\x00Jazz",
        }
    )

    result = _parse_apev2_tags(mock_tags)

    assert result.get("genre") == ["Rock", "Pop", "Jazz"]


def test_parse_apev2_tags_null_separated_artists() -> None:
    """Test that APEv2 null-separated Artist field is parsed as multiple artists."""
    mock_tags = _create_mock_apev2_tags(
        {
            "Artist": "ave;new\x00佐倉紗織",
            "Album Artist": "Album Artist A\x00Album Artist B",
        }
    )

    result = _parse_apev2_tags(mock_tags)

    # Multiple null-separated values should be stored as "artists" (plural)
    assert result.get("artists") == ["ave;new", "佐倉紗織"]
    assert result.get("albumartists") == ["Album Artist A", "Album Artist B"]
    # Singular keys should not be set
    assert "artist" not in result
    assert "albumartist" not in result


def test_parse_apev2_tags_single_artist() -> None:
    """Test that APEv2 single Artist field is parsed as singular."""
    mock_tags = _create_mock_apev2_tags(
        {
            "Artist": "Single Artist",
            "Album Artist": "Single Album Artist",
        }
    )

    result = _parse_apev2_tags(mock_tags)

    # Single value should be stored as "artist" (singular)
    assert result.get("artist") == "Single Artist"
    assert result.get("albumartist") == "Single Album Artist"
    # Plural keys should not be set
    assert "artists" not in result
    assert "albumartists" not in result


def test_parse_mp4_multi_value_musicbrainz_albumtype() -> None:
    """Multi-value MP4 freeform album type entries are joined into a single value."""
    mock_tags = MagicMock()
    mock_tags.__contains__ = lambda _, key: key == "----:com.apple.iTunes:MusicBrainz Album Type"
    mock_tags.__getitem__ = lambda _, _k: [b"album", b"live"]
    result = _parse_mp4_tags(mock_tags)
    assert result.get("musicbrainzalbumtype") == "album;live"


def test_parse_id3_multi_value_musicbrainz_albumtype() -> None:
    """Multi-value TXXX:MusicBrainz Album Type frame entries are joined into a single value."""
    frame = MagicMock()
    frame.text = ["album", "live"]
    result = _parse_id3_tags({"TXXX:MusicBrainz Album Type": frame})
    assert result.get("musicbrainzalbumtype") == "album;live"


def test_vorbis_multiple_artist_fields_semicolon_in_name() -> None:
    """Test that multiple ARTIST fields in Vorbis with semicolons are handled correctly.

    Regression test for the "ave;new" edge case per the Vorbis spec:
    - Japanese artist "ave;new" has a semicolon in their name
    - Vorbis allows multiple ARTIST (singular) fields for multi-artist tracks
    - The semicolon within "ave;new" must NOT cause additional splitting

    Correct Vorbis tagging (per https://xiph.org/vorbis/doc/v-comment.html):
        ARTIST=ave;new
        ARTIST=佐倉紗織
        MUSICBRAINZ_ARTISTID=2ade7b3c-a6f1-4d00-b7f7-fc60abf25dba
        MUSICBRAINZ_ARTISTID=822c07bd-1f8a-4fef-acdb-8acfe82fbef5

    See: https://musicbrainz.org/artist/2ade7b3c-a6f1-4d00-b7f7-fc60abf25dba
    """
    # Simulate Vorbis tags with multiple ARTIST fields (correct per Vorbis spec)
    mock_tags = _create_mock_vorbis_tags(
        {
            "TITLE": ["Call My Dears"],
            "ALBUM": ["Lovable"],
            "ARTIST": ["ave;new", "佐倉紗織"],  # Multiple ARTIST fields
            "ARTISTSORT": ["ave;new feat.Sakura, Saori"],
            "MUSICBRAINZ_ARTISTID": [
                "2ade7b3c-a6f1-4d00-b7f7-fc60abf25dba",
                "822c07bd-1f8a-4fef-acdb-8acfe82fbef5",
            ],
        }
    )

    result = _parse_vorbis_tags(mock_tags)

    # Multiple ARTIST fields should be stored as "artists" (plural key)
    assert result.get("artists") == ["ave;new", "佐倉紗織"]
    # MusicBrainz Artist IDs should be preserved
    assert result.get("musicbrainzartistid") == [
        "2ade7b3c-a6f1-4d00-b7f7-fc60abf25dba",
        "822c07bd-1f8a-4fef-acdb-8acfe82fbef5",
    ]

    # Now test that AudioTags.artists property correctly handles the multiple fields
    audio_tags = tags.AudioTags(
        raw={},
        sample_rate=44100,
        channels=2,
        bits_per_sample=16,
        format="flac",
        bit_rate=None,
        duration=180.0,
        tags=result,
        has_cover_image=False,
        filename="01 - ave;new feat.佐倉紗織 - Call My Dears.flac",
    )

    # The artists property must return exactly 2 artists
    assert audio_tags.artists == ("ave;new", "佐倉紗織")
    # The semicolon in "ave;new" must NOT cause it to be split
    assert "ave" not in audio_tags.artists
    assert "new" not in audio_tags.artists
    # MusicBrainz Artist IDs should match the artist count
    assert audio_tags.musicbrainz_artistids == (
        "2ade7b3c-a6f1-4d00-b7f7-fc60abf25dba",
        "822c07bd-1f8a-4fef-acdb-8acfe82fbef5",
    )
    assert len(audio_tags.artists) == len(audio_tags.musicbrainz_artistids)


async def test_flac_multiple_artist_fields_semicolon_e2e() -> None:
    """End-to-end test: FLAC with multiple ARTIST fields, one containing semicolon.

    Tests real file parsing to ensure the full pipeline correctly handles
    artist names with semicolons when using multiple ARTIST fields per Vorbis spec.

    See: https://xiph.org/vorbis/doc/v-comment.html
    See: https://musicbrainz.org/artist/2ade7b3c-a6f1-4d00-b7f7-fc60abf25dba
    """
    audio_tags = await tags.async_parse_tags(FILE_FLAC_SEMICOLON)

    # Verify the artists are correctly parsed without splitting on semicolons
    assert audio_tags.artists == ("ave;new", "佐倉紗織")
    assert "ave" not in audio_tags.artists
    assert "new" not in audio_tags.artists

    # Verify MB Artist IDs match
    assert audio_tags.musicbrainz_artistids == (
        "2ade7b3c-a6f1-4d00-b7f7-fc60abf25dba",
        "822c07bd-1f8a-4fef-acdb-8acfe82fbef5",
    )
    assert len(audio_tags.artists) == len(audio_tags.musicbrainz_artistids)

    # Verify other tags
    assert audio_tags.title == "Call My Dears"
    assert audio_tags.album == "Lovable"


def test_id3_artist_tag_semicolon_single_mbid() -> None:
    """Test that single ARTIST tag with semicolon is not split when 1 MB ID exists.

    Regression test for formats without multi-value ARTISTS tag support (ID3, etc.):
    - Artist name "ave;new" contains a semicolon
    - Single MUSICBRAINZ_ARTISTID confirms this is one artist
    - The semicolon must NOT cause the name to be split into "ave" and "new"

    See: https://musicbrainz.org/artist/2ade7b3c-a6f1-4d00-b7f7-fc60abf25dba
    """
    # Simulate ID3 tags: single ARTIST field with semicolon, single MB ID
    audio_tags = tags.AudioTags(
        raw={},
        sample_rate=44100,
        channels=2,
        bits_per_sample=16,
        format="mp3",
        bit_rate=None,
        duration=180.0,
        tags={
            "title": "Colorful",
            "album": "Lovable",
            "artist": "ave;new",
            "musicbrainzartistid": "2ade7b3c-a6f1-4d00-b7f7-fc60abf25dba",
        },
        has_cover_image=False,
        filename="01 - ave;new - Colorful.mp3",
    )

    # Single MB ID = single artist, no splitting
    assert audio_tags.artists == ("ave;new",)
    assert audio_tags.musicbrainz_artistids == ("2ade7b3c-a6f1-4d00-b7f7-fc60abf25dba",)
    # Verify the semicolon did NOT cause incorrect splitting
    assert "ave" not in audio_tags.artists
    assert "new" not in audio_tags.artists


def test_artists_tag_semicolon_single_mbid() -> None:
    """Test that ARTISTS tag with semicolon is not split when 1 MB ID exists.

    Regression test for the ARTISTS (plural) tag path:
    - Artist name "ave;new" contains a semicolon
    - Single MUSICBRAINZ_ARTISTID confirms this is one artist
    - The semicolon must NOT cause the name to be split

    Based on real tags from ave;new's "Lovable" album track "eve".
    See: https://musicbrainz.org/artist/2ade7b3c-a6f1-4d00-b7f7-fc60abf25dba
    """
    audio_tags = tags.AudioTags(
        raw={},
        sample_rate=44100,
        channels=2,
        bits_per_sample=16,
        format="flac",
        bit_rate=None,
        duration=180.0,
        tags={
            "title": "eve",
            "album": "Lovable",
            "artist": "ave;new",
            "artists": "ave;new",  # ARTISTS tag with semicolon
            "artistsort": "ave;new",
            "musicbrainzartistid": "2ade7b3c-a6f1-4d00-b7f7-fc60abf25dba",
            "musicbrainzrecordingid": "0389384e-3015-45ba-8a09-d949ff68f9d9",
        },
        has_cover_image=False,
        filename="04 - ave;new - eve.flac",
    )

    # Single MB ID = single artist, ARTISTS tag should NOT be split on semicolon
    assert audio_tags.artists == ("ave;new",)
    assert audio_tags.musicbrainz_artistids == ("2ade7b3c-a6f1-4d00-b7f7-fc60abf25dba",)
    # Verify the semicolon did NOT cause incorrect splitting
    assert "ave" not in audio_tags.artists
    assert "new" not in audio_tags.artists


def test_id3_artist_tag_semicolon_multiple_mbids() -> None:
    """Test that ARTIST tag with semicolon IS split when multiple MB IDs exist.

    When multiple MusicBrainz Artist IDs are present, the semicolon should be
    treated as a separator between artists.
    """
    audio_tags = tags.AudioTags(
        raw={},
        sample_rate=44100,
        channels=2,
        bits_per_sample=16,
        format="mp3",
        bit_rate=None,
        duration=180.0,
        # musicbrainzartistid can be list[str] from mutagen (dict type is str for ffprobe compat)
        tags={
            "artist": "Artist A;Artist B",
            "musicbrainzartistid": ["id-a", "id-b"],  # type: ignore[dict-item]
        },
        has_cover_image=False,
        filename="test.mp3",
    )

    # Multiple MB IDs = semicolon should split
    assert audio_tags.artists == ("Artist A", "Artist B")
    assert audio_tags.musicbrainz_artistids == ("id-a", "id-b")


def test_id3_albumartist_tag_semicolon_single_mbid() -> None:
    """Test that ALBUMARTIST tag with semicolon is not split when 1 MB Album Artist ID exists."""
    audio_tags = tags.AudioTags(
        raw={},
        sample_rate=44100,
        channels=2,
        bits_per_sample=16,
        format="mp3",
        bit_rate=None,
        duration=180.0,
        tags={
            "albumartist": "ave;new",
            "musicbrainzalbumartistid": "2ade7b3c-a6f1-4d00-b7f7-fc60abf25dba",
        },
        has_cover_image=False,
        filename="test.mp3",
    )

    # Single MB Album Artist ID = single artist, no splitting
    assert audio_tags.album_artists == ("ave;new",)
    assert audio_tags.musicbrainz_albumartistids == ("2ade7b3c-a6f1-4d00-b7f7-fc60abf25dba",)


def _read_replaygain_track_gain(path: str) -> str | None:
    """Read REPLAYGAIN_TRACK_GAIN from a file using mutagen (format-agnostic)."""
    audio = mutagen.File(path)  # type: ignore[attr-defined]
    if audio is None or audio.tags is None:
        return None
    tag_key_mp4 = "----:com.apple.iTunes:REPLAYGAIN_TRACK_GAIN"
    if tag_key_mp4 in audio.tags:
        val = audio.tags[tag_key_mp4][0]
        return val.decode("utf-8") if isinstance(val, bytes) else str(val)
    if "TXXX:REPLAYGAIN_TRACK_GAIN" in audio.tags:
        return str(audio.tags["TXXX:REPLAYGAIN_TRACK_GAIN"].text[0])
    if "REPLAYGAIN_TRACK_GAIN" in audio.tags:
        return str(audio.tags["REPLAYGAIN_TRACK_GAIN"][0])
    return None


@pytest.mark.parametrize(
    "source",
    [FILE_MP3, FILE_M4A, FILE_FLAC, FILE_WV],
)
async def test_write_replaygain_track_gain_roundtrip(tmp_path: pathlib.Path, source: str) -> None:
    """Write a REPLAYGAIN_TRACK_GAIN tag and verify the value is read back."""
    dest = tmp_path / pathlib.Path(source).name
    shutil.copy(source, dest)

    assert await write_replaygain_track_gain(str(dest), -5.3) is True
    assert _read_replaygain_track_gain(str(dest)) == "-5.30 dB"

    # verify overwrite replaces the previous value
    assert await write_replaygain_track_gain(str(dest), -2.1) is True
    assert _read_replaygain_track_gain(str(dest)) == "-2.10 dB"


async def test_write_replaygain_track_gain_missing_file(tmp_path: pathlib.Path) -> None:
    """Return False if the file does not exist or cannot be opened."""
    assert await write_replaygain_track_gain(str(tmp_path / "nope.mp3"), -5.0) is False


async def test_write_replaygain_track_gain_read_only(tmp_path: pathlib.Path) -> None:
    """Return False if the file cannot be written to."""
    dest = tmp_path / "readonly.mp3"
    shutil.copy(FILE_MP3, dest)
    dest.chmod(0o444)
    try:
        assert await write_replaygain_track_gain(str(dest), -5.0) is False
    finally:
        # restore permissions so tmp_path cleanup can remove the file
        dest.chmod(0o644)


VALID_MBID = "73c69a4b-1f9e-4c8c-b8bb-3ba903af1c3f"


def test_clean_mbid() -> None:
    """Test cleaning/canonicalizing MusicBrainz identifiers from file tags."""
    assert clean_mbid(VALID_MBID) == VALID_MBID
    # uppercase hex digits are canonicalized to lowercase
    assert clean_mbid(VALID_MBID.upper()) == VALID_MBID
    # trailing NUL bytes and surrounding whitespace are stripped
    assert clean_mbid(f"{VALID_MBID}\x00") == VALID_MBID
    assert clean_mbid(f"  {VALID_MBID} \n") == VALID_MBID
    # non-UUID values are rejected
    assert clean_mbid("CAAE0466 1G4B0N3 07800NE1") is None
    assert clean_mbid("abcdefg") is None
    assert clean_mbid("") is None
    assert clean_mbid(None) is None
    # non-string values (e.g. repeated NFO elements parsed as a list) are rejected
    assert clean_mbid([VALID_MBID, VALID_MBID]) is None  # type: ignore[arg-type]


def test_parse_id3_ufid_frame_binary_data() -> None:
    """A UFID frame with non-UTF-8 binary data must not break parsing of other tags."""
    ufid = UFID(  # type: ignore[no-untyped-call]
        owner="http://musicbrainz.org",
        data=bytes.fromhex("73c69a4b1f9e4c8cb8bb3ba903af1c3f"),
    )
    title_frame = MagicMock()
    title_frame.text = ["MyTitle"]
    frames = {"UFID:http://musicbrainz.org": ufid, "TIT2": title_frame}

    mock_tags = MagicMock()
    mock_tags.get = frames.get
    result = _parse_id3_tags(mock_tags)

    assert result.get("title") == "MyTitle"
    # the garbled identifier is rejected downstream by clean_mbid
    assert clean_mbid(result.get("musicbrainzrecordingid")) is None


async def test_parse_ufid_frame_with_dirty_payload(tmp_path: pathlib.Path) -> None:
    """
    A UFID payload with a NUL terminator must still yield a usable recording id.

    Regression test for https://github.com/music-assistant/support/issues/5906
    where such files failed to import with "Invalid MusicBrainz identifier".
    """
    dest = tmp_path / "ufid.mp3"
    shutil.copy(FILE_MP3, dest)
    id3 = ID3(str(dest))  # type: ignore[no-untyped-call]
    id3.add(  # type: ignore[no-untyped-call]
        UFID(owner="http://musicbrainz.org", data=f"{VALID_MBID}\x00".encode("ascii"))  # type: ignore[no-untyped-call]
    )
    id3.save()

    _tags = await tags.async_parse_tags(str(dest))
    assert clean_mbid(_tags.musicbrainz_recordingid) == VALID_MBID
