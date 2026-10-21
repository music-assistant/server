"""Tests for the shared NFO helpers."""

import uuid

import pytest
from music_assistant_models.media_items import Artist

from music_assistant.helpers.nfo import parse_artist_nfo, parse_nfo_root


def test_parse_nfo_root_returns_the_named_root_element() -> None:
    """A well-formed NFO with the expected root element returns it as a dict."""
    data = b"<album><title>My Album</title></album>"
    root = parse_nfo_root(data, "album")
    assert root is not None
    assert root["title"] == "My Album"


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

    parse_artist_nfo(
        artist,
        {
            "title": "Test Artist",
            "sortname": "Test Sortname",
            "musicbrainzartistid": mb_artist,
            "biography": "Test Biography",
            "genre": "Test Genre",
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
