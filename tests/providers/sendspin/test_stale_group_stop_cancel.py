"""
Tests for the deferred group-stopped cancel guard (support#5771).

The group-stopped event schedules a deferred cancel of the playback session.
When a new play_media starts a fresh session before that deferred task runs,
the stale cancel must not tear down the new session — doing so left the client
stranded in "playing" state with no audio stream.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from music_assistant.providers.sendspin.player import SendspinPlayer


def _make_player_mock() -> MagicMock:
    """Create a mock with the real methods under test bound to it."""
    mock = MagicMock()
    mock.synced_to = None
    mock.playback_session.cancel = AsyncMock()
    return mock


def test_group_stopped_binds_cancel_to_current_task() -> None:
    """The deferred cancel is bound to the session task live at event time."""
    mock = _make_player_mock()
    task = MagicMock()
    task.done.return_value = False
    mock.playback_session.playback_task = task

    SendspinPlayer._on_group_stopped(mock)

    mock._cancel_stale_playback_session.assert_called_once_with(task)
    mock.mass.create_task.assert_called_once()


def test_group_stopped_without_session_schedules_nothing() -> None:
    """No live session task means nothing to cancel."""
    mock = _make_player_mock()
    mock.playback_session.playback_task = None

    SendspinPlayer._on_group_stopped(mock)

    mock.mass.create_task.assert_not_called()


def test_group_stopped_with_finished_session_schedules_nothing() -> None:
    """A finished session task means nothing to cancel."""
    mock = _make_player_mock()
    task = MagicMock()
    task.done.return_value = True
    mock.playback_session.playback_task = task

    SendspinPlayer._on_group_stopped(mock)

    mock.mass.create_task.assert_not_called()


def test_group_stopped_as_synced_follower_schedules_nothing() -> None:
    """Synced followers do not own the playback session."""
    mock = _make_player_mock()
    mock.synced_to = "leader_id"
    task = MagicMock()
    task.done.return_value = False
    mock.playback_session.playback_task = task

    SendspinPlayer._on_group_stopped(mock)

    mock.mass.create_task.assert_not_called()


async def test_stale_cancel_skips_newer_session() -> None:
    """A cancel bound to a superseded task must not touch the new session."""
    mock = _make_player_mock()
    stale_task = MagicMock()
    mock.playback_session.playback_task = MagicMock()  # a newer session task

    await SendspinPlayer._cancel_stale_playback_session(mock, stale_task)

    mock.playback_session.cancel.assert_not_awaited()


async def test_stale_cancel_cancels_still_active_session() -> None:
    """A cancel bound to the still-active task cancels the session."""
    mock = _make_player_mock()
    task = MagicMock()
    mock.playback_session.playback_task = task

    await SendspinPlayer._cancel_stale_playback_session(mock, task)

    mock.playback_session.cancel.assert_awaited_once_with("group stopped")
