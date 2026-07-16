"""Tests for the filesystem provider parsers."""
import uuid

from music_assistant_models.enums import ExternalID
from music_assistant_models.media_items import Album, Artist
from music_assistant_models.unique_list import UniqueList

from music_assistant.providers.filesystem_local import parse_album_nfo


def test_parse_album_nfo():
    """Test parsing of an album NFO file."""
    album = Album(
        item_id="test",
        provider="test",
        name="",
        artists=UniqueList(
            [Artist(item_id="test", provider="test", name="test", provider_mappings=set())]
        ),
        provider_mappings=set(),
    )

    mb_releasegroup = str(uuid.uuid4())
    mb_album = str(uuid.uuid4())
    mb_artist = str(uuid.uuid4())

    def _asserts(res: Album):
        assert res.name == "Test Album"
        assert res.version == "Test Version"
        assert res.sort_name == "Test Sortname"
        assert res.external_ids == {
            (ExternalID.MB_RELEASEGROUP, mb_releasegroup),
            (ExternalID.MB_ALBUM, mb_album),
        }
        assert res.artists[0].mbid == mb_artist
        assert res.metadata.description == "Test Description"
        assert res.year == 1967
        assert res.metadata.genres == {"Test Genre"}

    parse_album_nfo(album, {
        "title": "Test Album (Test Version)",
        "sortname": "Test Sortname",
        "musicbrainzreleasegroupid": mb_releasegroup,
        "musicbrainzalbumid": mb_album,
        "musicbrainzalbumartistid": mb_artist,
        "review": "Test Description",
        "year": "1967",
        "genre": "Test Genre",
    })
    _asserts(album)

    parse_album_nfo(album, {}) # empty dict or missing keys should not change anything
    _asserts(album)

    parse_album_nfo(album, { # None and empty string should not erase existing values
        "title": None,
        "sortname": "",
    })
    _asserts(album)
