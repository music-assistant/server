"""Tests for the Recently played built-in playlist."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import ItemMapping, Track

from music_assistant.providers.builtin import BuiltinProvider


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


@pytest.mark.asyncio
async def test_recently_played_resolves_library_rows() -> None:
    """A playlog row for a library-originated play is resolved to its library track."""
    provider = _make_provider()
    library_track = Track(
        item_id="42",
        provider="library",
        name="Library Track",
        provider_mappings=set(),
    )
    provider.mass.music.tracks.get_library_items_by_query = AsyncMock(  # type: ignore[method-assign]
        return_value=[library_track]
    )
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

    provider.mass.music.tracks.get_library_items_by_query.assert_awaited_once_with(
        extra_query_parts=["tracks.item_id IN :item_ids"],
        extra_query_params={"item_ids": [42]},
        in_library_only=False,
    )
    assert result == [library_track]
    assert library_track.position == 1


@pytest.mark.asyncio
async def test_recently_played_skips_library_row_removed_from_library() -> None:
    """A library row is skipped when the batched lookup returns no track for it."""
    provider = _make_provider()
    provider.mass.music.tracks.get_library_items_by_query = AsyncMock(  # type: ignore[method-assign]
        return_value=[]
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
async def test_recently_played_skips_library_query_when_no_library_rows() -> None:
    """The batched library query is skipped entirely when no row is library-originated."""
    provider = _make_provider()
    item_provider = MagicMock()
    item_provider.domain = "spotify"
    item_provider.instance_id = "spotify--test"
    provider.mass.get_provider = MagicMock(return_value=item_provider)  # type: ignore[method-assign]
    provider.mass.music.tracks.get_library_items_by_query = AsyncMock()  # type: ignore[method-assign]
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

    provider.mass.music.tracks.get_library_items_by_query.assert_not_awaited()
    assert len(result) == 1
    assert result[0].item_id == "track123"


@pytest.mark.asyncio
async def test_recently_played_rereads_playlog_on_every_call() -> None:
    """Consecutive calls each re-read the playlog instead of sharing a cached result."""
    provider = _make_provider()
    first = ItemMapping(item_id="1", provider="library", name="First", media_type=MediaType.TRACK)
    second = ItemMapping(item_id="2", provider="library", name="Second", media_type=MediaType.TRACK)
    provider.mass.music.recently_played = AsyncMock(  # type: ignore[method-assign]
        side_effect=[[first], [second]]
    )
    provider.mass.music.tracks.get_library_items_by_query = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            [Track(item_id="1", provider="library", name="First", provider_mappings=set())],
            [Track(item_id="2", provider="library", name="Second", provider_mappings=set())],
        ]
    )

    first_result = await provider._get_builtin_playlist_recently_played()
    second_result = await provider._get_builtin_playlist_recently_played()

    assert provider.mass.music.recently_played.await_count == 2
    assert [t.item_id for t in first_result] == ["1"]
    assert [t.item_id for t in second_result] == ["2"]


@pytest.mark.asyncio
async def test_recently_played_builds_stub_track_for_real_provider() -> None:
    """A row naming a real provider instance still yields a stub Track for that provider."""
    provider = _make_provider()
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
