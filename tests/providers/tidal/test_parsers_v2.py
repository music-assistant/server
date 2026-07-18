"""Test parsing of official Tidal API (v2) JSON:API responses."""

import json
import pathlib
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from music_assistant_models.enums import AlbumType, ExternalID, ImageType, LinkType
from music_assistant_models.media_items import ItemMapping

from music_assistant.providers.tidal.jsonapi import JsonApiDocument
from music_assistant.providers.tidal.parsers_v2 import (
    _map_album_type,
    _parse_iso_duration,
    _select_image_url,
    _split_title_version,
    parse_album,
    parse_artist,
    parse_track,
)

if TYPE_CHECKING:
    from music_assistant_models.enums import MediaType

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures" / "v2"


@pytest.fixture
def provider_mock() -> Mock:
    """Return a mock provider."""
    provider = Mock()
    provider.domain = "tidal"
    provider.instance_id = "tidal_instance"

    def get_item_mapping(media_type: MediaType, key: str, name: str) -> ItemMapping:
        return ItemMapping(
            media_type=media_type, item_id=key, provider=provider.instance_id, name=name
        )

    provider.get_item_mapping.side_effect = get_item_mapping
    return provider


def _load(name: str) -> JsonApiDocument:
    with open(FIXTURES_DIR / name) as f:
        return JsonApiDocument(json.load(f))


def test_parse_track(provider_mock: Mock) -> None:
    """Test parsing a track resource with resolved artists, album and image."""
    doc = _load("track.json")
    track = parse_track(provider_mock, doc, doc.data)

    assert track.item_id == "58756128"
    assert track.name == "7 Years"
    assert track.duration == 237  # PT3M57S
    assert (ExternalID.ISRC, "USWB11506516") in track.external_ids
    assert track.metadata.popularity == 77  # 0.7728 scaled to 0..100
    assert track.audio_metadata is not None
    assert track.audio_metadata.bpm == 120.0
    assert track.audio_metadata.musical_key == "Eb major"
    assert [a.name for a in track.artists] == ["Lukas Graham"]
    assert track.album is not None
    assert track.album.item_id == "58756127"
    assert track.metadata.images
    assert track.metadata.images[0].type == ImageType.THUMB
    assert "750x750" in track.metadata.images[0].path
    assert track.metadata.genres == {"Pop"}
    mapping = next(iter(track.provider_mappings))
    assert mapping.available is True
    assert mapping.audio_format.bit_depth == 16  # LOSSLESS, not hi-res


def test_parse_album(provider_mock: Mock) -> None:
    """Test parsing an album resource with resolved artists and cover art."""
    doc = _load("album.json")
    album = parse_album(provider_mock, doc, doc.data)

    assert album.item_id == "58756127"
    # explicit version attribute is trusted and stripped from the title
    assert album.name == "Lukas Graham (Blue Album)"
    assert album.version == "International Version"
    assert album.album_type == AlbumType.ALBUM
    assert album.year == 2015
    assert (ExternalID.BARCODE, "00602547852748") in album.external_ids
    assert [a.name for a in album.artists] == ["Lukas Graham"]
    assert album.metadata.images
    assert "750x750" in album.metadata.images[0].path


def test_parse_artist(provider_mock: Mock) -> None:
    """Test parsing an artist resource with resolved profile art."""
    doc = _load("artist.json")
    artist = parse_artist(provider_mock, doc, doc.data)

    assert artist.item_id == "4184211"
    assert artist.name == "Lukas Graham"
    assert artist.metadata.popularity is not None
    assert artist.metadata.images
    assert artist.metadata.description  # from the biography include
    link_types = {link.type for link in (artist.metadata.links or [])}
    assert LinkType.FACEBOOK in link_types
    assert LinkType.TWITTER in link_types


def test_parse_artist_strips_wimplink_markup(provider_mock: Mock) -> None:
    """Test Tidal's internal [wimpLink] bio markup is stripped from the description."""
    doc = _load("artist_wimplink.json")
    artist = parse_artist(provider_mock, doc, doc.data)

    assert artist.metadata.description
    assert "wimpLink" not in artist.metadata.description
    assert "[" not in artist.metadata.description.split("~")[0]
    # inner link text is preserved
    assert "Asian Dub Foundation" in artist.metadata.description


