"""Tests for the flow stream lead-out that lets players play out their buffer."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from music_assistant.controllers.streams.constants import FLOW_STREAM_LEAD_OUT_SECONDS
from music_assistant.controllers.streams.controller import StreamsController

SESSION_ID = "session-1"
QUEUE_ID = "queue-1"


def _controller(session_id: str | None) -> StreamsController:
    """Build a streams controller whose queue reports the given active stream session."""
    mass = MagicMock()
    mass.config.get_raw_core_config_value.return_value = "GLOBAL"
    if session_id is None:
        mass.player_queues.queue_data_or_none.return_value = None
    else:
        mass.player_queues.queue_data_or_none.return_value = MagicMock(session_id=session_id)
    return StreamsController(mass)


@pytest.mark.asyncio
async def test_holds_connection_open_for_the_lead_out() -> None:
    """A finished flow stream keeps its connection open so the player can drain."""
    controller = _controller(SESSION_ID)
    with patch(
        "music_assistant.controllers.streams.controller.asyncio.sleep", new=AsyncMock()
    ) as sleep:
        await controller._flow_stream_lead_out(QUEUE_ID, SESSION_ID, keep_alive=False)
    sleep.assert_awaited_once_with(FLOW_STREAM_LEAD_OUT_SECONDS)


@pytest.mark.asyncio
async def test_skips_lead_out_when_superseded() -> None:
    """A newer stream session owns playback, so the stale response must not linger."""
    controller = _controller("session-2")
    with patch(
        "music_assistant.controllers.streams.controller.asyncio.sleep", new=AsyncMock()
    ) as sleep:
        await controller._flow_stream_lead_out(QUEUE_ID, SESSION_ID, keep_alive=False)
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_skips_lead_out_when_queue_is_gone() -> None:
    """There is nothing left to play out once the queue itself disappeared."""
    controller = _controller(None)
    with patch(
        "music_assistant.controllers.streams.controller.asyncio.sleep", new=AsyncMock()
    ) as sleep:
        await controller._flow_stream_lead_out(QUEUE_ID, SESSION_ID, keep_alive=False)
    sleep.assert_not_awaited()
