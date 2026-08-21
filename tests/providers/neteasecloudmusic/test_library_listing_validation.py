"""Tests for malformed NetEase library listing responses."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import InvalidDataError

from music_assistant.providers.neteasecloudmusic import NeteaseCloudMusicProvider


@pytest.mark.parametrize(
    ("method_name", "response"),
    [
        ("get_library_artists", {"data": {"unexpected": []}}),
        ("get_library_albums", {"data": {"unexpected": []}}),
        ("get_library_tracks", {"unexpected": []}),
        ("get_library_playlists", {"unexpected": []}),
    ],
)
async def test_malformed_listing_field_raises(
    provider: NeteaseCloudMusicProvider,
    method_name: str,
    response: dict[str, Any],
) -> None:
    """A missing item list must abort the sync instead of looking empty."""
    provider._client = AsyncMock()
    provider._client.get.return_value = response

    listing: Callable[[], AsyncGenerator[Any]] = getattr(provider, method_name)
    with pytest.raises(InvalidDataError):
        _ = [item async for item in listing()]


@pytest.mark.parametrize(
    ("method_name", "response", "media_type"),
    [
        ("get_library_artists", {"artists": [None]}, MediaType.ARTIST),
        ("get_library_albums", {"albums": [None]}, MediaType.ALBUM),
        ("get_library_playlists", {"playlist": [None]}, MediaType.PLAYLIST),
    ],
)
async def test_malformed_listing_entry_is_reported(
    provider: NeteaseCloudMusicProvider,
    method_name: str,
    response: dict[str, Any],
    media_type: MediaType,
) -> None:
    """An unidentifiable malformed entry must hold back deletions for the listing."""
    provider._client = AsyncMock()
    provider._client.get.return_value = response
    provider.report_skipped_sync_item = Mock()  # type: ignore[method-assign]

    listing: Callable[[], AsyncGenerator[Any]] = getattr(provider, method_name)
    assert [item async for item in listing()] == []

    provider.report_skipped_sync_item.assert_called_once()
    args = provider.report_skipped_sync_item.call_args.args
    assert args[:2] == (media_type, None)
    assert isinstance(args[2], InvalidDataError)


async def test_malformed_liked_track_id_is_reported(
    provider: NeteaseCloudMusicProvider,
) -> None:
    """An invalid liked-track id must not silently disappear from the authoritative list."""
    provider._client = AsyncMock()
    provider._client.get.return_value = {"ids": [None]}
    provider.report_skipped_sync_item = Mock()  # type: ignore[method-assign]

    assert [track async for track in provider.get_library_tracks()] == []

    provider.report_skipped_sync_item.assert_called_once()
    args = provider.report_skipped_sync_item.call_args.args
    assert args[:2] == (MediaType.TRACK, None)
    assert isinstance(args[2], InvalidDataError)
