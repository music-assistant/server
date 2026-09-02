"""Tests for the Spotify library albums listing."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from music_assistant.providers.spotify.provider import SpotifyProvider


def _make_provider(instance_id: str = "spotify--test") -> SpotifyProvider:
    """Return a Spotify provider with only what the library listing needs."""
    provider = object.__new__(SpotifyProvider)
    provider.config = MagicMock(instance_id=instance_id)
    provider.manifest = MagicMock(domain="spotify")
    provider.logger = MagicMock()
    provider.mass = MagicMock()
    return provider


def _saved_album(album_id: str) -> dict[str, Any]:
    """Return a saved album entry as returned by the me/albums endpoint."""
    return {
        "added_at": "2026-01-01T00:00:00Z",
        "album": {
            "id": album_id,
            "name": album_id,
            "album_type": "album",
            "external_urls": {"spotify": f"https://open.spotify.com/album/{album_id}"},
            "artists": [],
            "images": [],
        },
    }


async def test_library_albums_skips_null_entries() -> None:
    """
    A null entry in the me/albums response is skipped instead of aborting the sync.

    Spotify returns such an entry for an album the account can no longer resolve, which
    would otherwise crash the whole album sync.
    """
    provider = _make_provider()
    # a valid album after the empty ones, so stopping at an empty entry fails this test
    pages: list[dict[str, Any]] = [
        {
            "items": [
                _saved_album("album1"),
                None,
                {"added_at": "2026-01-01T00:00:00Z", "album": None},
                _saved_album("album2"),
            ],
            "total": 4,
        }
    ]
    provider._get_cached_paginated_meta = AsyncMock(return_value={"etag": "etag", "total": 4})  # type: ignore[method-assign]
    provider._get_data_with_caching = AsyncMock(side_effect=pages)  # type: ignore[method-assign]

    albums = [album async for album in provider.get_library_albums()]

    assert [album.item_id for album in albums] == ["album1", "album2"]
