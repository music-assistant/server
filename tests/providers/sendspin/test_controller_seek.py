"""Tests for sendspin controller seek / seek-relative event handling."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

from aiosendspin.server.roles import ControllerSeekEvent, ControllerSeekRelativeEvent

from music_assistant.providers.sendspin.player import SendspinPlayer


def _player(
    *, duration: float | None, elapsed: float = 0.0, has_item: bool = True
) -> tuple[SendspinPlayer, AsyncMock]:
    current_item = SimpleNamespace(duration=duration) if has_item else None
    queue = SimpleNamespace(
        queue_id="q1",
        current_item=current_item,
        corrected_elapsed_time=elapsed,
    )
    seek = AsyncMock()
    player_queues = SimpleNamespace(get_active_queue=lambda _pid: queue, seek=seek)
    fake = SimpleNamespace(player_id="p1", mass=SimpleNamespace(player_queues=player_queues))
    return cast("SendspinPlayer", fake), seek


async def test_absolute_seek_converts_ms_to_seconds() -> None:
    """An absolute seek forwards the position to the queue in seconds."""
    player, seek = _player(duration=100.0)
    await SendspinPlayer._handle_controller_event(player, ControllerSeekEvent(position_ms=5400))
    seek.assert_awaited_once_with("q1", 5)


async def test_absolute_seek_clamps_to_duration() -> None:
    """An absolute seek past the end clamps to the track duration."""
    player, seek = _player(duration=100.0)
    await SendspinPlayer._handle_controller_event(player, ControllerSeekEvent(position_ms=120_000))
    seek.assert_awaited_once_with("q1", 100)


async def test_absolute_seek_ignored_without_current_item() -> None:
    """An absolute seek is dropped when no item is loaded."""
    player, seek = _player(duration=None, has_item=False)
    await SendspinPlayer._handle_controller_event(player, ControllerSeekEvent(position_ms=5400))
    seek.assert_not_awaited()


async def test_absolute_seek_ignored_when_duration_unknown() -> None:
    """An absolute seek is dropped when the current item has no duration."""
    player, seek = _player(duration=None)
    await SendspinPlayer._handle_controller_event(player, ControllerSeekEvent(position_ms=5400))
    seek.assert_not_awaited()


async def test_relative_seek_clamps_to_duration() -> None:
    """A relative seek past the end clamps to the track duration."""
    player, seek = _player(duration=100.0, elapsed=95.0)
    await SendspinPlayer._handle_controller_event(
        player, ControllerSeekRelativeEvent(offset_ms=30_000)
    )
    seek.assert_awaited_once_with("q1", 100)


async def test_relative_seek_clamps_to_zero() -> None:
    """A relative seek before the start clamps to zero."""
    player, seek = _player(duration=100.0, elapsed=5.0)
    await SendspinPlayer._handle_controller_event(
        player, ControllerSeekRelativeEvent(offset_ms=-30_000)
    )
    seek.assert_awaited_once_with("q1", 0)


async def test_relative_seek_ignored_when_duration_unknown() -> None:
    """A relative seek is dropped when the current item has no duration."""
    player, seek = _player(duration=None, elapsed=10.0)
    await SendspinPlayer._handle_controller_event(
        player, ControllerSeekRelativeEvent(offset_ms=30_000)
    )
    seek.assert_not_awaited()
