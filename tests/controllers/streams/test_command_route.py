"""Tests for the command route that lets on-player buttons drive a flow stream."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from aiohttp import web

if TYPE_CHECKING:
    from music_assistant.controllers.streams.controller import StreamsController


def _queue_data(session_id: str | None) -> MagicMock:
    """Return a queue record carrying the given playback session."""
    queue_data = MagicMock()
    queue_data.session_id = session_id
    return queue_data


def _mock_player_queues(
    streams_controller: StreamsController,
    queue_data: MagicMock | None,
    active_queue_id: str | None = "queue-1",
) -> MagicMock:
    """Install a player_queues mock resolving to the given active queue and record."""
    player_queues = MagicMock()
    player_queues.get_active_queue.return_value = (
        MagicMock(queue_id=active_queue_id) if active_queue_id else None
    )
    player_queues.queue_data_or_none.return_value = queue_data
    streams_controller.mass.player_queues = player_queues
    return player_queues


def _request(session_id: str, queue_id: str = "queue-1", command: str = "next") -> MagicMock:
    """Return a request for the command route."""
    request = MagicMock()
    request.method = "GET"
    request.headers = {}
    request.match_info = {
        "session_id": session_id,
        "queue_id": queue_id,
        "command": command,
    }
    return request


def test_command_url_is_bound_to_the_playback_session(
    streams_controller: StreamsController,
) -> None:
    """The url carries the session, so it stops working once playback moves on."""
    _mock_player_queues(streams_controller, _queue_data("session-1"))
    url = streams_controller.get_command_url("queue-1", "next")
    assert url is not None
    assert url.endswith("/command/session-1/queue-1/next.mp3")


def test_command_url_targets_the_active_queue_of_a_protocol_player(
    streams_controller: StreamsController,
) -> None:
    """A protocol (child) player id resolves to the queue that actually drives it."""
    player_queues = _mock_player_queues(streams_controller, _queue_data("session-1"))
    url = streams_controller.get_command_url("cast-child-1", "next")
    assert url is not None
    assert url.endswith("/command/session-1/queue-1/next.mp3")
    player_queues.get_active_queue.assert_called_once_with("cast-child-1")
    player_queues.queue_data_or_none.assert_called_once_with("queue-1")


def test_command_url_falls_back_to_the_given_id_without_an_active_queue(
    streams_controller: StreamsController,
) -> None:
    """Without a resolvable active queue the given id is used as the queue id."""
    _mock_player_queues(streams_controller, _queue_data("session-1"), active_queue_id=None)
    url = streams_controller.get_command_url("queue-1", "next")
    assert url is not None
    assert url.endswith("/command/session-1/queue-1/next.mp3")


@pytest.mark.parametrize(
    ("queue_data", "reason"),
    [(None, "unknown queue"), (_queue_data(None), "queue not playing")],
)
def test_command_url_is_absent_without_a_session(
    streams_controller: StreamsController, queue_data: MagicMock | None, reason: str
) -> None:
    """Without an active session there is no url to hand to a player."""
    _mock_player_queues(streams_controller, queue_data)
    assert streams_controller.get_command_url("queue-1", "next") is None, reason


async def test_command_request_advances_the_queue(
    streams_controller: StreamsController, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A request carrying the current session skips to the next item."""
    _mock_player_queues(streams_controller, _queue_data("session-1"))
    create_task = MagicMock()
    monkeypatch.setattr(streams_controller.mass, "create_task", create_task)
    resp = await streams_controller.serve_command_request(_request("session-1"))
    assert isinstance(resp, web.FileResponse)
    create_task.assert_called_once()


@pytest.mark.parametrize("session_id", ["session-2", ""])
async def test_command_request_refuses_a_foreign_session(
    streams_controller: StreamsController, session_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller that does not hold the current session cannot drive the queue."""
    _mock_player_queues(streams_controller, _queue_data("session-1"))
    create_task = MagicMock()
    monkeypatch.setattr(streams_controller.mass, "create_task", create_task)
    with pytest.raises(web.HTTPNotFound):
        await streams_controller.serve_command_request(_request(session_id))
    create_task.assert_not_called()


async def test_command_request_refuses_an_unknown_queue(
    streams_controller: StreamsController, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unregistered queue is refused rather than raising out of the handler."""
    _mock_player_queues(streams_controller, None)
    create_task = MagicMock()
    monkeypatch.setattr(streams_controller.mass, "create_task", create_task)
    with pytest.raises(web.HTTPNotFound):
        await streams_controller.serve_command_request(_request("session-1"))
    create_task.assert_not_called()
