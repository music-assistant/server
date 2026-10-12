"""Tests for Emby library listing validation."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import InvalidDataError

from music_assistant.providers.emby import SUPPORTED_FEATURES, EmbyProvider
from music_assistant.providers.emby.const import ITEM_KEY_ID, ITEM_KEY_MEDIA_STREAMS, ITEMS


@pytest.fixture
def provider() -> EmbyProvider:
    """Create an Emby provider with mocked dependencies."""
    mass = Mock()
    manifest = Mock()
    manifest.domain = "emby"
    config = Mock()
    config.instance_id = "emby--test123"
    config.name = "Emby Test"
    config.enabled = True
    config.get_value.return_value = "GLOBAL"
    result = EmbyProvider(mass, manifest, config, SUPPORTED_FEATURES)
    result._user_id = "user"
    return result


@pytest.mark.parametrize(
    "method_name",
    [
        "get_library_artists",
        "get_library_albums",
        "get_library_tracks",
        "get_library_playlists",
    ],
)
@pytest.mark.parametrize("response", [{}, {ITEMS: None}, {ITEMS: {}}])
async def test_malformed_item_list_raises(
    provider: EmbyProvider,
    method_name: str,
    response: dict[str, Any],
) -> None:
    """A missing or wrong-typed Items field must abort the library sync."""
    provider._get_music_libraries = AsyncMock(  # type: ignore[method-assign]
        return_value=[{ITEM_KEY_ID: "library"}]
    )
    provider._get = AsyncMock(return_value=response)  # type: ignore[method-assign]

    listing: Callable[[], AsyncGenerator[Any]] = getattr(provider, method_name)
    with pytest.raises(InvalidDataError):
        _ = [item async for item in listing()]


async def test_empty_item_list_is_a_valid_empty_library(provider: EmbyProvider) -> None:
    """An explicit empty Items list remains a valid empty library."""
    provider._get_music_libraries = AsyncMock(  # type: ignore[method-assign]
        return_value=[{ITEM_KEY_ID: "library"}]
    )
    provider._get = AsyncMock(return_value={ITEMS: []})  # type: ignore[method-assign]

    assert [track async for track in provider.get_library_tracks()] == []


@pytest.mark.parametrize(
    ("method_name", "media_type"),
    [
        ("get_library_artists", MediaType.ARTIST),
        ("get_library_albums", MediaType.ALBUM),
        ("get_library_tracks", MediaType.TRACK),
        ("get_library_playlists", MediaType.PLAYLIST),
    ],
)
@pytest.mark.parametrize("entry", [None, {}, {ITEM_KEY_ID: {}}])
async def test_non_object_entry_is_reported(
    provider: EmbyProvider,
    method_name: str,
    media_type: MediaType,
    entry: Any,
) -> None:
    """An unidentifiable malformed entry must hold back deletions for its listing."""
    provider._get_music_libraries = AsyncMock(  # type: ignore[method-assign]
        return_value=[{ITEM_KEY_ID: "library"}]
    )
    provider._get = AsyncMock(  # type: ignore[method-assign]
        side_effect=[{ITEMS: [entry]}, {ITEMS: []}]
    )
    provider.report_skipped_sync_item = Mock()  # type: ignore[method-assign]

    listing: Callable[[], AsyncGenerator[Any]] = getattr(provider, method_name)
    assert [item async for item in listing()] == []

    provider.report_skipped_sync_item.assert_called_once()
    args = provider.report_skipped_sync_item.call_args.args
    assert args[:2] == (media_type, None)
    assert isinstance(args[2], InvalidDataError)


@pytest.mark.parametrize("media_streams", [None, {}, []])
async def test_streamless_track_is_reported(provider: EmbyProvider, media_streams: Any) -> None:
    """A track without media streams must be reported instead of silently disappearing."""
    provider._get_music_libraries = AsyncMock(  # type: ignore[method-assign]
        return_value=[{ITEM_KEY_ID: "library"}]
    )
    provider._get = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            {
                ITEMS: [
                    {
                        ITEM_KEY_ID: "track-1",
                        ITEM_KEY_MEDIA_STREAMS: media_streams,
                    }
                ]
            },
            {ITEMS: []},
        ]
    )
    provider.report_skipped_sync_item = Mock()  # type: ignore[method-assign]

    assert [track async for track in provider.get_library_tracks()] == []

    args = provider.report_skipped_sync_item.call_args.args
    assert args[:2] == (MediaType.TRACK, "track-1")
    assert isinstance(args[2], InvalidDataError)


async def test_malformed_music_library_list_raises(provider: EmbyProvider) -> None:
    """A malformed media-folder response must not make every listing look empty."""
    provider._get = AsyncMock(return_value={})  # type: ignore[method-assign]

    with pytest.raises(InvalidDataError):
        await provider._get_music_libraries()


async def test_malformed_music_library_entry_raises(provider: EmbyProvider) -> None:
    """A non-object media folder must abort every listing that depends on it."""
    provider._get = AsyncMock(return_value={ITEMS: [None]})  # type: ignore[method-assign]

    with pytest.raises(InvalidDataError):
        await provider._get_music_libraries()
