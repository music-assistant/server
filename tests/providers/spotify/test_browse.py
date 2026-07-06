"""Unit tests for the Spotify provider's curated browse implementation."""

from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import Album, BrowseFolder, Playlist

from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.spotify.provider import SpotifyProvider


def _make_album_obj(album_id: str, name: str) -> dict[str, Any]:
    return {
        "id": album_id,
        "name": name,
        "album_type": "album",
        "external_ids": {},
        "external_urls": {"spotify": f"https://open.spotify.com/album/{album_id}"},
        "artists": [
            {
                "id": "artist1",
                "name": "Test Artist",
                "external_urls": {"spotify": "https://open.spotify.com/artist/artist1"},
            }
        ],
        "images": [],
    }


def _make_playlist_obj(playlist_id: str, name: str) -> dict[str, Any]:
    return {
        "id": playlist_id,
        "name": name,
        "collaborative": False,
        "owner": {"id": "spotify", "display_name": "Spotify"},
        "external_urls": {"spotify": f"https://open.spotify.com/playlist/{playlist_id}"},
        "images": [],
    }


def _make_category_obj(category_id: str, name: str) -> dict[str, Any]:
    return {"id": category_id, "name": name}


@pytest.fixture
def get_data() -> AsyncMock:
    """Return an AsyncMock standing in for SpotifyProvider._get_data."""
    return AsyncMock()


@pytest.fixture
def provider(get_data: AsyncMock, monkeypatch: pytest.MonkeyPatch) -> SpotifyProvider:
    """Return a SpotifyProvider with mocked mass/cache, bypassing __init__."""
    prov = object.__new__(SpotifyProvider)
    # instance_id and domain are read-only properties backed by config/manifest
    prov.config = MagicMock(instance_id="spotify--test")
    prov.manifest = MagicMock(domain="spotify")
    prov.logger = MagicMock()
    prov._sp_user = None

    mass = MagicMock()
    mass.metadata.locale = "de_DE"
    # bypass the use_cache decorator: always miss, swallow the background store task
    mass.cache.get = AsyncMock(return_value=None)
    mass.cache.get_with_freshness = AsyncMock(return_value=(None, False, False))
    mass.cache.set = AsyncMock()
    mass.create_task = MagicMock(side_effect=lambda coro, **_: coro.close())
    prov.mass = mass

    monkeypatch.setattr(prov, "_get_data", get_data)
    return prov


@pytest.fixture
def patch_super_browse() -> Generator[AsyncMock]:
    """Patch MusicProvider.browse so the root listing does not need a full provider."""
    original = MusicProvider.browse
    mock = AsyncMock(return_value=[])
    MusicProvider.browse = mock  # type: ignore[method-assign]
    try:
        yield mock
    finally:
        MusicProvider.browse = original  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_get_new_releases_returns_albums(
    provider: SpotifyProvider, get_data: AsyncMock
) -> None:
    """_get_new_releases parses albums from the browse/new-releases response."""
    get_data.return_value = {
        "albums": {"items": [_make_album_obj("a1", "Album 1"), _make_album_obj("a2", "Album 2")]}
    }

    result = await provider._get_new_releases()

    get_data.assert_awaited_once_with("browse/new-releases", limit=50)
    assert len(result) == 2
    assert all(isinstance(a, Album) for a in result)


@pytest.mark.asyncio
async def test_get_new_releases_skips_items_without_id(
    provider: SpotifyProvider, get_data: AsyncMock
) -> None:
    """_get_new_releases ignores malformed album entries."""
    get_data.return_value = {"albums": {"items": [{"name": "no id"}, None]}}

    result = await provider._get_new_releases()

    assert result == []


@pytest.mark.asyncio
async def test_get_new_releases_handles_not_found(
    provider: SpotifyProvider, get_data: AsyncMock
) -> None:
    """_get_new_releases returns an empty list when the endpoint is unavailable."""
    get_data.side_effect = MediaNotFoundError("nope")

    result = await provider._get_new_releases()

    assert result == []