@pytest.mark.parametrize(
    ("title", "version", "expected_name", "expected_version"),
    [
        (
            "Lukas Graham (Blue Album) (International Version)",
            "International Version",
            "Lukas Graham (Blue Album)",
            "International Version",
        ),
        ("Song (Remastered)", "Remastered", "Song", "Remastered"),
        ("Song - Live", "Live", "Song", "Live"),
        ("Plain Title", "Deluxe", "Plain Title", "Deluxe"),  # version not in title
        ("7 Years", None, "7 Years", ""),  # no explicit version -> heuristic fallback
    ],
)
def test_split_title_version(
    title: str, version: str | None, expected_name: str, expected_version: str
) -> None:
    """Test the title/version split trusts the explicit version attribute."""
    assert _split_title_version(title, version) == (expected_name, expected_version)


def test_parse_track_no_includes(provider_mock: Mock) -> None:
    """Test a track with unresolved relationships still parses core fields."""
    doc = JsonApiDocument(
        {
            "data": {
                "id": "1",
                "type": "tracks",
                "attributes": {
                    "title": "Bare Track",
                    "duration": "PT2M3S",
                    "explicit": False,
                    "isrc": "AAA000000000",
                    "popularity": 0.5,
                    "mediaTags": ["HIRES_LOSSLESS"],
                },
                "relationships": {},
            }
        }
    )
    track = parse_track(provider_mock, doc, doc.data)
    assert track.name == "Bare Track"
    assert track.duration == 123
    assert track.artists == []
    assert track.album is None
    assert next(iter(track.provider_mappings)).audio_format.bit_depth == 24


def test_parse_album_genres(provider_mock: Mock) -> None:
    """Test album genres are resolved from the genres relationship."""
    doc = JsonApiDocument(
        {
            "data": {
                "id": "1",
                "type": "albums",
                "attributes": {"title": "A", "albumType": "ALBUM", "explicit": False},
                "relationships": {"genres": {"data": [{"id": "g1", "type": "genres"}]}},
            },
            "included": [{"id": "g1", "type": "genres", "attributes": {"genreName": "Rock"}}],
        }
    )
    album = parse_album(provider_mock, doc, doc.data)
    assert album.metadata.genres == {"Rock"}


@pytest.mark.parametrize(
    ("album_type", "various_artists", "expected"),
    [
        ("ALBUM", True, AlbumType.COMPILATION),  # various artists overrides
        ("ALBUM", False, AlbumType.ALBUM),
        ("EP", False, AlbumType.EP),
        ("SINGLE", False, AlbumType.SINGLE),
        (None, False, AlbumType.ALBUM),  # unknown -> default
    ],
)
def test_map_album_type(album_type: str | None, various_artists: bool, expected: AlbumType) -> None:
    """Test album type mapping across the official albumType values."""
    assert _map_album_type(album_type, "Some Album", None, various_artists) == expected


@pytest.mark.parametrize(
    ("widths", "expected_width"),
    [
        ([80, 160, 320, 640, 750, 1080, 1280], 750),  # exact preferred width
        ([800, 1280], 800),  # none at 750 -> smallest at/above
        ([80, 160, 320], 320),  # all below -> largest available
    ],
)
def test_select_image_url(widths: list[int], expected_width: int) -> None:
    """Test artwork selection prefers the smallest file at/above the target width."""
    files = [{"href": f"http://img/{w}.jpg", "meta": {"width": w}} for w in widths]
    assert _select_image_url(files) == f"http://img/{expected_width}.jpg"


def test_select_image_url_empty() -> None:
    """Test artwork selection returns None when there are no usable files."""
    assert _select_image_url([]) is None
    assert _select_image_url([{"meta": {"width": 750}}]) is None  # no href


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("PT1H2M3S", 3723),
        ("PT3M57S", 237),
        ("PT45S", 45),
        ("", 0),
        ("garbage", 0),
    ],
)
def test_parse_iso_duration(value: str, expected: int) -> None:
    """Test ISO-8601 duration parsing including the hours component."""
    assert _parse_iso_duration(value) == expected
