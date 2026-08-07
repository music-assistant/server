"""Tests for the flow stream lead-out that lets players play out their buffer."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from music_assistant.controllers.streams.constants import FLOW_STREAM_LEAD_OUT_SECONDS
from music_assistant.controllers.streams.controller import StreamsController

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


async def _lead_out(
    controller: StreamsController, session_id: str = SESSION_ID
) -> tuple[AsyncMock, MagicMock]:
    """Run the lead-out against a stub response, returning the sleep and the response."""
    resp = MagicMock()
    with patch(
        "music_assistant.controllers.streams.controller.asyncio.sleep", new=AsyncMock()
    ) as sleep:
        await controller._flow_stream_lead_out(resp, QUEUE_ID, session_id)
    return sleep, resp


@pytest.mark.asyncio
async def test_holds_connection_open_after_the_last_queue_item() -> None:
    """A flow stream that played the queue to its end lets the player drain first."""
    sleep, _ = await _lead_out(_controller(exhausted=True))
    sleep.assert_awaited_once_with(FLOW_STREAM_LEAD_OUT_SECONDS)


@pytest.mark.asyncio
async def test_closes_the_connection_after_the_lead_out() -> None:
    """The player only learns the stream ended once the connection is really closed."""
    _, resp = await _lead_out(_controller(exhausted=True))
    resp.force_close.assert_called_once()


@pytest.mark.asyncio
async def test_skips_lead_out_when_flow_restarts() -> None:
    """A flow that ended early to be restarted must not delay the next stream."""
    sleep, resp = await _lead_out(_controller(exhausted=False))
    sleep.assert_not_awaited()
    resp.force_close.assert_not_called()


@pytest.mark.asyncio
async def test_skips_lead_out_when_superseded() -> None:
    """A newer stream session owns playback, so the stale response must not linger."""
    sleep, resp = await _lead_out(_controller(exhausted=True), session_id="session-2")
    sleep.assert_not_awaited()
    resp.force_close.assert_not_called()
