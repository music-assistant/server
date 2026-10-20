"""
Tests for progress being frozen before the push stream is torn down (support#5813).

Pausing a Sendspin group falls back to STOP, and group.stop() can only snapshot
the live position while the group still has an active stream. See the ordering
comment in SendspinPlayer.stop() for why cancelling the session first breaks it.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.player import PlayerMedia

from music_assistant.providers.sendspin.player import SendspinPlayer


def _player_mock() -> MagicMock:
    """Create a mock with the real method under test bound to it."""
    mock = MagicMock()
    mock._clear_current_media_metadata = AsyncMock()
    mock.playback_session.cancel = AsyncMock()
    mock.api.group.stop = AsyncMock()
    return mock


async def test_stop_freezes_progress_while_transport_is_still_live() -> None:
    """group.stop() must be awaited before the push stream is torn down."""
    mock = _player_mock()
    transport = SimpleNamespace(stream_active=True)
    stream_active_at_freeze: list[bool] = []

    async def group_stop(**_kwargs: object) -> None:
        stream_active_at_freeze.append(transport.stream_active)
        transport.stream_active = False

    async def session_cancel(_reason: str) -> None:
        transport.stream_active = False

    mock.api.group.stop = group_stop
    mock.playback_session.cancel = session_cancel

    await SendspinPlayer.stop(mock)

    assert stream_active_at_freeze == [True]


async def test_stop_still_cancels_the_playback_session() -> None:
    """Freezing first must not drop the session teardown."""
    mock = _player_mock()

    await SendspinPlayer.stop(mock)

    mock.playback_session.cancel.assert_awaited_once_with("stop command")
    mock.api.group.stop.assert_awaited_once()


async def test_session_is_cancelled_even_if_the_group_stop_fails() -> None:
    """A failing group stop must not strand the playback task and its pipelines."""
    mock = _player_mock()
    mock.api.group.stop = AsyncMock(side_effect=RuntimeError("transport gone"))

    with pytest.raises(RuntimeError):
        await SendspinPlayer.stop(mock)

    mock.playback_session.cancel.assert_awaited_once_with("stop command")


async def test_stop_keeps_a_parked_item_frozen() -> None:
    """A stop that leaves current media keeps its metadata frozen, but no beats."""
    mock = _player_mock()
    mock.state.current_media = PlayerMedia(uri="track-1", queue_item_id="item-1")
    mock._published_item = ("track-1", "item-1")

    await SendspinPlayer.stop(mock)

    mock._clear_current_media_metadata.assert_not_awaited()
    mock.api.group.stop.assert_awaited_once_with(keep_progress=True)
    mock._visualizer_role.clear_beat_schedule.assert_called_once()
    mock.mass.create_task.assert_not_called()


async def test_stop_republishes_when_the_display_shows_another_item() -> None:
    """A stop that cancelled the push of a new item publishes that item."""
    mock = _player_mock()
    mock.synced_to = None
    mock.state.current_media = PlayerMedia(uri="track-2", queue_item_id="item-2")
    mock._published_item = ("track-1", "item-1")

    await SendspinPlayer.stop(mock)

    mock.mass.create_task.assert_called_once()
    assert (
        mock.mass.create_task.call_args.kwargs["task_id"] == f"sendspin_metadata_{mock.player_id}"
    )


@pytest.mark.parametrize(
    ("synced_to", "published_item"),
    [("leader", ("track-1", "item-1")), (None, None)],
    ids=["synced_member", "nothing_published"],
)
async def test_stop_republishes_nothing_where_nothing_was_shown(
    synced_to: str | None, published_item: tuple[str, str] | None
) -> None:
    """A synced member or an empty display gets no metadata from the stop."""
    mock = _player_mock()
    mock.synced_to = synced_to
    mock.state.current_media = PlayerMedia(uri="track-2", queue_item_id="item-2")
    mock._published_item = published_item

    await SendspinPlayer.stop(mock)

    mock.mass.create_task.assert_not_called()


async def test_stop_without_current_media_clears_metadata() -> None:
    """Nothing left to show clears the metadata."""
    mock = _player_mock()
    mock.state.current_media = None

    await SendspinPlayer.stop(mock)

    mock._clear_current_media_metadata.assert_awaited_once()
