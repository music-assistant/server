"""Live Phish catalog and library calls stay on livephish.com hosts."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import Album, Artist, BrowseFolder, ItemMapping

from music_assistant.providers.livephish import LivePhishProvider
from music_assistant.providers.livephish.constants import (
    ACCOUNT_URL,
    CATALOG_API,
    IMAGE_CDN,
    LEGACY_API_URL,
    STASH_API,
    STASH_URL,
)


def _response(provider: LivePhishProvider) -> None:
    """Point the HTTP session at an empty JSON object and skip a real login."""
    response = Mock(status=200, headers={})
    response.json = AsyncMock(return_value={"items": [], "total": 0, "result": {}})
    response.text = AsyncMock(
        return_value='{"responseAvailabilityCode":0,"Response":{"catalogSearchTypeContainers":[]}}'
    )
    context = AsyncMock()
    context.__aenter__.return_value = response
    cast("Mock", provider.mass.http_session.get).return_value = context
    cast("Any", provider).login = AsyncMock(return_value="test-token")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("api", "endpoint", "url"),
    [
        ("catalog", "releases/recent", f"{CATALOG_API}/releases/recent"),
        ("stash", "releases/favorite", f"{STASH_API}/me/releases/favorite"),
        ("purchases", "", f"{STASH_URL}/items"),
        ("subscription", "", ACCOUNT_URL),
        ("user", "", STASH_URL),
    ],
)
async def test_authenticated_calls_use_livephish_hosts(
    provider: LivePhishProvider, api: str, endpoint: str, url: str
) -> None:
    """Catalog, stash, subscription, and user calls use the player's Live Phish hosts."""
    _response(provider)
    method = cast("Any", LivePhishProvider._get_data).__wrapped__

    await method(provider, api, endpoint, limit=1)

    assert cast("Mock", provider.mass.http_session.get).call_args.args[0] == url
    headers = cast("Mock", provider.mass.http_session.get).call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer test-token"


@pytest.mark.asyncio
async def test_legacy_search_uses_livephish_stream_api(provider: LivePhishProvider) -> None:
    """Song search uses the Live Phish legacy catalog endpoint."""
    _response(provider)
    method = cast("Any", LivePhishProvider._get_search_data).__wrapped__.__wrapped__

    await method(provider, "Tweezer")

    call = cast("Mock", provider.mass.http_session.get).call_args
    assert call.args[0] == LEGACY_API_URL
    assert call.kwargs["params"]["method"] == "catalog.search"
    assert call.kwargs["params"]["searchStr"] == "Tweezer"


def test_relative_show_art_uses_the_player_cdn(provider: LivePhishProvider) -> None:
    """Show art stored as a relative path is resolved against the player's image CDN."""
    album = provider._parse_album(
        {
            "containerID": "1",
            "containerInfo": "2020.08.01 Madison Square Garden",
            "artistID": "2",
            "artistName": "Phish",
            "img": {"url": "/images/show.jpg"},
        }
    )

    assert album.image is not None
    assert album.image.path == f"{IMAGE_CDN}/images/show.jpg"


@pytest.mark.asyncio
async def test_legacy_artists_use_the_catalog_method(provider: LivePhishProvider) -> None:
    """The public catalog.artists method is requested from the legacy API."""
    _response(provider)
    method = cast("Any", LivePhishProvider._get_legacy_catalog).__wrapped__.__wrapped__

    await method(provider, "catalog.artists")

    call = cast("Mock", provider.mass.http_session.get).call_args
    assert call.args[0] == LEGACY_API_URL
    assert call.kwargs["params"]["method"] == "catalog.artists"


@pytest.mark.asyncio
async def test_library_uses_favorites_and_purchases(provider: LivePhishProvider) -> None:
    """Followed artists, favourite releases, and purchased shows form the library."""
    calls: list[tuple[str, str, dict[str, object]]] = []

    async def _get_all_items(api: str, endpoint: str, **kwargs: object) -> list[dict[str, object]]:
        calls.append((api, endpoint, kwargs))
        if endpoint == "artists/favorite":
            return [
                {"id": "1", "name": "Phish"},
                {"name": "Skip"},
            ]
        if endpoint == "releases/favorite":
            return [{"id": "435", "title": "A Live One", "artist": {"id": "1", "name": "Phish"}}]
        if api == "purchases":
            return [
                {
                    "id": "order-line",
                    "orderId": "order-1",
                    "product": {
                        "contentType": "audio",
                        "release": {
                            "id": "900",
                            "title": "Purchased Show",
                            "artist": {"id": "2", "name": "Trey"},
                        },
                    },
                },
                {
                    "id": "duplicate-order",
                    "product": {
                        "contentType": "audio",
                        "release": {
                            "id": "435",
                            "title": "A Live One",
                            "artist": {"id": "1", "name": "Phish"},
                        },
                    },
                },
                {
                    "id": "video-order",
                    "product": {
                        "contentType": "video",
                        "release": {"id": "901", "title": "Webcast"},
                    },
                },
            ]
        return []

    cast("Any", provider)._get_all_items = _get_all_items

    artists = [item async for item in provider.get_library_artists()]
    assert [artist.item_id for artist in artists] == ["1"]
    assert all(api != "purchases" for api, _endpoint, _kwargs in calls)

    albums = [item async for item in provider.get_library_albums()]
    assert artists[0].image is not None
    assert artists[0].image.path == "icon.png"
    icon = await provider.resolve_image(artists[0].image.path)
    assert isinstance(icon, str)
    assert icon.endswith("providers/livephish/icon.png")
    assert [album.item_id for album in albums] == ["435", "900"]
    assert ("catalog", "releases") not in {(api, endpoint) for api, endpoint, _kwargs in calls}
    assert calls[0][0:2] == ("stash", "artists/favorite")
    assert calls[0][2]["sortBy"] == "nameAsc"
    purchase_calls = [call for call in calls if call[0] == "purchases"]
    assert purchase_calls
    assert purchase_calls[0][2]["itemTypes"] == "audio"
    assert purchase_calls[0][2]["productTypes"] == "release"


