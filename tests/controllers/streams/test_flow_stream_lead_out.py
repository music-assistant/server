"""Tests for how a fully served flow stream is closed off."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from music_assistant.controllers.streams.constants import FLOW_STREAM_LEAD_OUT_SECONDS
from music_assistant.controllers.streams.controller import StreamsController
from music_assistant.helpers.webserver import DEFAULT_SHUTDOWN_TIMEOUT

SESSION_ID = "session-1"
QUEUE_ID = "queue-1"


def _controller(*, exhausted: bool) -> StreamsController:
    """Build a streams controller whose queue reports the given end-of-queue state."""
    mass = MagicMock()
    mass.config.get_raw_core_config_value.return_value = "GLOBAL"
    mass.player_queues.flow_queue_exhausted = MagicMock(
        side_effect=lambda qid, sid: exhausted and (qid, sid) == (QUEUE_ID, SESSION_ID)
    )
    return StreamsController(mass)


async def _finish(
    controller: StreamsController, session_id: str = SESSION_ID
) -> tuple[AsyncMock, MagicMock]:
    """Finish a flow stream against a stub response, returning the sleep and the response."""
    resp = MagicMock()
    with patch(
        "music_assistant.controllers.streams.controller.asyncio.sleep", new=AsyncMock()
    ) as sleep:
        await controller._finish_flow_stream(resp, QUEUE_ID, session_id)
    return sleep, resp


@pytest.mark.asyncio
async def test_holds_connection_open_after_the_last_queue_item() -> None:
    """A flow stream that played the queue to its end lets the player drain first."""
    sleep, _ = await _finish(_controller(exhausted=True))
    sleep.assert_awaited_once_with(FLOW_STREAM_LEAD_OUT_SECONDS)


@pytest.mark.asyncio
async def test_closes_the_connection_after_the_lead_out() -> None:
    """The player only learns the stream ended once the connection is really closed."""
    _, resp = await _finish(_controller(exhausted=True))
    resp.force_close.assert_called_once()


@pytest.mark.asyncio
async def test_closes_without_delay_when_the_flow_restarts() -> None:
    """A flow that ended early must free the player at once so the next stream can start."""
    sleep, resp = await _finish(_controller(exhausted=False))
    sleep.assert_not_awaited()
    resp.force_close.assert_called_once()


@pytest.mark.asyncio
async def test_closes_without_delay_when_superseded() -> None:
    """A newer stream session owns playback, so the stale response must not linger."""
    sleep, resp = await _finish(_controller(exhausted=True), session_id="session-2")
    sleep.assert_not_awaited()
    resp.force_close.assert_called_once()


def test_lead_out_fits_within_the_webserver_shutdown_budget() -> None:
    """A lead-out in flight must not outlive the shutdown grace period."""
    assert FLOW_STREAM_LEAD_OUT_SECONDS < DEFAULT_SHUTDOWN_TIMEOUT
