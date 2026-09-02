"""Tests for the Recently played built-in playlist."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import ItemMapping, Track

from music_assistant.providers.builtin import BuiltinProvider
from tests.common import use_real_create_task


def _make_provider() -> BuiltinProvider:
    """Return a BuiltinProvider instance with a minimal mock mass."""
    provider = BuiltinProvider.__new__(BuiltinProvider)
    provider.mass = MagicMock()
    # instance_id and domain are read-only properties backed by config/manifest
    provider.config = MagicMock()
    provider.config.instance_id = "builtin"
    provider.manifest = MagicMock()
    provider.manifest.domain = "builtin"
    return provider


def _install_cache_mocks(provider: BuiltinProvider) -> None:
    """Make the @use_cache decorator treat every call as a cache miss."""
    provider.mass.cache.get_with_freshness = AsyncMock(  # type: ignore[method-assign]
        return_value=(None, False, False)
    )
    provider.mass.cache.set = AsyncMock()  # type: ignore[method-assign]
    use_real_create_task(provider.mass)


@pytest.mark.asyncio
async def test_recently_played_resolves_library_rows() -> None:
    """A playlog row for a library-originated play is resolved to its library track."""
    provider = _make_provider()
    _install_cache_mocks(provider)
    library_track = Track(
        item_id="42",
        provider="library",
        name="Library Track",
        provider_mappings=set(),
    )
    provider.mass.music.tracks.get_library_item = AsyncMock(return_value=library_track)  # type: ignore[method-assign]
    provider.mass.music.recently_played = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            ItemMapping(
                item_id="42",
                provider="library",
                name="Library Track",
                media_type=MediaType.TRACK,
            )
        ]
    )

    result = await provider._get_builtin_playlist_recently_played()

    provider.mass.music.tracks.get_library_item.assert_awaited_once_with("42")
    assert len(result) == 1
    # use_cache clones the flight result, so compare identity fields rather than object identity
    assert result[0].item_id == library_track.item_id
    assert result[0].provider == "library"
    assert result[0].position == 1


@pytest.mark.asyncio
async def test_recently_played_skips_library_row_removed_from_library() -> None:
    """A library row for a track since removed from the library is skipped, not raised."""
    provider = _make_provider()
    _install_cache_mocks(provider)
    provider.mass.music.tracks.get_library_item = AsyncMock(  # type: ignore[method-assign]
        side_effect=MediaNotFoundError("gone")
    )
    provider.mass.music.recently_played = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            ItemMapping(
                item_id="99",
                provider="library",
                name="Deleted Track",
                media_type=MediaType.TRACK,
            )
        ]
    )

    result = await provider._get_builtin_playlist_recently_played()

    assert result == []


@pytest.mark.asyncio
async def test_recently_played_builds_stub_track_for_real_provider() -> None:
    """A row naming a real provider instance still yields a stub Track for that provider."""
    provider = _make_provider()
    _install_cache_mocks(provider)
    item_provider = MagicMock()
    item_provider.domain = "spotify"
    item_provider.instance_id = "spotify--test"
    provider.mass.get_provider = MagicMock(return_value=item_provider)  # type: ignore[method-assign]
    provider.mass.music.recently_played = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            ItemMapping(
                item_id="track123",
                provider="spotify--test",
                name="Streamed Track",
                media_type=MediaType.TRACK,
            )
        ]
    )

    result = await provider._get_builtin_playlist_recently_played()

    assert len(result) == 1
    track = result[0]
    assert track.item_id == "track123"
    assert track.name == "Streamed Track"
    assert track.position == 1
    mapping = next(iter(track.provider_mappings))
    assert mapping.provider_domain == "spotify"
    assert mapping.provider_instance == "spotify--test"


@pytest.mark.asyncio
async def test_recently_played_skips_row_for_unknown_provider() -> None:
    """A row naming a provider instance that is no longer registered is dropped."""
    provider = _make_provider()
    _install_cache_mocks(provider)
    provider.mass.get_provider = MagicMock(return_value=None)  # type: ignore[method-assign]
    provider.mass.music.recently_played = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            ItemMapping(
                item_id="track123",
                provider="removed_provider",
                name="Orphaned Track",
                media_type=MediaType.TRACK,
            )
        ]
    )

    result = await provider._get_builtin_playlist_recently_played()

    assert result == []
