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


@pytest.mark.asyncio
async def test_holds_connection_open_after_the_last_queue_item() -> None:
    """A flow stream that played the queue to its end lets the player drain first."""
    controller = _controller(exhausted=True)
    with patch(
        "music_assistant.controllers.streams.controller.asyncio.sleep", new=AsyncMock()
    ) as sleep:
        await controller._flow_stream_lead_out(QUEUE_ID, SESSION_ID, keep_alive=False)
    sleep.assert_awaited_once_with(FLOW_STREAM_LEAD_OUT_SECONDS)


@pytest.mark.asyncio
async def test_skips_lead_out_when_flow_restarts() -> None:
    """A flow that ended early to be restarted must not delay the next stream."""
    controller = _controller(exhausted=False)
    with patch(
        "music_assistant.controllers.streams.controller.asyncio.sleep", new=AsyncMock()
    ) as sleep:
        await controller._flow_stream_lead_out(QUEUE_ID, SESSION_ID, keep_alive=False)
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_skips_lead_out_on_a_kept_alive_connection() -> None:
    """A connection that outlives the response is not something the player waits on."""
    controller = _controller(exhausted=True)
    with patch(
        "music_assistant.controllers.streams.controller.asyncio.sleep", new=AsyncMock()
    ) as sleep:
        await controller._flow_stream_lead_out(QUEUE_ID, SESSION_ID, keep_alive=True)
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_skips_lead_out_when_superseded() -> None:
    """A newer stream session owns playback, so the stale response must not linger."""
    controller = _controller(exhausted=True)
    with patch(
        "music_assistant.controllers.streams.controller.asyncio.sleep", new=AsyncMock()
    ) as sleep:
        await controller._flow_stream_lead_out(QUEUE_ID, "session-2", keep_alive=False)
    sleep.assert_not_awaited()
