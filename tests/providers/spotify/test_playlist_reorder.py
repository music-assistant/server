"""Tests for Spotify playlist track reordering."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, call

import aiohttp
import pytest
from music_assistant_models.errors import InvalidDataError

from music_assistant.helpers.playlists import move_playlist_block
from music_assistant.providers.spotify.provider import SpotifyProvider

PLAYLIST_ID = "playlist"
ENDPOINT = f"playlists/{PLAYLIST_ID}/items"


def _make_provider(total: int = 10) -> SpotifyProvider:
    """Return a Spotify provider with mocked playlist API calls."""
    provider = object.__new__(SpotifyProvider)
    provider.logger = MagicMock()
    provider._put_data = AsyncMock()  # type: ignore[method-assign]
    provider._get_data = AsyncMock(return_value={"total": total})  # type: ignore[method-assign]
    tracks = [MagicMock(position=i) for i in range(1, total + 1)]
    provider.get_playlist_tracks = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda _playlist_id, page=0: tracks if page == 0 else []
    )
    return provider


def _put(range_start: int, range_length: int, insert_before: int) -> Any:
    """Return the expected reorder request."""
    data = {
        "range_start": range_start,
        "range_length": range_length,
        "insert_before": insert_before,
    }
    return call(ENDPOINT, data=data)


def _apply_spotify_move(items: list[int], data: dict[str, int]) -> list[int]:
    """Apply a reorder request the way Spotify does, insert_before counted before removal."""
    start, length, before = data["range_start"], data["range_length"], data["insert_before"]
    block = items[start : start + length]
    rest = items[:start] + items[start + length :]
    index = before if before <= start else before - length
    return rest[:index] + block + rest[index:]


async def test_single_move_down() -> None:
    """Moving a track down inserts before the track after its new spot."""
    provider = _make_provider()
    await provider.move_playlist_track(PLAYLIST_ID, 2, 5)
    assert provider._put_data.await_args_list == [_put(1, 1, 5)]  # type: ignore[attr-defined]


async def test_single_move_up() -> None:
    """Moving a track up inserts before the track at its new spot."""
    provider = _make_provider()
    await provider.move_playlist_track(PLAYLIST_ID, 5, 2)
    assert provider._put_data.await_args_list == [_put(4, 1, 1)]  # type: ignore[attr-defined]


async def test_contiguous_block_moves_in_one_request() -> None:
    """A contiguous block moves down and up with one request each."""
    provider = _make_provider()
    await provider.move_playlist_tracks(PLAYLIST_ID, [2, 3, 4], 6)
    await provider.move_playlist_tracks(PLAYLIST_ID, [6, 7], 2)
    assert provider._put_data.await_args_list == [  # type: ignore[attr-defined]
        _put(1, 3, 8),
        _put(5, 2, 1),
    ]
    provider._get_data.assert_not_awaited()  # type: ignore[attr-defined]


async def test_contiguous_block_to_end_reads_total_once() -> None:
    """Moving a contiguous block to the end reads the playlist size once."""
    provider = _make_provider(total=10)
    await provider.move_playlist_tracks(PLAYLIST_ID, [2, 3])
    provider._get_data.assert_awaited_once_with(ENDPOINT, limit=1, fields="total")  # type: ignore[attr-defined]
    assert provider._put_data.await_args_list == [_put(1, 2, 10)]  # type: ignore[attr-defined]


async def test_block_already_in_place_sends_nothing() -> None:
    """A block that is already at the target position sends no request."""
    provider = _make_provider(total=10)
    await provider.move_playlist_tracks(PLAYLIST_ID, [9, 10])
    await provider.move_playlist_tracks(PLAYLIST_ID, [3, 4], 3)
    provider._put_data.assert_not_awaited()  # type: ignore[attr-defined]


async def test_block_that_does_not_fit_is_rejected() -> None:
    """A block that does not fit in the playlist is rejected before any change."""
    provider = _make_provider(total=10)
    with pytest.raises(InvalidDataError):
        await provider.move_playlist_tracks(PLAYLIST_ID, [11, 12])
    provider._put_data.assert_not_awaited()  # type: ignore[attr-defined]


async def test_non_contiguous_positions_move_one_by_one() -> None:
    """Positions that are not one run use the shared one-by-one fallback."""
    provider = _make_provider(total=5)
    await provider.move_playlist_tracks(PLAYLIST_ID, [1, 3], 4)
    assert provider._put_data.await_count == 2  # type: ignore[attr-defined]


async def test_refused_move_raises_invalid_data() -> None:
    """A move Spotify refuses as a bad request raises InvalidDataError."""
    provider = _make_provider()
    provider._put_data.side_effect = aiohttp.ClientResponseError(  # type: ignore[attr-defined]
        MagicMock(), (), status=400
    )
    with pytest.raises(InvalidDataError):
        await provider.move_playlist_track(PLAYLIST_ID, 2, 50)


@pytest.mark.parametrize(
    ("positions", "to_position"),
    [
        ([2], 5),
        ([5], 2),
        ([2, 3, 4], 6),
        ([6, 7], 2),
        ([1, 2], 9),
        ([9, 10], 1),
        ([4, 5], 4),
        ([1, 3], 4),
        ([2, 7, 9], 3),
    ],
)
async def test_requests_match_block_move_result(positions: list[int], to_position: int) -> None:
    """The requests give the same order as moving the block directly."""
    provider = _make_provider()
    items = list(range(1, 11))
    expected = move_playlist_block(items, positions, to_position)
    await provider.move_playlist_tracks(PLAYLIST_ID, positions, to_position)
    result = items
    for sent in provider._put_data.await_args_list:  # type: ignore[attr-defined]
        result = _apply_spotify_move(result, sent.kwargs["data"])
    assert result == expected
