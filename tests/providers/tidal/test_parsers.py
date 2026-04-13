"""Test we can parse Tidal models into Music Assistant models."""

import json
import pathlib
from unittest.mock import Mock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import ItemMapping
from syrupy.assertion import SnapshotAssertion

from music_assistant.providers.tidal.parsers import (
    parse_album,
    parse_artist,
    parse_playlist,
    parse_track,
)

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"
ARTIST_FIXTURES = list(FIXTURES_DIR.glob("artists/*.json"))
ALBUM_FIXTURES = list(FIXTURES_DIR.glob("albums/*.json"))
TRACK_FIXTURES = list(FIXTURES_DIR.glob("tracks/*.json"))
PLAYLIST_FIXTURES = list(FIXTURES_DIR.glob("playlists/*.json"))


@pytest.fixture
def provider_mock() -> Mock:
    """Return a mock provider."""
    provider = Mock()
    provider.domain = "tidal"
    provider.instance_id = "tidal_instance"
    provider.auth.user_id = "12345"
    provider.auth.user.profile_name = "Test User"
    provider.auth.user.user_name = "Test User"

    def get_item_mapping(media_type: MediaType, key: str, name: str) -> ItemMapping:
        return ItemMapping(
            media_type=media_type,
            item_id=key,
            provider=provider.instance_id,
            name=name,
        )

    provider.get_item_mapping.side_effect = get_item_mapping

    return provider


@pytest.mark.parametrize("example", ARTIST_FIXTURES, ids=lambda val: str(val.stem))
def test_parse_artist(
    example: pathlib.Path, provider_mock: Mock, snapshot: SnapshotAssertion
) -> None:
    """Test we can parse artists."""
    with open(example) as f:
        data = json.load(f)
    parsed = parse_artist(provider_mock, data).to_dict()
    assert snapshot == parsed


@pytest.mark.parametrize("example", ALBUM_FIXTURES, ids=lambda val: str(val.stem))
def test_parse_album(
    example: pathlib.Path, provider_mock: Mock, snapshot: SnapshotAssertion
) -> None:
    """Test we can parse albums."""
    with open(example) as f:
        data = json.load(f)
    parsed = parse_album(provider_mock, data).to_dict()
    assert snapshot == parsed


@pytest.mark.parametrize("example", TRACK_FIXTURES, ids=lambda val: str(val.stem))
def test_parse_track(
    example: pathlib.Path, provider_mock: Mock, snapshot: SnapshotAssertion
) -> None:
    """Test we can parse tracks."""
    with open(example) as f:
        data = json.load(f)
    parsed = parse_track(provider_mock, data).to_dict()
    assert snapshot == parsed


@pytest.mark.parametrize("example", PLAYLIST_FIXTURES, ids=lambda val: str(val.stem))
def test_parse_playlist(
    example: pathlib.Path, provider_mock: Mock, snapshot: SnapshotAssertion
) -> None:
    """Test we can parse playlists."""
    with open(example) as f:
        data = json.load(f)

    is_mix = "mix" in example.name
    parsed = parse_playlist(provider_mock, data, is_mix=is_mix).to_dict()
    assert snapshot == parsed
