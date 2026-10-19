"""Tests for Emby response parsers."""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock

import pytest

from music_assistant.providers.emby.parsers import parse_album

INSTANCE_ID = "emby--test123"
BASE_URL = "http://127.0.0.1:8096/"


@pytest.fixture
def provider() -> Mock:
    """Create a stub Emby provider for the parsers."""
    prov = Mock()
    prov.domain = "emby"
    prov._base_url = BASE_URL
    return prov


def test_album_artists_prefer_album_artists_over_artist_items(provider: Mock) -> None:
    """Featured track artists in ArtistItems must not become album artists."""
    item: dict[str, Any] = {
        "Id": "album-1",
        "Name": "Album",
        "AlbumArtists": [{"Id": "artist-a", "Name": "Artist A"}],
        "ArtistItems": [
            {"Id": "artist-a", "Name": "Artist A"},
            {"Id": "artist-b", "Name": "Featured B"},
        ],
    }
    album = parse_album(INSTANCE_ID, provider, item)
    assert [artist.item_id for artist in album.artists] == ["artist-a"]


def test_album_artists_fall_back_to_artist_items(provider: Mock) -> None:
    """Without AlbumArtists the album artists come from ArtistItems."""
    item: dict[str, Any] = {
        "Id": "album-1",
        "Name": "Album",
        "ArtistItems": [{"Id": "artist-a", "Name": "Artist A"}],
    }
    album = parse_album(INSTANCE_ID, provider, item)
    assert [artist.item_id for artist in album.artists] == ["artist-a"]
