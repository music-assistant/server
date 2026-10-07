"""Tests for Emby response parsers."""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock

import pytest
from music_assistant_models.enums import ImageType

from music_assistant.providers.emby.parsers import (
    parse_album,
    parse_artist,
    parse_playlist,
    parse_track,
)

INSTANCE_ID = "emby--test123"
BASE_URL = "http://127.0.0.1:8096/"


@pytest.fixture
def provider() -> Mock:
    """Create a stub Emby provider for the parsers."""
    prov = Mock()
    prov.domain = "emby"
    prov._base_url = BASE_URL
    return prov


def _image_path(item: Any) -> str | None:
    images = item.metadata.images or []
    thumb = next((x for x in images if x.type == ImageType.THUMB), None)
    return thumb.path if thumb else None


def test_track_image_url_carries_tag(provider: Mock) -> None:
    """The track image URL must carry the image tag for ValidateImageTags servers."""
    item: dict[str, Any] = {
        "Id": "track-1",
        "Name": "Track",
        "AlbumId": "album-1",
        "Album": "Album",
        "RunTimeTicks": 180 * 10000000,
        "ImageTags": {"Primary": "a1b2c3"},
        "MediaStreams": [{"Type": "Audio", "Codec": "flac"}],
    }
    track = parse_track(INSTANCE_ID, provider, item)
    assert _image_path(track) == (f"{BASE_URL}Items/track-1/Images/Primary?tag=a1b2c3")


def test_track_without_image_tag_has_no_image(provider: Mock) -> None:
    """A track without a Primary image tag must not get an image."""
    item: dict[str, Any] = {
        "Id": "track-1",
        "Name": "Track",
        "AlbumId": "album-1",
        "Album": "Album",
        "RunTimeTicks": 180 * 10000000,
        "MediaStreams": [{"Type": "Audio", "Codec": "flac"}],
    }
    track = parse_track(INSTANCE_ID, provider, item)
    assert _image_path(track) is None


def test_artist_image_url_carries_tag(provider: Mock) -> None:
    """The artist image URL must carry the image tag."""
    item: dict[str, Any] = {
        "Id": "artist-1",
        "Name": "Artist",
        "ImageTags": {"Primary": "art-tag"},
    }
    artist = parse_artist(INSTANCE_ID, provider, item)
    assert _image_path(artist) == (f"{BASE_URL}Items/artist-1/Images/Primary?tag=art-tag")


def test_album_image_prefers_primary_image_tag(provider: Mock) -> None:
    """An album borrowing its parent's image must use PrimaryImageTag on that item."""
    item: dict[str, Any] = {
        "Id": "album-1",
        "Name": "Album",
        "ImageTags": {},
        "PrimaryImageItemId": "parent-album-1",
        "PrimaryImageTag": "album-tag",
    }
    album = parse_album(INSTANCE_ID, provider, item)
    assert _image_path(album) == (f"{BASE_URL}Items/parent-album-1/Images/Primary?tag=album-tag")


def test_album_image_falls_back_to_own_tag(provider: Mock) -> None:
    """Without PrimaryImageTag the album's own ImageTags entry is used."""
    item: dict[str, Any] = {
        "Id": "album-1",
        "Name": "Album",
        "ImageTags": {"Primary": "own-tag"},
    }
    album = parse_album(INSTANCE_ID, provider, item)
    assert _image_path(album) == (f"{BASE_URL}Items/album-1/Images/Primary?tag=own-tag")


def test_album_fallback_tag_pairs_with_own_item_id(provider: Mock) -> None:
    """A parent image id without the parent tag must not pair it with the album's own tag."""
    item: dict[str, Any] = {
        "Id": "album-1",
        "Name": "Album",
        "ImageTags": {"Primary": "own-tag"},
        "PrimaryImageItemId": "parent-album-1",
    }
    album = parse_album(INSTANCE_ID, provider, item)
    assert _image_path(album) == (f"{BASE_URL}Items/album-1/Images/Primary?tag=own-tag")


def test_album_without_tag_has_no_image(provider: Mock) -> None:
    """An album id without a tag must not get an image (it would 404)."""
    item: dict[str, Any] = {
        "Id": "album-1",
        "Name": "Album",
        "ImageTags": {},
        "PrimaryImageItemId": "album-1",
    }
    album = parse_album(INSTANCE_ID, provider, item)
    assert _image_path(album) is None


def test_playlist_image_url_carries_tag(provider: Mock) -> None:
    """The playlist image URL must carry the image tag."""
    item: dict[str, Any] = {
        "Id": "playlist-1",
        "Name": "Playlist",
        "ImageTags": {"Primary": "pl-tag"},
    }
    playlist = parse_playlist(INSTANCE_ID, provider, item)
    assert _image_path(playlist) == (f"{BASE_URL}Items/playlist-1/Images/Primary?tag=pl-tag")
