"""Tests for the filesystem provider parsers."""

import uuid

import pytest
from music_assistant_models.enums import ArtistEntityType, ExternalID
from music_assistant_models.media_items import Album, Artist
from music_assistant_models.media_items.metadata import LifeSpan
from music_assistant_models.unique_list import UniqueList

from music_assistant.providers.filesystem_local.parsers import (
    nfo_album_artist,
    parse_album_nfo,
    parse_artist_nfo,
)


def test_parse_album_nfo() -> None:
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

    def _asserts(res: Album) -> None:
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

    parse_album_nfo(
        album,
        {
            "title": "Test Album (Test Version)",
            "sortname": "Test Sortname",
            "musicbrainzreleasegroupid": mb_releasegroup,
            "musicbrainzalbumid": mb_album,
            "musicbrainzalbumartistid": mb_artist,
            "review": "Test Description",
            "year": "1967",
            "genre": "Test Genre",
        },
    )
    _asserts(album)

    parse_album_nfo(album, {})  # empty dict or missing keys should not change anything
    _asserts(album)

    parse_album_nfo(
        album,
        {  # None and empty string should not erase existing values
            "title": None,
            "sortname": "",
        },
    )
    _asserts(album)


def test_parse_artist_nfo() -> None:
    """Test parsing of an artist NFO file."""
    artist = Artist(item_id="test", provider="test", name="", provider_mappings=set())

    mb_artist = str(uuid.uuid4())

    def _asserts(res: Artist) -> None:
        assert res.name == "Test Artist"
        assert res.sort_name == "Test Sortname"
        assert res.mbid == mb_artist
        assert res.metadata.description == "Test Biography"
        assert res.metadata.genres == {"Test Genre"}
        assert res.metadata.style == "Rock; Pop"
        assert res.metadata.mood == "Energetic"
        assert res.metadata.artist_entity_type == ArtistEntityType.GROUP
        assert res.metadata.life_span == LifeSpan(begin="1960", end="1970-04-10", ended=True)

    parse_artist_nfo(
        artist,
        {
            "title": "Test Artist",
            "sortname": "Test Sortname",
            "musicbrainzartistid": mb_artist,
            "biography": "Test Biography",
            "genre": "Test Genre",
            "style": ["Rock", "Pop"],
            "mood": "Energetic",
            "type": "Group",
            "formed": "1960",
            "disbanded": "1970-04-10",
        },
    )
    _asserts(artist)

    parse_artist_nfo(artist, {})  # empty dict or missing keys should not change anything
    _asserts(artist)

    parse_artist_nfo(
        artist,
        {  # None and empty string should not erase existing values
            "title": None,
            "sortname": "",
        },
    )
    _asserts(artist)


def test_parse_artist_nfo_life_span() -> None:
    """Born/died take part in the life span; dates that are not ISO are ignored."""
    artist = Artist(item_id="test", provider="test", name="", provider_mappings=set())
    parse_artist_nfo(artist, {"born": "1958-08-29"})
    assert artist.metadata.life_span == LifeSpan(begin="1958-08-29", end=None, ended=False)

    artist = Artist(item_id="test", provider="test", name="", provider_mappings=set())
    parse_artist_nfo(artist, {"born": "August 29, 1958", "died": "2009-06"})
    assert artist.metadata.life_span == LifeSpan(begin=None, end="2009-06", ended=True)

    artist = Artist(item_id="test", provider="test", name="", provider_mappings=set())
    parse_artist_nfo(artist, {"born": "unknown", "type": "not-a-type"})
    assert artist.metadata.life_span is None
    assert artist.metadata.artist_entity_type is None


@pytest.mark.parametrize("value", ["0000-00-00", "1958-13", "1958-00", "2023-02-29", "1958-04-31"])
def test_parse_artist_nfo_ignores_impossible_dates(value: str) -> None:
    """Dates that match the ISO shape but do not exist on the calendar are ignored."""
    artist = Artist(item_id="test", provider="test", name="", provider_mappings=set())
    parse_artist_nfo(artist, {"born": value, "died": value})
    assert artist.metadata.life_span is None


@pytest.mark.parametrize("value", ["2024-02-29", "2000-02-29", "1958-12", "1958"])
def test_parse_artist_nfo_keeps_valid_dates_with_their_precision(value: str) -> None:
    """Valid (partial) dates, leap days included, are kept exactly as written."""
    artist = Artist(item_id="test", provider="test", name="", provider_mappings=set())
    parse_artist_nfo(artist, {"died": value})
    assert artist.metadata.life_span == LifeSpan(begin=None, end=value, ended=True)


def test_nfo_album_artist() -> None:
    """Only exactly one named album artist is used, anything ambiguous is not."""
    assert nfo_album_artist({"albumartist": "The Beatles"}) == "The Beatles"
    assert nfo_album_artist({}) is None
    assert nfo_album_artist({"albumartist": None}) is None
    assert nfo_album_artist({"albumartist": ["Artist A", "Artist B"]}) is None
    assert nfo_album_artist({"albumartist": "Artist A; Artist B"}) is None
