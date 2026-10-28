"""Unit tests for Apple Music catalog/library endpoint selection in the media manager."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.media_items import Track

from music_assistant.providers.apple_music.media import AppleMusicMediaManager
from tests.common import use_real_create_task


def _catalog_song(catalog_id: str) -> dict[str, Any]:
    """Build a minimal catalog/songs response item."""
    return {
        "id": catalog_id,
        "type": "songs",
        "attributes": {
            "name": f"Catalog {catalog_id}",
            "artistName": "Artist",
            "albumName": "Album",
            "durationInMillis": 180000,
            "playParams": {"id": catalog_id},
        },
        "relationships": {},
    }


def _library_song(library_id: str, *, catalog_id: str | None = None) -> dict[str, Any]:
    """Build a me/library/songs response item, optionally carrying its catalog twin."""
    relationships: dict[str, Any] = {}
    if catalog_id is not None:
        relationships["catalog"] = {"data": [_catalog_song(catalog_id)]}
    return {
        "id": library_id,
        "type": "library-songs",
        "attributes": {
            "name": f"Library {library_id}",
            "artistName": "Artist",
            "albumName": "Album",
            "durationInMillis": 180000,
            "playParams": {"id": library_id},
        },
        "relationships": relationships,
    }


@pytest.fixture
def mock_api() -> MagicMock:
    """Return a MagicMock representing the Apple Music API client."""
    api_client = MagicMock()
    api_client.get_data = AsyncMock()
    return api_client


@pytest.fixture
def manager(mock_api: MagicMock) -> AppleMusicMediaManager:
    """Return an AppleMusicMediaManager wired to a mock API client and a always-miss cache."""
    provider = MagicMock()
    provider.instance_id = "apple_music_test"
    provider.domain = "apple_music"
    provider._storefront = "us"
    provider.logger = MagicMock()
    provider.mass.cache.get = AsyncMock(return_value=None)
    provider.mass.cache.get_with_freshness = AsyncMock(return_value=(None, False, False))
    provider.mass.cache.set = AsyncMock()
    use_real_create_task(provider.mass)
    provider.api_client = mock_api

    return AppleMusicMediaManager(provider)


@pytest.mark.asyncio
async def test_get_track_uses_catalog_endpoint_for_catalog_id(
    manager: AppleMusicMediaManager,
    mock_api: MagicMock,
) -> None:
    """get_track queries the catalog endpoint for a numeric adam id."""
    mock_api.get_data.return_value = {"data": [_catalog_song("1234567890")]}

    result = await manager.get_track("1234567890")

    mock_api.get_data.assert_called_once_with(
        "catalog/us/songs/1234567890",
        include="artists,albums",
    )
    assert isinstance(result, Track)
    assert result.item_id == "1234567890"


@pytest.mark.asyncio
async def test_get_track_uses_library_endpoint_for_library_id(
    manager: AppleMusicMediaManager,
    mock_api: MagicMock,
) -> None:
    """get_track queries me/library/songs for an 'i.' library id instead of 404ing."""
    mock_api.get_data.return_value = {"data": [_library_song("i.AWPNG58CL3m51X")]}

    result = await manager.get_track("i.AWPNG58CL3m51X")

    mock_api.get_data.assert_called_once_with(
        "me/library/songs/i.AWPNG58CL3m51X",
        include="catalog,artists,albums",
    )
    assert isinstance(result, Track)
    assert result.item_id == "i.AWPNG58CL3m51X"


@pytest.mark.asyncio
async def test_get_track_library_id_prefers_catalog_twin(
    manager: AppleMusicMediaManager,
    mock_api: MagicMock,
) -> None:
    """The included catalog relationship collapses a library id onto the catalog adam id."""

    async def _get_data(_endpoint: str, **kwargs: Any) -> dict[str, Any]:
        # Apple only embeds the catalog twin when it was actually requested, so a
        # dropped include= would silently leave the library id in place
        catalog_id = "1234567890" if "catalog" in kwargs.get("include", "") else None
        return {"data": [_library_song("i.AWPNG58CL3m51X", catalog_id=catalog_id)]}

    mock_api.get_data.side_effect = _get_data

    result = await manager.get_track("i.AWPNG58CL3m51X")

    assert result.item_id == "1234567890"
    assert result.name == "Catalog 1234567890"
