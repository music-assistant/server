"""Unit tests for the Apple Music request counters and provider diagnostics."""

from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import MediaType

from music_assistant.providers.apple_music.helpers.request_counter import (
    RequestCounter,
    endpoint_family,
)
from music_assistant.providers.apple_music.provider import AppleMusicProvider


@pytest.mark.parametrize(
    ("endpoint", "params", "family"),
    [
        ("me/library/songs", {"limit": 100, "offset": 0}, "me/library/songs"),
        ("catalog/us/songs/1440857781", {}, "catalog/songs/{id}"),
        ("catalog/nl/songs", {"ids": "1,2"}, "catalog/songs?ids"),
        ("catalog/us/search", {"term": "my secret query"}, "catalog/search"),
        ("me/ratings/songs", {"ids": "1,2"}, "me/ratings/songs?ids"),
        ("me/ratings/library-songs/i.AbC123", {}, "me/ratings/library-songs/{id}"),
        ("me/stations/next-tracks/ra.1440857781", {}, "me/stations/next-tracks/{id}"),
        ("me/recommendations?include[personal-recommendation]=contents", {}, "me/recommendations"),
        ("me/library/albums/l.PnamISl/tracks", {}, "me/library/albums/{id}/tracks"),
        ("catalog/us/artists/136975/view/top-songs", {}, "catalog/artists/{id}/view/top-songs"),
    ],
)
def test_endpoint_family_strips_ids_storefront_and_query(
    endpoint: str, params: dict[str, Any], family: str
) -> None:
    """The endpoint family carries no ids, storefront or search terms."""
    assert endpoint_family(endpoint, params) == family


def test_request_counter_since_returns_only_new_counts() -> None:
    """since() reports what was counted after the snapshot, dropping untouched families."""
    counter = RequestCounter()
    counter.record("me/library/songs", "attempts")
    counter.record("catalog/search", "attempts")
    before = counter.as_dict()
    counter.record("me/library/songs", "attempts")
    counter.record("me/library/songs", "rate_limited")

    assert counter.since(before) == {"me/library/songs": {"attempts": 1, "rate_limited": 1}}
    assert counter.as_dict()["me/library/songs"] == {"attempts": 2, "rate_limited": 1}


@pytest.mark.asyncio
async def test_library_listing_snapshot_lands_in_diagnostics() -> None:
    """A library listing records its item count and the requests it made for the diagnostics."""
    provider = MagicMock()
    provider.api_client.request_counter = counter = RequestCounter()
    provider._last_library_listings = {}
    counter.record("catalog/search", "attempts")

    async def _items() -> AsyncGenerator[str]:
        counter.record("me/library/songs", "attempts")
        yield "a"
        counter.record("me/ratings/songs?ids", "attempts")
        yield "b"

    synced = [
        item
        async for item in AppleMusicProvider._counted_listing(provider, MediaType.TRACK, _items())
    ]
    diagnostics = await AppleMusicProvider.get_diagnostics(provider)

    assert synced == ["a", "b"]
    track_listing = provider._last_library_listings["track"]
    assert track_listing["items"] == 2
    assert track_listing["completed"] is True
    assert track_listing["requests"] == {
        "me/library/songs": {"attempts": 1},
        "me/ratings/songs?ids": {"attempts": 1},
    }
    assert diagnostics == {
        "requests_since": counter.started_at,
        "requests": {
            "catalog/search": {"attempts": 1},
            "me/library/songs": {"attempts": 1},
            "me/ratings/songs?ids": {"attempts": 1},
        },
        "last_library_listings": {"track": track_listing},
    }
