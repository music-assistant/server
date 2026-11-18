"""Tests for aborting per-item stream responses of superseded sessions."""

from __future__ import annotations

from unittest.mock import MagicMock

from music_assistant.controllers.streams.controller import StreamsController


def _make_controller() -> StreamsController:
    """Build a bare controller carrying only the open-streams registry."""
    controller = StreamsController.__new__(StreamsController)
    controller._open_item_streams = {}
    return controller


def test_only_responses_of_stale_sessions_are_aborted() -> None:
    """The session that owns playback keeps its responses; every other one dies."""
    controller = _make_controller()
    stale, current = MagicMock(), MagicMock()
    controller._open_item_streams["queue-1"] = [("old", stale), ("new", current)]

    controller.close_superseded_item_streams("queue-1", "new")

    stale.transport.abort.assert_called_once()
    current.transport.abort.assert_not_called()


def test_a_cleared_session_aborts_every_open_response() -> None:
    """A stopped queue owns no session, so nothing may keep streaming."""
    controller = _make_controller()
    first, second = MagicMock(), MagicMock()
    controller._open_item_streams["queue-1"] = [("a", first), ("b", second)]

    controller.close_superseded_item_streams("queue-1", None)

    first.transport.abort.assert_called_once()
    second.transport.abort.assert_called_once()


def test_an_already_gone_transport_is_left_alone() -> None:
    """A response whose connection already closed has nothing left to abort."""
    controller = _make_controller()
    gone = MagicMock()
    gone.transport = None
    controller._open_item_streams["queue-1"] = [("old", gone)]

    controller.close_superseded_item_streams("queue-1", "new")


def test_a_queue_without_open_responses_is_a_noop() -> None:
    """Rotating a session on an idle queue must not error."""
    _make_controller().close_superseded_item_streams("queue-x", "s")
