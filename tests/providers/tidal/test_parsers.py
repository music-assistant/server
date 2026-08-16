"""Test we can parse Tidal models into Music Assistant models."""

import json
import pathlib
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from music_assistant.providers.tidal.parsers import (
    parse_album,
    parse_artist,
    parse_playlist,
    parse_track,
)

if TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"
ARTIST_FIXTURES = list(FIXTURES_DIR.glob("artists/*.json"))
ALBUM_FIXTURES = list(FIXTURES_DIR.glob("albums/*.json"))
TRACK_FIXTURES = list(FIXTURES_DIR.glob("tracks/*.json"))
PLAYLIST_FIXTURES = list(FIXTURES_DIR.glob("playlists/*.json"))


@pytest.mark.parametrize("example", ARTIST_FIXTURES, ids=lambda val: str(val.stem))
def test_parse_artist(
    example: pathlib.Path, provider_mock: Mock, snapshot: SnapshotAssertion
) -> None:
    """Test we can parse artists."""
    with open(example, encoding="utf-8") as f:
        data = json.load(f)
    parsed = parse_artist(provider_mock, data).to_dict()
    assert snapshot == parsed


@pytest.mark.parametrize("example", ALBUM_FIXTURES, ids=lambda val: str(val.stem))
def test_parse_album(
    example: pathlib.Path, provider_mock: Mock, snapshot: SnapshotAssertion
) -> None:
    """Test we can parse albums."""
    with open(example, encoding="utf-8") as f:
        data = json.load(f)
    parsed = parse_album(provider_mock, data).to_dict()
    assert snapshot == parsed


@pytest.mark.parametrize("example", TRACK_FIXTURES, ids=lambda val: str(val.stem))
def test_parse_track(
    example: pathlib.Path, provider_mock: Mock, snapshot: SnapshotAssertion
) -> None:
    """Test we can parse tracks."""
    with open(example, encoding="utf-8") as f:
        data = json.load(f)
    parsed = parse_track(provider_mock, data).to_dict()
    assert snapshot == parsed


@pytest.mark.parametrize("example", PLAYLIST_FIXTURES, ids=lambda val: str(val.stem))
def test_parse_playlist(
    example: pathlib.Path, provider_mock: Mock, snapshot: SnapshotAssertion
) -> None:
    """Test we can parse playlists."""
    with open(example, encoding="utf-8") as f:
        data = json.load(f)

    is_mix = "mix" in example.name
    parsed = parse_playlist(provider_mock, data, is_mix=is_mix).to_dict()
    assert snapshot == parsed


def test_parse_track_partial_album(provider_mock: Mock) -> None:
    """Test track parsing tolerates partial album objects."""
    track_obj = {
        "id": 123,
        "title": "Test Track",
        "duration": 100,
        "artists": [{"id": 1, "name": "Test Artist", "picture": None}],
        "album": {"id": 456},  # No title or cover
    }

    track = parse_track(provider_mock, track_obj)

    assert track.album is not None
    assert track.album.item_id == "456"
    assert track.album.name == ""

    # An album without an id is useless: no mapping should be created
    track_obj["album"] = {"title": "Test Album"}
    track = parse_track(provider_mock, track_obj)
    assert track.album is None
