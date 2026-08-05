"""Tests for Spotify external ID lookup."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from music_assistant.providers.spotify.provider import SpotifyProvider


@pytest.fixture
def spotify_provider() -> SpotifyProvider:
    """Return a SpotifyProvider with mocked mass/cache."""
    prov = object.__new__(SpotifyProvider)
    prov.config = MagicMock(instance_id="spotify--test")
    prov.manifest = MagicMock(domain="spotify")
    prov.logger = MagicMock()
    prov._sp_user = None

    mass = MagicMock()
    mass.metadata.locale = "en_US"
    mass.cache.get = AsyncMock(return_value=None)
    mass.cache.get_with_freshness = AsyncMock(return_value=(None, False, False))
    mass.cache.set = AsyncMock()
    mass.create_task = MagicMock(side_effect=lambda coro, **_: coro.close())
    prov.mass = mass

    return prov


@pytest.mark.asyncio
async def test_get_track_by_isrc(
    spotify_provider: SpotifyProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test track lookup by ISRC."""
    get_data_mock = AsyncMock(
        return_value={
            "tracks": {
                "items": [
                    {
                        "id": "track123",
                        "name": "Test Track",
                        "artists": [{"id": "artist123", "name": "Test Artist"}],
                        "album": {
                            "id": "album123",
                            "name": "Test Album",
                            "images": [{"url": "https://example.com/image.jpg"}],
                        },
                        "duration_ms": 180000,
                        "external_ids": {"isrc": "USUM71703861"},
                    }
                ]
            }
        }
    )
    monkeypatch.setattr(spotify_provider, "_get_data", get_data_mock)

    track = await spotify_provider.get_track_by_external_id("USUM71703861", "isrc")

    assert track is not None
    assert track.item_id == "track123"
    assert track.name == "Test Track"
    get_data_mock.assert_called_once_with("search", q="isrc:USUM71703861", type="track", limit=1)


@pytest.mark.asyncio
async def test_get_track_by_isrc_not_found(
    spotify_provider: SpotifyProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test track lookup by ISRC when not found."""
    get_data_mock = AsyncMock(return_value={"tracks": {"items": []}})
    monkeypatch.setattr(spotify_provider, "_get_data", get_data_mock)

    track = await spotify_provider.get_track_by_external_id("INVALID_ISRC", "isrc")

    assert track is None


@pytest.mark.asyncio
async def test_get_track_by_wrong_type(spotify_provider: SpotifyProvider) -> None:
    """Test track lookup with wrong external ID type."""
    track = await spotify_provider.get_track_by_external_id("12345", "upc")

    assert track is None


@pytest.mark.asyncio
async def test_get_album_by_upc(
    spotify_provider: SpotifyProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test album lookup by UPC."""
    get_data_mock = AsyncMock(
        return_value={
            "albums": {
                "items": [
                    {
                        "id": "album123",
                        "name": "Test Album",
                        "artists": [{"id": "artist123", "name": "Test Artist"}],
                        "images": [{"url": "https://example.com/image.jpg"}],
                        "release_date": "2017-01-01",
                        "total_tracks": 10,
                        "external_ids": {"upc": "00602547924766"},
                    }
                ]
            }
        }
    )
    monkeypatch.setattr(spotify_provider, "_get_data", get_data_mock)

    album = await spotify_provider.get_album_by_external_id("00602547924766", "upc")

    assert album is not None
    assert album.item_id == "album123"
    assert album.name == "Test Album"
    get_data_mock.assert_called_once_with("search", q="upc:00602547924766", type="album", limit=1)


@pytest.mark.asyncio
async def test_get_album_by_barcode(
    spotify_provider: SpotifyProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test album lookup by Barcode (same as UPC)."""
    get_data_mock = AsyncMock(
        return_value={
            "albums": {
                "items": [
                    {
                        "id": "album123",
                        "name": "Test Album",
                        "artists": [{"id": "artist123", "name": "Test Artist"}],
                        "images": [{"url": "https://example.com/image.jpg"}],
                        "release_date": "2017-01-01",
                        "total_tracks": 10,
                    }
                ]
            }
        }
    )
    monkeypatch.setattr(spotify_provider, "_get_data", get_data_mock)

    album = await spotify_provider.get_album_by_external_id("00602547924766", "barcode")

    assert album is not None
    assert album.item_id == "album123"
    get_data_mock.assert_called_once_with("search", q="upc:00602547924766", type="album", limit=1)


@pytest.mark.asyncio
async def test_get_album_by_upc_not_found(
    spotify_provider: SpotifyProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test album lookup by UPC when not found."""
    get_data_mock = AsyncMock(return_value={"albums": {"items": []}})
    monkeypatch.setattr(spotify_provider, "_get_data", get_data_mock)

    album = await spotify_provider.get_album_by_external_id("INVALID_UPC", "upc")

    assert album is None


@pytest.mark.asyncio
async def test_get_album_by_wrong_type(spotify_provider: SpotifyProvider) -> None:
    """Test album lookup with wrong external ID type."""
    album = await spotify_provider.get_album_by_external_id("USUM71703861", "isrc")

    assert album is None
