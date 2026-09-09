"""Tests for MSX mappers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ImageType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    Album,
    Artist,
    MediaItemImage,
    MediaItemMetadata,
    Track,
    UniqueList,
)

from music_assistant.providers.msx_bridge.mappers import (
    get_album_image_fallback,
    map_album_to_msx,
    map_artist_to_msx,
    map_track_to_msx,
    msx_list_page,
    sort_album_tracks,
)
from music_assistant.providers.msx_bridge.provider import MSXBridgeProvider


def _mock_provider() -> MSXBridgeProvider:
    """Create a mock provider."""
    provider = MagicMock()
    provider.mass.metadata.get_image_url.return_value = "http://image.url"
    provider.get_stream_token.return_value = "tok123"
    return provider


def _artist() -> Artist:
    """Create an artist used by test media items."""
    return Artist(
        item_id="artist-1",
        provider="library",
        name="Test Artist",
        provider_mappings=set(),
    )


def _track(
    item_id: str = "1",
    name: str = "Test Track",
    disc_number: int = 0,
    track_number: int = 0,
) -> Track:
    """Create a track with the media fields used by mapper tests."""
    image = MediaItemImage(type=ImageType.THUMB, path="some_image", provider="library")
    return Track(
        item_id=item_id,
        provider="library",
        name=name,
        provider_mappings=set(),
        metadata=MediaItemMetadata(images=UniqueList([image])),
        duration=125,
        artists=UniqueList([_artist()]),
        disc_number=disc_number,
        track_number=track_number,
    )


def _album() -> Album:
    """Create an album with the media fields used by mapper tests."""
    image = MediaItemImage(type=ImageType.THUMB, path="album_image", provider="library")
    return Album(
        item_id="1",
        provider="library",
        name="Test Album",
        provider_mappings=set(),
        metadata=MediaItemMetadata(images=UniqueList([image])),
        artists=UniqueList([_artist()]),
    )


def test_map_track_to_msx() -> None:
    """Test mapping a track to MSX item."""
    prov = _mock_provider()
    track = _track()
    assert track.uri is not None

    item = map_track_to_msx(
        track=track,
        prefix="http://localhost",
        player_id="msx_123",
        provider=prov,
        device_param="device_id=abc",
        context_uri=track.uri,
    )

    assert item.title_header == "{txt:msx-white:Test Track}"
    assert item.title_footer == "Test Artist · 2:05"
    assert item.image == "http://image.url"
    assert item.action is not None
    assert item.action.startswith("execute:http://localhost/api/play-context/msx_123")
    assert "uri=library%3A%2F%2Ftrack%2F1" in item.action
    assert "device_id=abc" in item.action
    assert item.properties is not None
    assert item.properties["trigger:complete"] == "execute:http://localhost/api/next/msx_123"


def test_map_track_to_msx_play_context() -> None:
    """Album/playlist clicks must enqueue the container into the MA queue."""
    prov = _mock_provider()
    track = _track()

    item = map_track_to_msx(
        track=track,
        prefix="http://localhost",
        player_id="msx_123",
        provider=prov,
        device_param="device_id=abc",
        context_uri="library://album/9",
        context_start=3,
    )

    assert item.action is not None
    assert item.action.startswith("execute:http://localhost/api/play-context/msx_123")
    assert "uri=library%3A%2F%2Falbum%2F9" in item.action
    assert "start=3" in item.action
    assert "track=library%3A%2F%2Ftrack%2F1" in item.action
    assert "device_id=abc" in item.action


@pytest.mark.asyncio
async def test_map_album_to_msx() -> None:
    """Test mapping an album to MSX item."""
    prov = _mock_provider()
    album = _album()

    item = await map_album_to_msx(
        album=album,
        prefix="http://localhost",
        provider=prov,
        device_param="device_id=abc",
    )

    assert item.title == "Test Album"
    # Mock has no year attribute set, so footer is "Artist · year" only if year exists
    assert "Test Artist" in (item.title_footer or "")
    assert item.image == "http://image.url"
    assert (
        item.action
        == "content:http://localhost/msx/albums/1/tracks.json?provider=library&device_id=abc"
    )


def test_map_artist_to_msx_preserves_provider() -> None:
    """Artist actions must retain the provider used for provider search results."""
    artist = Artist(
        item_id="5531642",
        provider="yandex_music--test",
        name="Test Artist",
        provider_mappings=set(),
    )

    item = map_artist_to_msx(
        artist=artist,
        prefix="http://localhost",
        provider=_mock_provider(),
        device_param="device_id=abc",
    )

    assert (
        item.action == "content:http://localhost/msx/artists/5531642/albums.json?"
        "provider=yandex_music--test&device_id=abc"
    )


def test_sort_album_tracks_uses_name_as_tiebreaker() -> None:
    """Display and playlist pages must agree when disc/track numbers collide."""
    early = _track(item_id="a", name="A", disc_number=1, track_number=1)
    late = _track(item_id="b", name="B", disc_number=1, track_number=1)
    assert [t.name for t in sort_album_tracks([late, early])] == ["A", "B"]


def test_sort_album_tracks_uses_uri_as_final_tiebreaker() -> None:
    """Tracks with identical numbering and names still sort deterministically."""
    early = _track(item_id="a", name="Same", disc_number=1, track_number=1)
    late = _track(item_id="b", name="Same", disc_number=1, track_number=1)

    assert sort_album_tracks([late, early]) == [early, late]


@pytest.mark.asyncio
async def test_album_image_fallback_returns_none_on_music_assistant_error() -> None:
    """A missing album from MA must not fail the album list page."""
    prov = _mock_provider()
    album = _album()
    prov.mass.music.albums.tracks = AsyncMock(  # type: ignore[method-assign]
        side_effect=MediaNotFoundError("gone")
    )

    assert await get_album_image_fallback(album, prov) is None


@pytest.mark.asyncio
async def test_album_image_fallback_does_not_swallow_unexpected_error() -> None:
    """Programming errors while fetching album art must not be hidden."""
    prov = _mock_provider()
    album = _album()
    prov.mass.music.albums.tracks = AsyncMock(side_effect=ValueError("bug"))  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="bug"):
        await get_album_image_fallback(album, prov)


def test_msx_list_page_uses_empty_title() -> None:
    """A list page with no items still has one placeholder item."""
    page = msx_list_page("Albums", [], empty_title="No albums found", layout="0,0,3,4")
    assert page.headline == "Albums"
    assert page.items is not None
    assert page.items[0].title == "No albums found"
