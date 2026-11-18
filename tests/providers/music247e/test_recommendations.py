"""Test the shared 24-7 (247e) music two-method recommendations contract."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.media_items import Album, ItemMapping, Track, UniqueList

from music_assistant.providers.music247e.provider import Music247eProvider
from music_assistant.providers.music247e.recommendations import Music247eRecommendationsManager

if TYPE_CHECKING:
    from typing import Any

GRAPHQL_RESULT = {
    "data": {
        "me": {
            "recommendations": {
                "albumRecommendations": {
                    "id": "discoveralbums",
                    "title": "Discover albums",
                    "subtitle": "Albums picked for you",
                    "albums": {
                        "items": [
                            {
                                "id": "album1",
                                "title": "Album One",
                                "artist": {"id": "artist1", "title": "Artist One"},
                                "featuredArtists": {
                                    "items": [{"id": "artist2", "title": "Artist Two"}]
                                },
                            }
                        ]
                    },
                },
                "trackRecommendations": {
                    "id": "discovertracks",
                    "title": "Discover tracks",
                    "subtitle": "Tracks picked for you",
                    "tracks": {
                        "items": [
                            {
                                "id": "track1",
                                "title": "Track One",
                                "duration": 200,
                                "availableToStream": False,
                                "artist": {"id": "artist1", "title": "Artist One"},
                                "featuredArtists": {
                                    "items": [{"id": "artist2", "title": "Artist Two"}]
                                },
                                "album": {
                                    "id": "album1",
                                    "title": "Album One",
                                    "artist": {"id": "artist1", "title": "Artist One"},
                                    "available": False,
                                    "releaseDate": "2020-01-01",
                                    "cover": "https://example.invalid/album.jpg",
                                },
                            }
                        ]
                    },
                },
                "weeklyDiscoveries": {
                    "id": "weeklyDiscoveries",
                    "title": "Weekly discoveries",
                    "subtitle": "Fresh finds",
                    "tracks": {
                        "items": [
                            {
                                "id": "track2",
                                "title": "Track Two",
                                "duration": 180,
                                "availableToStream": False,
                                "artist": {"id": "artist2", "title": "Artist Two"},
                            }
                        ]
                    },
                },
            }
        }
    }
}


def _stub_api(provider: Music247eProvider) -> Mock:
    """Attach a stubbed API client and a real recommendations manager to the provider."""
    api = Mock()
    api.post_graphql = AsyncMock(return_value=GRAPHQL_RESULT)
    provider.api = api
    provider.auth = Mock()
    provider.recommendations_manager = Music247eRecommendationsManager(provider)
    return api


@pytest.mark.asyncio
async def test_get_recommendations_returns_rows_without_items(
    provider: Music247eProvider,
) -> None:
    """get_recommendations returns the payload's rows, stripped of items."""
    api = _stub_api(provider)

    rows = await provider.get_recommendations()

    api.post_graphql.assert_awaited_once()
    assert [row.item_id for row in rows] == [
        "discoveralbums",
        "discovertracks",
        "weeklyDiscoveries",
    ]
    assert [row.name for row in rows] == [
        "Discover albums",
        "Discover tracks",
        "Weekly discoveries",
    ]
    assert [row.subtitle for row in rows] == [
        "Albums picked for you",
        "Tracks picked for you",
        "Fresh finds",
    ]
    assert all(row.provider == provider.instance_id for row in rows)
    assert all(not row.items for row in rows)


@pytest.mark.asyncio
async def test_rows_and_items_share_one_payload_fetch(
    provider: Music247eProvider,
    background_tasks: list[asyncio.Future[Any]],
) -> None:
    """The rows call and subsequent per-row items calls trigger exactly one GraphQL fetch."""
    api = _stub_api(provider)

    rows = await provider.get_recommendations()
    await asyncio.gather(*background_tasks)
    album_items = await provider.get_recommendation_items("discoveralbums")
    track_items = await provider.get_recommendation_items("discovertracks")

    api.post_graphql.assert_awaited_once()
    assert len(rows) == 3
    assert [item.item_id for item in album_items] == ["album1"]
    assert [item.name for item in album_items] == ["Album One"]
    assert [item.item_id for item in track_items] == ["track1"]
    assert [item.name for item in track_items] == ["Track One"]
    album = album_items[0]
    assert isinstance(album, Album)
    assert all(isinstance(artist, ItemMapping) for artist in album.artists)
    assert [artist.item_id for artist in album.artists] == ["artist1", "artist2"]
    track = track_items[0]
    assert isinstance(track, Track)
    assert all(isinstance(artist, ItemMapping) for artist in track.artists)
    assert [artist.item_id for artist in track.artists] == ["artist1", "artist2"]
    assert isinstance(track.album, ItemMapping)
    assert track.album.item_id == "album1"
    assert track.album.provider == provider.instance_id
    assert track.album.name == "Album One"
    assert track.album.available is False
    assert track.album.year == 2020
    assert track.album.image is not None
    assert track.album.image.path == "https://example.invalid/album.jpg"
    serialized_track = track.to_dict()
    assert "artists" not in serialized_track["album"]
    assert "provider_mappings" not in serialized_track["artists"][0]
    assert next(iter(track.provider_mappings)).available is False
    assert "availableToStream" in api.post_graphql.call_args.args[0]


@pytest.mark.asyncio
async def test_get_recommendation_items_returns_row_items(
    provider: Music247eProvider,
) -> None:
    """A cold items call fetches the payload and returns only the requested row's items."""
    api = _stub_api(provider)

    items = await provider.get_recommendation_items("weeklyDiscoveries")

    api.post_graphql.assert_awaited_once()
    assert [item.item_id for item in items] == ["track2"]
    assert [item.name for item in items] == ["Track Two"]
    track = items[0]
    assert isinstance(track, Track)
    assert next(iter(track.provider_mappings)).available is False
    assert "availableToStream" in api.post_graphql.call_args.args[0]


@pytest.mark.asyncio
async def test_get_recommendation_items_unknown_id_returns_empty(
    provider: Music247eProvider,
) -> None:
    """An unknown row item_id yields an empty UniqueList."""
    _stub_api(provider)

    result = await provider.get_recommendation_items("bogus_row")

    assert isinstance(result, UniqueList)
    assert len(result) == 0