@pytest.mark.asyncio
async def test_recommendations_omit_recommended_shows(provider: LivePhishProvider) -> None:
    """Recommended Shows is not a Live Phish row."""
    rows = await provider.get_recommendations()

    assert [row.item_id for row in rows] == [
        "livephish_popular_shows",
        "livephish_recent_shows",
    ]


@pytest.mark.asyncio
async def test_album_tracks_keep_the_provider_artist(provider: LivePhishProvider) -> None:
    """Album tracks name the Live Phish artist."""
    track = provider._parse_track(
        {"trackID": 8869, "songTitle": "Sample in a Jar"},
        artist=ItemMapping(
            media_type=MediaType.ARTIST,
            item_id="1",
            provider=provider.instance_id,
            name="Phish",
        ),
    )
    provider._load_album_tracks = AsyncMock(return_value=[track])  # type: ignore[method-assign]
    lookup = AsyncMock()
    cast("Any", provider.mass).music.artists.get_library_item_by_prov_id = lookup

    tracks = await provider.get_album_tracks("504")

    assert tracks[0].artists[0].item_id == "1"
    assert tracks[0].artists[0].provider == provider.instance_id
    lookup.assert_not_awaited()


def _show(container_id: str, title: str) -> dict[str, object]:
    """Return one catalog container row."""
    return {
        "containerID": container_id,
        "containerInfo": title,
        "artistID": 1,
        "artistName": "Phish",
    }


@pytest.mark.asyncio
async def test_browse_root_matches_the_app_sections(provider: LivePhishProvider) -> None:
    """Browse starts with Artist, Years, Songs, Albums, and Tour."""
    items = await provider.browse("livephish--test123://")

    assert [item.item_id for item in items] == ["artist", "years", "songs", "albums", "tour"]
    assert [item.name for item in items] == ["Artist", "Years", "Songs", "Albums", "Tour"]
    assert all(isinstance(item, BrowseFolder) and not item.is_playable for item in items)
    years = items[1]
    assert isinstance(years, BrowseFolder)
    assert years.path == "livephish--test123://years"


@pytest.mark.asyncio
async def test_browse_loads_each_app_catalog(provider: LivePhishProvider) -> None:
    """Each section calls the catalog method the Live Phish app uses."""
    calls: list[tuple[str, dict[str, object]]] = []

    async def catalog(method: str, **params: object) -> object:
        calls.append((method, params))
        if method == "catalog.artists":
            return {"artists": [{"artistID": 1, "artistName": "Phish"}]}
        if method == "catalog.showYearCatalog":
            return ["1989", "2024", "1997"]
        if method == "catalog.songCatalog":
            return [{"ID": 10, "title": "Tweezer"}, {"title": "Skip"}]
        if method == "catalog.containerCategories":
            return {
                "containerCategories": [
                    {
                        "containerCategoryID": 121,
                        "containerCategoryName": "Fall 2026",
                        "img": {"url": "/images/tour.jpg"},
                    }
                ]
            }
        if method == "catalog.containersAll":
            return {"containers": [_show("430", "12/30/97 Madison Square Garden")]}
        return {}

    provider._get_legacy_catalog = catalog  # type: ignore[method-assign]

    artists = await provider.browse("livephish--test123://artist")
    years = await provider.browse("livephish--test123://years")
    year_shows = await provider.browse("livephish--test123://years/1997")
    songs = await provider.browse("livephish--test123://songs")
    song_shows = await provider.browse("livephish--test123://songs/10")
    albums = await provider.browse("livephish--test123://albums")
    tours = await provider.browse("livephish--test123://tour")
    tour_shows = await provider.browse("livephish--test123://tour/121")

    assert [item.item_id for item in artists] == ["1"]
    assert isinstance(artists[0], Artist)
    assert [item.name for item in years] == ["2024", "1997", "1989"]
    assert isinstance(year_shows[0], Album)
    assert year_shows[0].item_id == "430"
    assert [item.item_id for item in songs] == ["10"]
    assert song_shows[0].item_id == "430"
    assert albums[0].item_id == "430"
    assert tours[0].name == "Fall 2026"
    assert isinstance(tours[0], BrowseFolder)
    assert tours[0].image is not None
    assert tours[0].image.path == f"{IMAGE_CDN}/images/tour.jpg"
    assert tour_shows[0].item_id == "430"
    assert calls == [
        ("catalog.artists", {}),
        ("catalog.showYearCatalog", {}),
        (
            "catalog.containersAll",
            {
                "showYears": "1997",
                "sortBy": "performanceDate",
                "sortType": "desc",
                "startOffset": 1,
            },
        ),
        ("catalog.songCatalog", {}),
        ("catalog.songCatalog", {}),
        (
            "catalog.containersAll",
            {"songsPlayed": "Tweezer", "startOffset": 1, "availType": 1},
        ),
        (
            "catalog.containersAll",
            {"sortBy": "albumTitle", "sortType": "asc", "startOffset": 1},
        ),
        ("catalog.containerCategories", {}),
        (
            "catalog.containersAll",
            {"containerCategoryID": "121", "startOffset": 1, "availtype": 1},
        ),
    ]
