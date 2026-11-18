"""Tests for the Spotify paged listing helper."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from music_assistant.providers.spotify.provider import SpotifyProvider


def _make_provider(
    pages: dict[int, list[dict[str, Any]]], total: int
) -> tuple[SpotifyProvider, AsyncMock]:
    """Return a Spotify provider serving the given pages keyed by offset, and its page fetch mock."""
    provider = object.__new__(SpotifyProvider)
    provider.config = MagicMock(instance_id="spotify--test")
    provider.manifest = MagicMock(domain="spotify")
    provider.logger = MagicMock()
    provider.mass = MagicMock()
    provider._get_cached_paginated_meta = AsyncMock(  # type: ignore[method-assign]
        return_value={"etag": "etag", "total": total}
    )

    async def _page(*_args: Any, offset: int, **_kwargs: Any) -> dict[str, Any]:
        return {"items": pages.get(offset, []), "total": total}

    fetch_page = AsyncMock(side_effect=_page)
    provider._get_data_with_caching = fetch_page  # type: ignore[method-assign]
    return provider, fetch_page


def _items(start: int, end: int) -> list[dict[str, Any]]:
    return [{"id": f"i{i}"} for i in range(start, end)]


async def test_short_page_mid_list_continues_until_total() -> None:
    """A short page before the reported total does not end the listing."""
    pages = {0: _items(0, 50), 50: _items(50, 99), 100: _items(100, 150)}
    provider, fetch_page = _make_provider(pages, total=150)

    items = [item async for item in provider._get_all_items("me/playlists")]

    assert [item["id"] for item in items] == [f"i{i}" for i in range(150) if i != 99]
    assert fetch_page.await_count == 3


async def test_short_page_ends_listing_when_total_unknown() -> None:
    """Without a reported total, a short page is treated as the last page."""
    pages = {0: _items(0, 50), 50: _items(50, 70), 100: _items(100, 150)}
    provider, fetch_page = _make_provider(pages, total=0)

    items = [item async for item in provider._get_all_items("me/playlists")]

    assert len(items) == 70
    assert fetch_page.await_count == 2
