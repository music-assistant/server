"""Tests for parsing ID3 tags functions."""

import pathlib

from music_assistant.server.helpers import tags

RESOURCES_DIR = pathlib.Path(__file__).parent.resolve().joinpath("fixtures")

FILE_1 = str(RESOURCES_DIR.joinpath("MyArtist - MyTitle.mp3"))


async def test_parse_metadata_from_id3tags() -> None:
    """Test parsing of parsing metadata from ID3 tags."""
    filename = str(RESOURCES_DIR.joinpath("MyArtist - MyTitle.mp3"))
    _tags = await tags.parse_tags(filename)
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


async def test_parse_metadata_from_filename() -> None:
    """Test parsing of parsing metadata from filename."""
    filename = str(RESOURCES_DIR.joinpath("MyArtist - MyTitle without Tags.mp3"))
    _tags = await tags.parse_tags(filename)
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
    _tags = await tags.parse_tags(filename)
    assert _tags.album is None
    assert _tags.title == "test"
    assert _tags.duration == 1.032
    assert _tags.album_artists == ()
    assert _tags.artists == (tags.UNKNOWN_ARTIST,)
    assert _tags.genres == ()
    assert _tags.musicbrainz_albumartistids == ()
    assert _tags.musicbrainz_artistids == ()
    assert _tags.musicbrainz_releasegroupid is None
    assert _tags.musicbrainz_recordingid is None
