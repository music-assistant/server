"""Tests for the Spotify library tracks listing."""

from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import MagicMock

from music_assistant.providers.spotify.provider import SpotifyProvider


def _make_provider(instance_id: str = "spotify--test") -> SpotifyProvider:
    """Return a Spotify provider with only what the library listing needs."""
    provider = object.__new__(SpotifyProvider)
    provider.config = MagicMock(instance_id=instance_id)
    provider.manifest = MagicMock(domain="spotify")
    provider.logger = MagicMock()
    provider.mass = MagicMock()
    return provider


def _saved_track(track_id: str) -> dict[str, Any]:
    """Return a saved track entry as returned by the me/tracks endpoint."""
    return {
        "added_at": "2026-01-01T00:00:00Z",
        "track": {
            "id": track_id,
            "name": track_id,
            "duration_ms": 180000,
            "external_urls": {"spotify": f"https://open.spotify.com/track/{track_id}"},
            "is_local": False,
            "is_playable": True,
            "explicit": False,
        },
    }


async def test_library_tracks_skips_entry_without_track() -> None:
    """
    A saved track entry that carries no track at all is skipped.

    Spotify returns such an entry for a track that is no longer available, which would
    otherwise abort the whole track sync.
    """
    provider = _make_provider()
    items: list[Any] = [_saved_track("track1"), {"added_at": "x", "track": None}, None]

    async def _iter_items() -> AsyncGenerator[Any]:
        for item in items:
            yield item

    provider._get_all_items = MagicMock(return_value=_iter_items())  # type: ignore[method-assign]

    tracks = [track async for track in provider.get_library_tracks()]

    assert [track.item_id for track in tracks] == ["track1"]