@pytest.mark.asyncio
async def test_get_categories_returns_browse_folders(
    provider: SpotifyProvider, get_data: AsyncMock
) -> None:
    """_get_categories maps Spotify categories onto browse folders with stable paths."""
    get_data.return_value = {
        "categories": {
            "items": [_make_category_obj("pop", "Pop"), _make_category_obj("rock", "Rock")]
        }
    }

    result = await provider._get_categories("de_DE")

    get_data.assert_awaited_once_with("browse/categories", locale="de_DE", limit=50)
    assert all(isinstance(f, BrowseFolder) for f in result)
    pop = result[0]
    assert pop.item_id == "pop"
    assert pop.name == "Pop"
    assert pop.path == "spotify--test://categories/pop"
    assert pop.is_playable is False


@pytest.mark.asyncio
async def test_get_categories_skips_incomplete(
    provider: SpotifyProvider, get_data: AsyncMock
) -> None:
    """_get_categories ignores categories missing an id or name."""
    get_data.return_value = {"categories": {"items": [{"id": "x"}, {"name": "y"}, None]}}

    result = await provider._get_categories("de_DE")

    assert result == []


@pytest.mark.asyncio
async def test_get_categories_handles_not_found(
    provider: SpotifyProvider, get_data: AsyncMock
) -> None:
    """_get_categories returns an empty list when the endpoint is unavailable."""
    get_data.side_effect = MediaNotFoundError("nope")

    result = await provider._get_categories("de_DE")

    assert result == []


@pytest.mark.asyncio
async def test_get_category_playlists_returns_playlists(
    provider: SpotifyProvider, get_data: AsyncMock
) -> None:
    """_get_category_playlists parses playlists via the grandfathered global session."""
    get_data.return_value = {"playlists": {"items": [_make_playlist_obj("p1", "Playlist 1")]}}

    result = await provider._get_category_playlists("pop", "de_DE")

    get_data.assert_awaited_once_with(
        "browse/categories/pop/playlists",
        locale="de_DE",
        limit=50,
        use_global_session=True,
    )
    assert len(result) == 1
    assert isinstance(result[0], Playlist)


@pytest.mark.asyncio
async def test_get_category_playlists_handles_not_found(
    provider: SpotifyProvider, get_data: AsyncMock
) -> None:
    """_get_category_playlists returns an empty list when the endpoint is unavailable."""
    get_data.side_effect = MediaNotFoundError("nope")

    result = await provider._get_category_playlists("pop", "de_DE")

    assert result == []


@pytest.mark.asyncio
async def test_browse_root_prepends_curated_folders(
    provider: SpotifyProvider, patch_super_browse: AsyncMock
) -> None:
    """Browsing the root adds the curated folders before the standard library folders."""
    patch_super_browse.return_value = [
        BrowseFolder(item_id="artists", provider=provider.instance_id, name="Artists")
    ]

    result = await provider.browse("spotify--test://")

    assert [f.item_id for f in result] == ["new-releases", "categories", "artists"]
    new_releases = next(
        f for f in result if isinstance(f, BrowseFolder) and f.item_id == "new-releases"
    )
    categories = next(
        f for f in result if isinstance(f, BrowseFolder) and f.item_id == "categories"
    )
    assert new_releases.translation_key == "new_releases"
    assert new_releases.path == "spotify--test://new-releases"
    assert new_releases.is_playable is True
    assert categories.translation_key == "genres_and_moods"
    assert categories.path == "spotify--test://categories"
    assert categories.is_playable is False


@pytest.mark.asyncio
async def test_browse_dispatches_to_helpers(
    provider: SpotifyProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Browse delegates each curated subpath to the matching cached helper."""
    new_releases: list[Album] = []
    folders: list[BrowseFolder] = []
    playlists: list[Playlist] = []
    get_new_releases = AsyncMock(return_value=new_releases)
    get_categories = AsyncMock(return_value=folders)
    get_category_playlists = AsyncMock(return_value=playlists)
    monkeypatch.setattr(provider, "_get_new_releases", get_new_releases)
    monkeypatch.setattr(provider, "_get_categories", get_categories)
    monkeypatch.setattr(provider, "_get_category_playlists", get_category_playlists)

    assert await provider.browse("spotify--test://new-releases") is new_releases
    get_new_releases.assert_awaited_once_with()

    assert await provider.browse("spotify--test://categories") is folders
    get_categories.assert_awaited_once_with("de_DE")

    assert await provider.browse("spotify--test://categories/pop") is playlists
    get_category_playlists.assert_awaited_once_with("pop", "de_DE")
