"""Tests for Infinite Mix dynamic built-in playlists."""

from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.media_items import ProviderMapping, Track

from music_assistant.helpers.track_filter import track_filter
from music_assistant.providers.builtin import BuiltinProvider
from music_assistant.providers.builtin.constants import (
    ALL_FAVORITE_TRACKS,
    BUILTIN_PLAYLISTS,
    DYNAMIC_BUILTIN_PLAYLISTS,
    INFINITE_MIX,
    INFINITE_MIX_FAVORITES,
    RANDOM_TRACKS,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_infinite_mix_ids_in_builtin_playlists() -> None:
    """Both Infinite Mix IDs must be registered in BUILTIN_PLAYLISTS."""
    assert INFINITE_MIX in BUILTIN_PLAYLISTS
    assert INFINITE_MIX_FAVORITES in BUILTIN_PLAYLISTS


def test_dynamic_builtin_playlists_membership() -> None:
    """DYNAMIC_BUILTIN_PLAYLISTS contains exactly the two Infinite Mix variants."""
    assert INFINITE_MIX in DYNAMIC_BUILTIN_PLAYLISTS
    assert INFINITE_MIX_FAVORITES in DYNAMIC_BUILTIN_PLAYLISTS
    # Static playlists must NOT be in the dynamic set.
    assert RANDOM_TRACKS not in DYNAMIC_BUILTIN_PLAYLISTS
    assert ALL_FAVORITE_TRACKS not in DYNAMIC_BUILTIN_PLAYLISTS


# ---------------------------------------------------------------------------
# get_playlist() — is_dynamic flag
# ---------------------------------------------------------------------------


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
@pytest.mark.parametrize("playlist_id", [INFINITE_MIX, INFINITE_MIX_FAVORITES])
async def test_get_playlist_is_dynamic_true_for_infinite_mix(playlist_id: str) -> None:
    """get_playlist() must set is_dynamic=True for Infinite Mix playlists."""
    provider = _make_provider()
    playlist = await provider.get_playlist(playlist_id)
    assert playlist.is_dynamic is True


@pytest.mark.asyncio
@pytest.mark.parametrize("playlist_id", [ALL_FAVORITE_TRACKS, RANDOM_TRACKS])
async def test_get_playlist_is_dynamic_false_for_static_playlists(playlist_id: str) -> None:
    """get_playlist() must set is_dynamic=False for non-dynamic built-in playlists."""
    provider = _make_provider()
    playlist = await provider.get_playlist(playlist_id)
    assert playlist.is_dynamic is False


# ---------------------------------------------------------------------------
# _get_builtin_playlist_infinite_mix — track fetching
# ---------------------------------------------------------------------------


def _make_tracks(n: int) -> list[Track]:
    """Return n minimal Track objects."""
    return cast("list[Track]", [MagicMock(spec=Track) for _ in range(n)])


@pytest.mark.asyncio
async def test_infinite_mix_returns_25_tracks_with_positions() -> None:
    """_get_builtin_playlist_infinite_mix returns 25 tracks with 1-based positions."""
    provider = _make_provider()
    fake_tracks = _make_tracks(25)
    provider.mass.music.tracks.library_items = AsyncMock(return_value=fake_tracks)  # type: ignore[method-assign]

    result = await provider._get_builtin_playlist_infinite_mix()

    provider.mass.music.tracks.library_items.assert_called_once_with(
        favorite=None, limit=25, order_by="random", summary=False
    )
    assert len(result) == 25
    for expected_pos, track in enumerate(result, 1):
        assert track.position == expected_pos


@pytest.mark.asyncio
async def test_infinite_mix_favorites_passes_favorite_filter() -> None:
    """_get_builtin_playlist_infinite_mix_favorites passes favorite=True to library_items."""
    provider = _make_provider()
    fake_tracks = _make_tracks(25)
    provider.mass.music.tracks.library_items = AsyncMock(return_value=fake_tracks)  # type: ignore[method-assign]

    result = await provider._get_builtin_playlist_infinite_mix_favorites()

    provider.mass.music.tracks.library_items.assert_called_once_with(
        favorite=True, limit=25, order_by="random", summary=False
    )
    assert len(result) == 25
    for expected_pos, track in enumerate(result, 1):
        assert track.position == expected_pos


@pytest.mark.asyncio
async def test_infinite_mix_empty_library_returns_empty_list() -> None:
    """_get_builtin_playlist_infinite_mix handles an empty library gracefully."""
    provider = _make_provider()
    provider.mass.music.tracks.library_items = AsyncMock(return_value=[])  # type: ignore[method-assign]

    result = await provider._get_builtin_playlist_infinite_mix()

    assert result == []


# ---------------------------------------------------------------------------
# _infinite_mix_tracks — recency track filter
# ---------------------------------------------------------------------------


def _real_tracks(item_ids: list[str]) -> list[Track]:
    """Return real Track objects so a filter predicate can read item_id."""
    return [
        Track(
            item_id=item_id,
            provider="test",
            name=item_id,
            provider_mappings={
                ProviderMapping(item_id=item_id, provider_domain="test", provider_instance="test")
            },
        )
        for item_id in item_ids
    ]


@pytest.mark.asyncio
async def test_infinite_mix_over_fetches_and_drops_filtered_tracks() -> None:
    """With a recency filter active, the mix over-fetches (3x) and drops rejected tracks."""
    provider = _make_provider()
    pool = _real_tracks([str(i) for i in range(75)])
    provider.mass.music.tracks.library_items = AsyncMock(return_value=pool)  # type: ignore[method-assign]

    # reject odd-numbered tracks; the even ones remain and are trimmed back to the mix size
    with track_filter(lambda track: int(track.item_id) % 2 == 0):
        result = await provider._get_builtin_playlist_infinite_mix()

    provider.mass.music.tracks.library_items.assert_called_once_with(
        favorite=None, limit=75, order_by="random", summary=False
    )
    assert len(result) == 25
    assert all(int(track.item_id) % 2 == 0 for track in result)
    for expected_pos, track in enumerate(result, 1):
        assert track.position == expected_pos


@pytest.mark.asyncio
async def test_infinite_mix_filter_rejecting_all_falls_back_to_pool() -> None:
    """If the filter rejects every track, the mix still returns a full batch (never empty)."""
    provider = _make_provider()
    pool = _real_tracks([str(i) for i in range(75)])
    provider.mass.music.tracks.library_items = AsyncMock(return_value=pool)  # type: ignore[method-assign]

    with track_filter(lambda _track: False):
        result = await provider._get_builtin_playlist_infinite_mix()

    assert len(result) == 25
