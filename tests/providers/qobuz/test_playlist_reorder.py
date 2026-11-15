"""Test Qobuz playlist track reordering."""

from collections.abc import Sequence
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.errors import InvalidDataError

from music_assistant.helpers.playlists import move_playlist_block
from music_assistant.providers.qobuz import SUPPORTED_FEATURES, QobuzProvider

LENGTH = 6


def _entry_id(position: int) -> int:
    """Return the playlist entry id used for a 1-based position in the fixture playlist."""
    return position * 10


@pytest.fixture
def provider() -> QobuzProvider:
    """Create a QobuzProvider with a six track playlist and mocked API calls."""
    config = Mock()
    config.instance_id = "qobuz_test"
    config.name = "Qobuz Test"
    config.enabled = True
    config.get_value.side_effect = lambda key, default=None: {
        "quality": "27",
        "log_level": "GLOBAL",
    }.get(key, default)
    manifest = Mock()
    manifest.domain = "qobuz"
    provider = QobuzProvider(Mock(), manifest, config, SUPPORTED_FEATURES)
    items = [{"id": 1000 + pos, "playlist_track_id": _entry_id(pos)} for pos in range(1, 7)]
    provider._get_all_items = AsyncMock(return_value=items)  # type: ignore[method-assign]
    provider._get_data = AsyncMock(return_value={})  # type: ignore[method-assign]
    return provider


def _sent(provider: QobuzProvider) -> dict[str, object]:
    """Return the parameters of the single update request."""
    get_data = cast("AsyncMock", provider._get_data)
    get_data.assert_awaited_once()
    assert get_data.await_args is not None
    assert get_data.await_args.args == ("playlist/updateTracksPosition",)
    return dict(get_data.await_args.kwargs)


async def test_move_track_down(provider: QobuzProvider) -> None:
    """Moving track 2 to position 5 inserts it before the track now at position 6."""
    await provider.move_playlist_track("pl1", 2, 5)
    assert _sent(provider) == {
        "playlist_id": "pl1",
        "playlist_track_ids": "20",
        "insert_before": 6,
    }


async def test_unavailable_entry_keeps_its_slot(provider: QobuzProvider) -> None:
    """An entry without a track id still counts, so positions match the listing."""
    get_all_items = cast("AsyncMock", provider._get_all_items)
    get_all_items.return_value[0]["id"] = None
    await provider.move_playlist_track("pl1", 3, 2)
    assert _sent(provider)["playlist_track_ids"] == "30"


async def test_move_track_up(provider: QobuzProvider) -> None:
    """Moving track 5 to position 2 inserts it before the track at position 2."""
    await provider.move_playlist_track("pl1", 5, 2)
    assert _sent(provider) == {
        "playlist_id": "pl1",
        "playlist_track_ids": "50",
        "insert_before": 2,
    }


async def test_move_scattered_tracks_in_one_call(provider: QobuzProvider) -> None:
    """Scattered tracks are sent in playlist order in a single request."""
    await provider.move_playlist_tracks("pl1", [5, 1, 3], 2)
    assert _sent(provider) == {
        "playlist_id": "pl1",
        "playlist_track_ids": "10,30,50",
        "insert_before": 4,
    }


async def test_move_to_end(provider: QobuzProvider) -> None:
    """Without a target position the tracks go to the end of the playlist."""
    await provider.move_playlist_tracks("pl1", [1, 2])
    assert _sent(provider) == {
        "playlist_id": "pl1",
        "playlist_track_ids": "10,20",
        "insert_before": LENGTH + 1,
    }


async def test_block_already_in_place_sends_nothing(provider: QobuzProvider) -> None:
    """A block that already sits at the target position needs no request."""
    await provider.move_playlist_tracks("pl1", [3, 4], 3)
    cast("AsyncMock", provider._get_data).assert_not_awaited()


@pytest.mark.parametrize(("positions", "to_position"), [([7], 1), ([1, 2], 6), ([0], 1)])
async def test_out_of_range_raises(
    provider: QobuzProvider, positions: list[int], to_position: int
) -> None:
    """Positions or targets outside the playlist are rejected without a request."""
    with pytest.raises(InvalidDataError):
        await provider.move_playlist_tracks("pl1", positions, to_position)
    cast("AsyncMock", provider._get_data).assert_not_awaited()


@pytest.mark.parametrize(
    ("positions", "to_position"),
    [
        ([2], 5),
        ([5], 2),
        ([1], 6),
        ([6], 1),
        ([2, 3], 4),
        ([4, 5], 1),
        ([1, 3, 5], 2),
        ([2, 6], 1),
        ([1, 4], 5),
        ([2, 4, 6], 4),
        ([1, 6], 3),
    ],
)
async def test_request_matches_block_move(
    provider: QobuzProvider, positions: Sequence[int], to_position: int
) -> None:
    """Applying the sent request gives the same playlist as the block move."""
    original = [_entry_id(pos) for pos in range(1, LENGTH + 1)]
    await provider.move_playlist_tracks("pl1", positions, to_position)
    sent = _sent(provider)
    moved = [int(entry) for entry in str(sent["playlist_track_ids"]).split(",")]
    insert_before = int(str(sent["insert_before"]))
    rest = [entry for entry in original if entry not in moved]
    index = len(rest) if insert_before == LENGTH + 1 else rest.index(original[insert_before - 1])
    assert rest[:index] + moved + rest[index:] == move_playlist_block(
        original, positions, to_position
    )
