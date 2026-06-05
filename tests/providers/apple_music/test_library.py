"""Unit tests for Apple Music library track streaming and windowed enrichment."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

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
