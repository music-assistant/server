"""Test authenticated discovery and release filtering with live response fixtures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import Album, ItemMapping

from music_assistant.providers.nugs import NugsProvider
from tests.common import use_real_create_task


def _fixture(name: str) -> dict[str, Any]:
    """Load a catalog response captured from Nugs."""
    data = json.loads((Path(__file__).parent / "fixtures" / f"{name}.json").read_text())
    assert isinstance(data, dict)
    return data


def _catalog(provider: NugsProvider, *pages: dict[str, Any]) -> AsyncMock:
    """Use real cache wrappers around a mocked authenticated transport."""
    provider.mass.cache.get_with_freshness = AsyncMock(return_value=(None, False, False))  # type: ignore[method-assign]
    provider.mass.cache.set = AsyncMock()  # type: ignore[method-assign]
    use_real_create_task(provider.mass)
    request = AsyncMock(side_effect=[_fixture("search_modern"), *pages])
    provider._get_data = request  # type: ignore[method-assign]
    return request


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("Billy Strings", {"artistIds": "1125"}),
        ("RED ROCKS", {"venueIds": "42,5363,164"}),
        (
            "Billy Strings at Red Rocks 2023",
            {"artistIds": "1125", "venueIds": "42,5363,164", "years": "2023"},
        ),
        (
            "Billy Strings Red Rocks 2023-05-11",
            {
                "artistIds": "1125",
                "venueIds": "42,5363,164",
                "years": "2023",
                "monthDayFrom": "05-11",
                "monthDayTo": "05-11",
            },
        ),
        ("2023-05-11", {"years": "2023", "monthDayFrom": "05-11", "monthDayTo": "05-11"}),
        ("2023", {"years": "2023"}),
        ("Billy Strings unknown venue", {}),
        ("Billy Strings Red", {"artistIds": "1125", "venueIds": "42,5363,164"}),
        ("2023-02-30", {}),
        ("", {}),
    ],
)
def test_explicit_filters(provider: NugsProvider, query: str, expected: dict[str, str]) -> None:
    """Only fully recognized query components become intersected filters."""
    assert provider._get_search_filters(query, _fixture("search_modern")["result"]) == expected


@pytest.mark.asyncio
async def test_artist_venue_year_intersection(provider: NugsProvider) -> None:
    """A combined query returns matching shows, with compact nested artists."""
    request = _catalog(provider, _fixture("releases_intersection"))
    result = await provider.search(
        "Billy Strings Red Rocks 2023", [MediaType.ALBUM, MediaType.ARTIST], 5
    )
    assert [item.item_id for item in result.albums] == ["32613", "32612"]
    assert [item.item_id for item in result.artists] == ["1125"]
    album = result.albums[0]
    assert isinstance(album, Album)
    assert album.year == 2023
    assert isinstance(album.artists[0], ItemMapping)
    assert album.artists[0].item_id == "1125"
    assert album.image is not None
    assert next(iter(album.provider_mappings)).provider_domain == "nugs"
    assert request.await_count == 2
    request.assert_any_await("catalog", "search", query="Billy Strings Red Rocks 2023")
    params = request.call_args.kwargs
    assert params["artistIds"] == "1125"
    assert params["venueIds"] == "42,5363,164"
    assert params["years"] == "2023"
    assert params["contentType"] == "audio"


@pytest.mark.asyncio
async def test_exact_date(provider: NugsProvider) -> None:
    """ISO date queries resolve to the date filter, not fuzzy date suggestions."""
    request = _catalog(provider, _fixture("releases_date_intersection"))
    result = await provider.search("Billy Strings Red Rocks 2023-05-11", [MediaType.ALBUM])
    assert [item.item_id for item in result.albums] == ["32612"]
    assert request.call_args.kwargs["monthDayFrom"] == "05-11"
    assert request.call_args.kwargs["monthDayTo"] == "05-11"


@pytest.mark.asyncio
async def test_filtered_release_pagination(provider: NugsProvider) -> None:
    """Duplicates and unavailable releases cannot consume the result limit."""
    first = _fixture("releases_page0")["items"][0]
    second = _fixture("releases_page1")["items"][0]
    pages = [
        {"items": [{**first, "status": "preorder"}, first], "total": 6},
        {"items": [first, {**second, "isInSubscriptionCatalog": False}], "total": 6},
        {"items": [second], "total": 6},
    ]
    request = _catalog(provider, *pages)
    result = await provider.search("Billy Strings", [MediaType.ALBUM], 2)
    assert [item.item_id for item in result.albums] == [first["id"], second["id"]]
    assert [call.kwargs["offset"] for call in request.call_args_list[1:]] == [0, 2, 4]


@pytest.mark.asyncio
async def test_album_title_discovery_has_no_per_hit_fetch(provider: NugsProvider) -> None:
    """Sparse album discoveries retain artwork and IDs without detail request fanout."""
    request = _catalog(provider)
    request.side_effect = None
    request.return_value = {
        "result": {
            "albums": [
                {
                    "id": 3602,
                    "title": "Billy Breathes",
                    "coverImage": "https://example.com/cover.jpg",
                },
                {"id": 3602, "title": "Billy Breathes"},
            ]
        }
    }
    result = await provider.search("Billy Breathes", [MediaType.ALBUM], 2)
    assert len(result.albums) == 1
    album = result.albums[0]
    assert isinstance(album, Album)
    assert album.item_id == "3602"
    assert album.name == "Billy Breathes"
    assert album.image is not None
    assert not album.artists
    request.assert_awaited_once()


@pytest.mark.asyncio
async def test_artist_only_does_not_expand_releases(provider: NugsProvider) -> None:
    """Artist discovery does not fetch releases or legacy song matches."""
    request = _catalog(provider)
    provider._get_search_data = AsyncMock()  # type: ignore[method-assign]
    result = await provider.search("Billy Strings", [MediaType.ARTIST], 1)
    assert [item.item_id for item in result.artists] == ["1125"]
    request.assert_awaited_once()
    provider._get_search_data.assert_not_awaited()


@pytest.mark.asyncio
async def test_discovery_cache(provider: NugsProvider) -> None:
    """Repeated discovery reuses the shared cache regardless of requested result types."""
    request = _catalog(provider)
    data = await provider._get_catalog_search("Billy Strings")
    provider.mass.cache.get_with_freshness = AsyncMock(return_value=(data, True, True))  # type: ignore[method-assign]
    assert await provider._get_catalog_search("Billy Strings") == data
    request.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, [], {}, {"result": []}])
async def test_invalid_discovery(provider: NugsProvider, payload: Any) -> None:
    """Malformed API envelopes must not become successful empty results."""
    request = _catalog(provider)
    request.side_effect = None
    request.return_value = payload
    with pytest.raises(InvalidDataError):
        await provider._get_catalog_search("Billy")
    assert isinstance(provider.mass.cache.set, Mock)
    provider.mass.cache.set.assert_not_called()


@pytest.mark.parametrize("query", ["Umphreys McGee", "Umphrey's McGee", "Umphrey\u2019s McGee"])
def test_core_apostrophe_normalization(provider: NugsProvider, query: str) -> None:
    """Core-sanitized artist and venue names still resolve exact filters."""
    data = {
        "artists": [{"id": 22, "name": "Umphrey's McGee"}],
        "venues": [{"id": 7, "name": "George\u2019s Majestic Lounge"}],
    }
    assert provider._get_search_filters(f"{query} Georges Majestic Lounge", data) == {
        "artistIds": "22",
        "venueIds": "7",
    }


@pytest.mark.asyncio
async def test_track_only_skips_unused_discovery(provider: NugsProvider) -> None:
    """Recording-only searches need no artist, venue or album discovery request."""
    provider._get_catalog_search = AsyncMock()  # type: ignore[method-assign]
    provider._search_recordings = AsyncMock(return_value=[])  # type: ignore[method-assign]
    await provider.search("Dust in a Baggie", [MediaType.TRACK])
    provider._get_catalog_search.assert_not_awaited()
    provider._search_recordings.assert_awaited_once_with("Dust in a Baggie", 5)


@pytest.mark.asyncio
async def test_date_without_artist(provider: NugsProvider) -> None:
    """An exact date returns concerts across artists instead of fuzzy album matches."""
    _catalog(provider, _fixture("releases_date_monthDay"))
    result = await provider.search("2023-05-11", [MediaType.ALBUM, MediaType.ARTIST], 3)
    assert len(result.albums) == 3
    assert not result.artists
    assert all(isinstance(album, Album) and album.year == 2023 for album in result.albums)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("Billy Strings Oakland", {"artistIds": "1125", "venueIds": "9361,6546"}),
        ("Billy Strings in Oakland", {"artistIds": "1125", "venueIds": "9361,6546"}),
        ("Oakland", {}),
        ("Unknown Artist Oakland Arena", {}),
        ("Billy Strings Oakland unknown", {}),
        ("Billy Strings Oak", {}),
        ("Oakland Arena", {"venueIds": "9361"}),
    ],
)
def test_artist_scopes_partial_venue(
    provider: NugsProvider, query: str, expected: dict[str, str]
) -> None:
    """Whole venue words require a full artist match and consume the remaining query."""
    data = {
        "artists": [{"id": 1125, "name": "Billy Strings"}],
        "venues": [
            {"id": 9361, "name": "Oakland Arena"},
            {"id": 6546, "name": "The Fox Oakland"},
            {"id": 9361, "name": "Oakland Arena"},
            {"id": 999, "name": "Oaklands Theatre"},
        ],
    }
    assert provider._get_search_filters(query, data) == expected


@pytest.mark.asyncio
async def test_release_request_budget(provider: NugsProvider) -> None:
    """Duplicate pages cannot cause an unbounded foreground catalog scan."""
    first = _fixture("releases_page0")["items"][0]
    page = {"items": [first], "total": 10000}
    request = _catalog(provider, page, page, page)
    result = await provider.search("Billy Strings", [MediaType.ALBUM], 5)
    assert [album.item_id for album in result.albums] == [str(first["id"])]
    assert request.await_count == 4


@pytest.mark.asyncio
async def test_artist_venue_candidates_share_request(provider: NugsProvider) -> None:
    """Artist and all matching venues intersect in one call without song requests."""
    page = _fixture("releases_intersection")
    request = _catalog(provider, page)
    provider._search_recordings = AsyncMock(return_value=[])  # type: ignore[method-assign]
    result = await provider.search("Billy Strings Red", [MediaType.ALBUM], 5)
    assert result.albums
    assert request.await_count == 2
    assert request.call_args.kwargs["artistIds"] == "1125"
    assert request.call_args.kwargs["venueIds"] == "42,5363,164"
    provider._search_recordings.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_venue_preserves_recording_results(provider: NugsProvider) -> None:
    """Album interpretation leaves mixed-media recording queries and results intact."""
    _catalog(provider, _fixture("releases_intersection"))
    recording = Mock()
    provider._search_recordings = AsyncMock(return_value=[recording])  # type: ignore[method-assign]
    result = await provider.search("Billy Strings Red", [MediaType.ALBUM, MediaType.TRACK], 5)
    assert result.albums
    assert result.tracks == [recording]
    provider._search_recordings.assert_awaited_once_with("Billy Strings Red", 5)
