"""Tests for the shared NFO helpers."""

import logging
import uuid

import pytest
from music_assistant_models.enums import ArtistEntityType
from music_assistant_models.media_items import Artist
from music_assistant_models.media_items.metadata import LifeSpan

from music_assistant.helpers.nfo import parse_artist_nfo, parse_nfo_root


def test_parse_nfo_root_returns_the_named_root_element() -> None:
    """A well-formed NFO with the expected root element returns it as a dict."""
    data = b"<album><title>My Album</title></album>"
    root = parse_nfo_root(data, "album")
    assert root is not None
    assert root["title"] == "My Album"


def test_parse_nfo_root_matches_tag_names_case_insensitively() -> None:
    """Tag names are lowercased, so Kodi's camelCase tags match the lowercase lookups."""
    data = (
        b"<artist><name>Name</name><musicBrainzArtistID>abc</musicBrainzArtistID>"
        b'<thumb Aspect="thumb">x</thumb></artist>'
    )
    root = parse_nfo_root(data, "artist")
    assert root is not None
    assert root["musicbrainzartistid"] == "abc"
    assert root["thumb"] == {"@aspect": "thumb", "#text": "x"}


@pytest.mark.parametrize(
    ("data", "root_tag"),
    [
        (b"not xml at all <<<", "album"),
        (b"<artist><title>Name</title></artist>", "album"),  # wrong root element
        (b"\xff\xfe not utf-8", "album"),
        (b"<album>just text, no dict</album>", "album"),
    ],
)
def test_parse_nfo_root_returns_none_for_malformed_content(data: bytes, root_tag: str) -> None:
    """Malformed XML, an undecodable file or the wrong/non-dict root element returns None."""
    assert parse_nfo_root(data, root_tag) is None


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
    """Born/died take part in the life span; dates that are not recognised are ignored."""
    artist = Artist(item_id="test", provider="test", name="", provider_mappings=set())
    parse_artist_nfo(artist, {"born": "1958-08-29"})
    assert artist.metadata.life_span == LifeSpan(begin="1958-08-29", end=None, ended=False)

    artist = Artist(item_id="test", provider="test", name="", provider_mappings=set())
    parse_artist_nfo(artist, {"born": "August 29, 1958", "died": "2009-06"})
    assert artist.metadata.life_span == LifeSpan(begin="1958-08-29", end="2009-06", ended=True)

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


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("15 Dec 1970", "1970-12-15"),
        ("15 December 1970", "1970-12-15"),
        ("December 15, 1970", "1970-12-15"),
        ("Dec 15 1970", "1970-12-15"),
        (" 5 MAY 1970 ", "1970-05-05"),
        ("29 Feb 2000", "2000-02-29"),
        ("15/12/1970", "1970-12-15"),
        ("Dec 1970", "1970-12"),
        ("December, 1970", "1970-12"),
        ("1970/12", "1970-12"),
        ("1970.12", "1970-12"),
        ("12-1970", "1970-12"),
        ("12.1970", "1970-12"),
        ("13/1970", None),
        ("1970.0", None),
        ("Decem 1970", None),
        ("5/6/1970", "1970-06-05"),
        ("1970/12/15", "1970-12-15"),
        ("12/15/1970", None),
        ("1970/15/12", None),
        ("15-12-1970", "1970-12-15"),
        ("15.12.1970", "1970-12-15"),
        ("1970.12.15", "1970-12-15"),
        ("1970-1-5", "1970-01-05"),
        ("15/12-1970", None),
        ("15 Decem 1970", None),
        ("31 April 1970", None),
        ("29 Feb 2023", None),
    ],
)
def test_parse_artist_nfo_reads_written_out_dates(value: str, expected: str | None) -> None:
    """Full and year-month dates with a month name or a numeric month are read; impossible ones not."""
    artist = Artist(item_id="test", provider="test", name="", provider_mappings=set())
    parse_artist_nfo(artist, {"born": value})
    assert (artist.metadata.life_span.begin if artist.metadata.life_span else None) == expected


@pytest.mark.parametrize("value", ["2024-02-29", "2000-02-29", "1958-12", "1958"])
def test_parse_artist_nfo_keeps_valid_dates_with_their_precision(value: str) -> None:
    """Valid (partial) dates, leap days included, are kept exactly as written."""
    artist = Artist(item_id="test", provider="test", name="", provider_mappings=set())
    parse_artist_nfo(artist, {"died": value})
    assert artist.metadata.life_span == LifeSpan(begin=None, end=value, ended=True)


def test_parse_artist_nfo_takes_the_first_of_repeated_single_value_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A single-value field given more than once uses its first value and logs a warning."""
    artist = Artist(item_id="test", provider="test", name="", provider_mappings=set())
    root = parse_nfo_root(
        b"<artist><title>Test Artist</title><type>Group</type><type>Person</type>"
        b"<born>1970</born><born>1971</born><died>2001</died>"
        b"<biography>One</biography><biography>Two</biography></artist>",
        "artist",
    )
    assert root is not None

    with caplog.at_level(logging.WARNING):
        parse_artist_nfo(artist, root, "Test Artist/artist.nfo")

    assert artist.name == "Test Artist"
    assert artist.metadata.artist_entity_type == ArtistEntityType.GROUP
    assert artist.metadata.life_span == LifeSpan(begin="1970", end="2001", ended=True)
    assert artist.metadata.description == "One"
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 3
    assert all("Test Artist/artist.nfo" in message for message in warnings)
    for key in ("type", "born", "biography"):
        assert any(f"<{key}>" in message for message in warnings)


def test_parse_artist_nfo_ignores_single_value_fields_without_text() -> None:
    """A single-value field that holds no plain text is ignored."""
    artist = Artist(item_id="test", provider="test", name="Kept", provider_mappings=set())

    parse_artist_nfo(artist, {"title": {"#text": "x", "@lang": "en"}, "biography": [{"p": "x"}]})

    assert artist.name == "Kept"
    assert artist.metadata.description is None
