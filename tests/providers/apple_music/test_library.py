"""Unit tests for Apple Music library track streaming and windowed enrichment."""

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import (
    Album,
    Artist,
    ItemMapping,
    ProviderMapping,
    Track,
    UniqueList,
)

from music_assistant.providers.apple_music.library import (
    _TRACK_SYNC_WINDOW,
    AppleMusicLibraryManager,
)


def _library_song(idx: int, *, catalog_id: str | None) -> dict[str, Any]:
    """Build a minimal me/library/songs listing item, optionally catalog-backed."""
    play_params: dict[str, Any] = {"id": f"i.{idx}"}
    if catalog_id is not None:
        play_params["catalogId"] = catalog_id
    return {
        "id": f"i.{idx}",
        "type": "library-songs",
        "attributes": {"name": f"Track {idx}", "playParams": play_params},
    }


def _catalog_song(catalog_id: str) -> dict[str, Any]:
    """Build a minimal catalog/songs response item."""
    return {
        "id": catalog_id,
        "type": "songs",
        "attributes": {"name": f"Catalog {catalog_id}", "playParams": {"id": catalog_id}},
    }


def _make_test_track(
    track_id: str,
    track_name: str,
    artist_id: str,
    artist_name: str,
    album_id: str | None = None,
    album_name: str | None = None,
    instance_id: str = "apple_music--test",
) -> Track:
    """Build a real Track instance with Artist and optional Album for testing."""
    artists = UniqueList(
        [
            Artist(
                provider="apple_music",
                item_id=artist_id,
                name=artist_name,
                provider_mappings={
                    ProviderMapping(
                        item_id=artist_id,
                        provider_domain="apple_music",
                        provider_instance=instance_id,
                    )
                },
            )
        ]
    )

    album = None
    if album_id and album_name:
        album = Album(
            provider="apple_music",
            item_id=album_id,
            name=album_name,
            provider_mappings={
                ProviderMapping(
                    item_id=album_id,
                    provider_domain="apple_music",
                    provider_instance=instance_id,
                )
            },
        )

    return Track(
        provider="apple_music",
        item_id=track_id,
        name=track_name,
        artists=cast("UniqueList[Artist | ItemMapping]", artists),
        album=album,
        provider_mappings={
            ProviderMapping(
                item_id=track_id,
                provider_domain="apple_music",
                provider_instance=instance_id,
            )
        },
    )


def _make_test_provider() -> MagicMock:
    """Build a mock provider for search replacement tests."""
    provider = MagicMock()
    provider.domain = "apple_music"
    provider.instance_id = "apple_music--test"
    return provider


def _make_manager(
    stream_items: list[dict[str, Any]],
) -> tuple[AppleMusicLibraryManager, MagicMock, dict[str, Any]]:
    """
    Build a library manager whose api streams ``stream_items`` and echoes catalog enrichment.

    The returned ``state`` dict tracks how many listing items have been streamed and at what
    point the first enrichment request fired, so tests can assert streaming/windowing behaviour.
    """
    provider = MagicMock()
    provider.domain = "apple_music"
    provider.instance_id = "apple_music--test"
    provider._storefront = "us"
    api = provider.api_client
    state: dict[str, Any] = {"streamed": 0, "first_enrich_at": None}

    async def _iter(*_args: Any, **_kwargs: Any) -> Any:
        for item in stream_items:
            state["streamed"] += 1
            yield item

    async def _get_data(_endpoint: str, **kwargs: Any) -> dict[str, Any]:
        if state["first_enrich_at"] is None:
            state["first_enrich_at"] = state["streamed"]
        ids = kwargs["ids"].split(",")
        assert len(ids) <= _TRACK_SYNC_WINDOW  # never exceed the documented catalog batch limit
        return {"data": [_catalog_song(cid) for cid in ids]}

    api.iter_all_items = _iter
    api.get_data = AsyncMock(side_effect=_get_data)
    api.get_ratings = AsyncMock(return_value={})
    return AppleMusicLibraryManager(provider), api, state


@pytest.mark.asyncio
async def test_catalog_enrichment_is_windowed() -> None:
    """Catalog enrichment runs in batches capped at the window size, never one giant request."""
    count = _TRACK_SYNC_WINDOW * 2 + 20
    items = [_library_song(i, catalog_id=f"c{i}") for i in range(count)]
    manager, api, _ = _make_manager(items)
    tracks = [track async for track in manager.get_library_tracks()]
    assert len(tracks) == count
    # 320 catalog ids -> ceil(320 / 150) = 3 enrichment requests.
    assert api.get_data.call_count == 3


@pytest.mark.asyncio
async def test_enriches_before_listing_completes() -> None:
    """A window is enriched and yielded as soon as it fills, not after the whole listing."""
    count = _TRACK_SYNC_WINDOW * 2
    items = [_library_song(i, catalog_id=f"c{i}") for i in range(count)]
    manager, _, state = _make_manager(items)
    [track async for track in manager.get_library_tracks()]
    assert state["first_enrich_at"] == _TRACK_SYNC_WINDOW


@pytest.mark.asyncio
async def test_search_replacement_finds_exact_match() -> None:
    """Search replacement finds exact match when deprecated catalog ID no longer exists."""
    provider = _make_test_provider()

    # Mock library item with track metadata
    library_item = {
        "id": "i.123",
        "attributes": {
            "name": "Test Track",
            "artistName": "Test Artist",
            "albumName": "Test Album",
        },
    }

    # Create real Track instance for proper isinstance() check
    mock_track = _make_test_track(
        track_id="999",
        track_name="Test Track",
        artist_id="456",
        artist_name="Test Artist",
        album_id="789",
        album_name="Test Album",
    )

    search_results = MagicMock()
    search_results.tracks = [mock_track]

    provider.media_manager.search = AsyncMock(return_value=search_results)

    manager = AppleMusicLibraryManager(provider)

    # Test search replacement
    result = await manager._try_search_replacement_for_deprecated_track(library_item, True)

    assert result is not None
    assert result.name == "Test Track"
    assert result.favorite is True
    provider.media_manager.search.assert_called_once_with(
        "Test Artist Test Track", [MediaType.TRACK], limit=10
    )


