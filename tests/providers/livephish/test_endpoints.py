"""Live Phish catalog and library calls stay on livephish.com hosts."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import ItemMapping

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
    """Library artists are loaded from the public catalog.artists method."""
    _response(provider)
    method = cast("Any", LivePhishProvider._get_legacy_catalog).__wrapped__.__wrapped__

    await method(provider, "catalog.artists")

    call = cast("Mock", provider.mass.http_session.get).call_args
    assert call.args[0] == LEGACY_API_URL
    assert call.kwargs["params"]["method"] == "catalog.artists"


@pytest.mark.asyncio
async def test_library_uses_catalog_artists_and_releases(provider: LivePhishProvider) -> None:
    """Artists come from catalog.artists and albums from that artist's releases."""
    provider._get_legacy_catalog = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "artists": [
                {"artistID": 1, "artistName": "Phish", "artistImage": "/images/phish.jpg"},
                {"artistName": "Skip"},
            ]
        }
    )
    provider._get_all_items = AsyncMock(  # type: ignore[method-assign]
        return_value=[{"id": "435", "title": "A Live One", "artist": {"id": "1", "name": "Phish"}}]
    )

    artists = [item async for item in provider.get_library_artists()]
    albums = [item async for item in provider.get_library_albums()]

    assert [artist.item_id for artist in artists] == ["1"]
    assert artists[0].image is not None
    assert artists[0].image.path == f"{IMAGE_CDN}/images/phish.jpg"
    assert [album.item_id for album in albums] == ["435"]
    assert provider._get_all_items.await_args is not None
    assert provider._get_all_items.await_args.args[:2] == ("catalog", "releases")
    assert provider._get_all_items.await_args.kwargs["artistIds"] == "1"


@pytest.mark.asyncio
async def test_recommendations_omit_recommended_shows(provider: LivePhishProvider) -> None:
    """Recommended Shows is not a Live Phish row."""
    rows = await provider.get_recommendations()

    assert [row.item_id for row in rows] == [
        "livephish_popular_shows",
        "livephish_recent_shows",
    ]


@pytest.mark.asyncio
async def test_album_tracks_name_the_library_artist(provider: LivePhishProvider) -> None:
    """Playing a track points at the library artist, not the catalog id."""
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
    cast("Any", provider.mass).music.artists.get_library_item_by_prov_id = AsyncMock(
        return_value=ItemMapping(
            media_type=MediaType.ARTIST,
            item_id="8",
            provider="library",
            name="Phish",
        )
    )

    tracks = await provider.get_album_tracks("504")

    assert tracks[0].artists[0].item_id == "8"
    assert tracks[0].artists[0].provider == "library"
