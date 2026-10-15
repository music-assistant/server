"""Exercise MSX library routes against the real Music Assistant listing contracts."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

from music_assistant.controllers.music.media.albums import AlbumsController
from music_assistant.controllers.music.media.artists import ArtistsController
from music_assistant.controllers.music.media.playlists import PlaylistController
from music_assistant.providers.msx_bridge.mappers import get_album_image_fallback

from .factories import album as make_album
from .factories import track as make_track

if TYPE_CHECKING:
    from aiohttp.test_utils import TestClient

    from music_assistant.providers.msx_bridge.provider import MSXBridgeProvider


@pytest.mark.parametrize(
    "route",
    [
        "/msx/playlists/playlist-1/tracks.json",
        "/api/playlists/playlist-1/tracks",
    ],
)
async def test_playlist_routes_walk_real_controller_pages(
    http_client: TestClient[Any, Any], mass_mock: Mock, route: str
) -> None:
    """Every route retains the last item across multiple provider pages."""
    tracks = [make_track(str(index), f"Track {index}") for index in range(501)]
    controller = PlaylistController(Mock())
    mass_mock.music.playlists = controller
    with (
        patch.object(controller, "_playlist_source", return_value=("playlist-1", "source", None)),
        patch.object(
            controller,
            "_get_provider_playlist_tracks",
            side_effect=[tracks[:500], tracks[500:], []],
        ) as fetch_page,
        patch.object(controller, "_add_audio_metadata"),
    ):
        response = await http_client.get(route)
        assert response.status == 200
        data = await response.json()

    assert len(data["items"]) == 501
    assert [call.kwargs["page"] for call in fetch_page.await_args_list] == [0, 1, 2]


@pytest.mark.parametrize(
    "route",
    [
        "/msx/albums/album-1/tracks.json",
        "/api/albums/album-1/tracks",
    ],
)
async def test_album_routes_keep_items_after_default_listing_limit(
    http_client: TestClient[Any, Any], mass_mock: Mock, route: str
) -> None:
    """The real controller must not truncate an album to its default 500 tracks."""
    tracks = [
        make_track(str(index), f"Track {index}", track_number=index + 1) for index in range(501)
    ]
    controller = AlbumsController(Mock())
    mass_mock.music.albums = controller
    with (
        patch.object(controller, "get_library_item_by_prov_id", return_value=make_album()),
        patch.object(controller, "_ensure_provider_filter", return_value=None),
        patch(
            "music_assistant.controllers.music.media.albums.cached_listing",
            new=AsyncMock(return_value=tracks),
        ),
    ):
        response = await http_client.get(route)
        assert response.status == 200
        data = await response.json()

    assert len(data["items"]) == 501


@pytest.mark.parametrize(
    "route", ["/msx/artists/artist-1/albums.json", "/api/artists/artist-1/albums"]
)
async def test_artist_routes_keep_items_after_default_listing_limit(
    http_client: TestClient[Any, Any], mass_mock: Mock, route: str
) -> None:
    """The real controller must not truncate an artist to its default 500 albums."""
    albums = [make_album(str(index), f"Album {index}", image_path="cover") for index in range(501)]
    controller = ArtistsController(Mock())
    mass_mock.music.artists = controller
    with patch.object(controller, "get_library_artist_albums", return_value=albums):
        response = await http_client.get(route)
        assert response.status == 200
        data = await response.json()

    assert len(data["items"]) == 501


async def test_album_cover_fallback_searches_after_default_listing_limit(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """A cover available only beyond the first page must still be found."""
    tracks = [make_track(str(index)) for index in range(500)]
    tracks.append(make_track("last", image_path="last-cover"))
    controller = AlbumsController(Mock())
    mass_mock.music.albums = controller
    mass_mock.metadata.get_image_url.return_value = "http://ma.local/last-cover"
    with (
        patch.object(controller, "get_library_item_by_prov_id", return_value=make_album()),
        patch.object(controller, "_ensure_provider_filter", return_value=None),
        patch(
            "music_assistant.controllers.music.media.albums.cached_listing",
            new=AsyncMock(return_value=tracks),
        ),
    ):
        assert (
            await get_album_image_fallback(make_album(), provider) == "http://ma.local/last-cover"
        )