@pytest.mark.asyncio
async def test_search_replacement_no_match_wrong_track_name() -> None:
    """Search replacement returns None when track name doesn't match."""
    provider = _make_test_provider()

    library_item = {
        "id": "i.123",
        "attributes": {
            "name": "Test Track",
            "artistName": "Test Artist",
        },
    }

    mock_track = _make_test_track(
        track_id="999",
        track_name="Different Song",  # Wrong name
        artist_id="456",
        artist_name="Test Artist",
    )

    search_results = MagicMock()
    search_results.tracks = [mock_track]

    provider.media_manager.search = AsyncMock(return_value=search_results)

    manager = AppleMusicLibraryManager(provider)
    result = await manager._try_search_replacement_for_deprecated_track(library_item, False)

    assert result is None


@pytest.mark.asyncio
async def test_search_replacement_no_match_wrong_artist() -> None:
    """Search replacement returns None when artist name doesn't match."""
    provider = _make_test_provider()

    library_item = {
        "id": "i.123",
        "attributes": {
            "name": "Test Track",
            "artistName": "Test Artist",
        },
    }

    mock_track = _make_test_track(
        track_id="999",
        track_name="Test Track",
        artist_id="456",
        artist_name="Different Artist",  # Wrong artist
    )

    search_results = MagicMock()
    search_results.tracks = [mock_track]

    provider.media_manager.search = AsyncMock(return_value=search_results)

    manager = AppleMusicLibraryManager(provider)
    result = await manager._try_search_replacement_for_deprecated_track(library_item, False)

    assert result is None


@pytest.mark.asyncio
async def test_search_replacement_album_mismatch_skipped() -> None:
    """Search replacement skips tracks with mismatched album when album info available."""
    provider = _make_test_provider()

    library_item = {
        "id": "i.123",
        "attributes": {
            "name": "Test Track",
            "artistName": "Test Artist",
            "albumName": "Test Album",
        },
    }

    mock_track = _make_test_track(
        track_id="999",
        track_name="Test Track",
        artist_id="456",
        artist_name="Test Artist",
        album_id="789",
        album_name="Different Album",  # Wrong album
    )

    search_results = MagicMock()
    search_results.tracks = [mock_track]

    provider.media_manager.search = AsyncMock(return_value=search_results)

    manager = AppleMusicLibraryManager(provider)
    result = await manager._try_search_replacement_for_deprecated_track(library_item, False)

    assert result is None


@pytest.mark.asyncio
async def test_search_replacement_no_results() -> None:
    """Search replacement returns None when search yields no results."""
    provider = _make_test_provider()

    library_item = {
        "id": "i.123",
        "attributes": {
            "name": "Test Track",
            "artistName": "Test Artist",
        },
    }

    search_results = MagicMock()
    search_results.tracks = []  # Empty results

    provider.media_manager.search = AsyncMock(return_value=search_results)

    manager = AppleMusicLibraryManager(provider)
    result = await manager._try_search_replacement_for_deprecated_track(library_item, False)

    assert result is None


@pytest.mark.asyncio
async def test_search_replacement_handles_exceptions() -> None:
    """Search replacement returns None and logs when search raises exception."""
    provider = _make_test_provider()

    library_item = {
        "id": "i.123",
        "attributes": {
            "name": "Test Track",
            "artistName": "Test Artist",
        },
    }

    provider.media_manager.search = AsyncMock(side_effect=Exception("Network error"))

    manager = AppleMusicLibraryManager(provider)
    result = await manager._try_search_replacement_for_deprecated_track(library_item, False)

    assert result is None


@pytest.mark.asyncio
async def test_search_replacement_missing_metadata() -> None:
    """Search replacement returns None when library item lacks required metadata."""
    provider = _make_test_provider()

    # Missing track name
    library_item = {
        "id": "i.123",
        "attributes": {
            "artistName": "Test Artist",
        },
    }

    manager = AppleMusicLibraryManager(provider)
    result = await manager._try_search_replacement_for_deprecated_track(library_item, False)

    assert result is None

    # Missing artist name
    library_item = {
        "id": "i.123",
        "attributes": {
            "name": "Test Track",
        },
    }

    result = await manager._try_search_replacement_for_deprecated_track(library_item, False)

    assert result is None


@pytest.mark.asyncio
async def test_search_replacement_skips_item_mappings() -> None:
    """Search replacement skips ItemMapping entries and only processes Track objects."""
    provider = _make_test_provider()

    library_item = {
        "id": "i.123",
        "attributes": {
            "name": "Test Track",
            "artistName": "Test Artist",
            "albumName": "Test Album",
        },
    }

    # Create an ItemMapping instead of a Track
    item_mapping = ItemMapping(
        media_type=MediaType.TRACK,
        item_id="999",
        provider="apple_music",
        name="Test Track",
    )

    search_results = MagicMock()
    search_results.tracks = [item_mapping]  # Only ItemMapping, no Track objects

    provider.media_manager.search = AsyncMock(return_value=search_results)

    manager = AppleMusicLibraryManager(provider)
    result = await manager._try_search_replacement_for_deprecated_track(library_item, False)

    # Should return None since ItemMapping should be skipped
    assert result is None
