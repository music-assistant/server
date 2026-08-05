"""Unit tests for Apple Music external ID lookup."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from music_assistant.providers.apple_music.media import AppleMusicMediaManager


def _make_media_manager() -> tuple[AppleMusicMediaManager, MagicMock]:
    """Return a MediaManager together with its mock provider."""
    provider = MagicMock()
    provider.logger = MagicMock()
    provider._storefront = "us"
    api_mock = MagicMock()
    provider.api = api_mock
    return AppleMusicMediaManager(provider), api_mock


@pytest.mark.asyncio
async def test_get_track_by_isrc() -> None:
    """Track lookup by ISRC calls the correct API endpoint."""
    manager, api_mock = _make_media_manager()

    api_mock.get_data = AsyncMock(
        return_value={
            "data": [
                {
                    "id": "1234567890",
                    "type": "songs",
                    "attributes": {
                        "name": "Test Track",
                        "artistName": "Test Artist",
                        "isrc": "USTEST1234567",
                    },
                }
            ]
        }
    )
    api_mock.get_ratings = AsyncMock(return_value={})

    result = await manager.get_track_by_external_id("USTEST1234567", "ISRC")

    assert result is not None
    api_mock.get_data.assert_called_once()
    call_args = api_mock.get_data.call_args
    assert "catalog/us/songs" in call_args[0][0]
    assert call_args[1]["filter[isrc]"] == "USTEST1234567"


@pytest.mark.asyncio
async def test_get_track_by_isrc_not_found() -> None:
    """Track lookup returns None when ISRC is not found."""
    manager, api_mock = _make_media_manager()

    api_mock.get_data = AsyncMock(return_value={"data": []})

    result = await manager.get_track_by_external_id("UNKNOWN123", "ISRC")

    assert result is None


@pytest.mark.asyncio
async def test_get_track_by_wrong_id_type() -> None:
    """Track lookup returns None for unsupported ID types."""
    manager, api_mock = _make_media_manager()

    result = await manager.get_track_by_external_id("123456", "UPC")

    assert result is None
    api_mock.get_data.assert_not_called()


@pytest.mark.asyncio
async def test_get_album_by_upc() -> None:
    """Album lookup by UPC calls the correct API endpoint."""
    manager, api_mock = _make_media_manager()

    api_mock.get_data = AsyncMock(
        return_value={
            "data": [
                {
                    "id": "9876543210",
                    "type": "albums",
                    "attributes": {
                        "name": "Test Album",
                        "artistName": "Test Artist",
                        "upc": "123456789012",
                    },
                }
            ]
        }
    )
    api_mock.get_ratings = AsyncMock(return_value={})

    result = await manager.get_album_by_external_id("0123456789012", "UPC")

    assert result is not None
    api_mock.get_data.assert_called_once()
    call_args = api_mock.get_data.call_args
    assert "catalog/us/albums" in call_args[0][0]
    # UPC should be normalized (leading zero stripped)
    assert call_args[1]["filter[upc]"] == "123456789012"


@pytest.mark.asyncio
async def test_get_album_by_barcode() -> None:
    """Album lookup by BARCODE (synonym for UPC) works."""
    manager, api_mock = _make_media_manager()

    api_mock.get_data = AsyncMock(
        return_value={
            "data": [
                {
                    "id": "9876543210",
                    "type": "albums",
                    "attributes": {
                        "name": "Test Album",
                        "artistName": "Test Artist",
                        "upc": "123456789012",
                    },
                }
            ]
        }
    )
    api_mock.get_ratings = AsyncMock(return_value={})

    result = await manager.get_album_by_external_id("123456789012", "BARCODE")

    assert result is not None


@pytest.mark.asyncio
async def test_get_album_by_wrong_id_type() -> None:
    """Album lookup returns None for unsupported ID types."""
    manager, api_mock = _make_media_manager()

    result = await manager.get_album_by_external_id("USTEST1234567", "ISRC")

    assert result is None
    api_mock.get_data.assert_not_called()
