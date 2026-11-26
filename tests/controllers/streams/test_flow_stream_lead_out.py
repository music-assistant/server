"""Tests for how a fully served flow stream is closed off."""

from __future__ import annotations

import asyncio
from typing import cast
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from music_assistant.controllers.streams.constants import FLOW_STREAM_MAX_LEAD_OUT_SECONDS
from music_assistant.controllers.streams.controller import StreamsController

SESSION_ID = "session-1"
QUEUE_ID = "queue-1"


def _controller(*, exhausted: bool, played_out_after: int | None = None) -> StreamsController:
    """
    Build a streams controller whose queue reports the given end-of-queue state.

    :param exhausted: Whether the flow stream session played the queue to its end.
    :param played_out_after: Number of checks after which the player has played out the
        session, None for a player that never does.
    """
    mass = MagicMock()
    mass.config.get_raw_core_config_value.return_value = "GLOBAL"
    mass.player_queues.flow_queue_exhausted = MagicMock(
        side_effect=lambda qid, sid: exhausted and (qid, sid) == (QUEUE_ID, SESSION_ID)
    )
    checks = 0

    def _played_out(*_args: object) -> bool:
        nonlocal checks
        checks += 1
        return played_out_after is not None and checks > played_out_after

    mass.player_queues.flow_stream_played_out = MagicMock(side_effect=_played_out)
    return StreamsController(mass)


def _request(*, disconnect_after: int | None = None) -> MagicMock:
    """
    Return a stub request whose client stays connected, or disconnects after a number of polls.

    :param disconnect_after: Number of transport checks after which the client is gone, None
        for a client that stays connected.
    """
    request = MagicMock()
    checks = 0

    def _transport() -> object | None:
        nonlocal checks
        checks += 1
        if disconnect_after is not None and checks > disconnect_after:
            return None
        return MagicMock()

    type(request).transport = PropertyMock(side_effect=_transport)
    return request


async def _finish(
    controller: StreamsController,
    request: MagicMock | None = None,
    session_id: str = SESSION_ID,
    player: MagicMock | None = None,
) -> tuple[AsyncMock, MagicMock]:
    """Finish a flow stream against a stub response, returning the sleep and the response."""
    resp = MagicMock()
    with patch(
        "music_assistant.controllers.streams.controller.asyncio.sleep", new=AsyncMock()
    ) as sleep:
        await controller._finish_flow_stream(
            request or _request(), resp, QUEUE_ID, session_id, player or MagicMock()
        )
    return sleep, resp


@pytest.mark.asyncio
async def test_holds_the_connection_until_the_player_played_out_its_buffer() -> None:
    """A flow stream that played the queue to its end stays open until the player played it all."""
    controller = _controller(exhausted=True, played_out_after=3)
    player = MagicMock()

    sleep, resp = await _finish(controller, player=player)

    assert sleep.await_count == 3
    resp.force_close.assert_called_once()
    cast("MagicMock", controller.mass).player_queues.flow_stream_played_out.assert_called_with(
        QUEUE_ID, SESSION_ID, player
    )


@pytest.mark.asyncio
async def test_stops_holding_when_the_player_disconnects() -> None:
    """A player that closed the connection itself needs no lead-out."""
    sleep, resp = await _finish(_controller(exhausted=True), _request(disconnect_after=2))

    assert sleep.await_count == 2
    resp.force_close.assert_called_once()


@pytest.mark.asyncio
async def test_stops_holding_at_the_lead_out_cap() -> None:
    """A player that never plays out its buffer is cut off after the maximum lead-out."""
    sleep, resp = await _finish(_controller(exhausted=True))

    assert sleep.await_count == FLOW_STREAM_MAX_LEAD_OUT_SECONDS
    resp.force_close.assert_called_once()


@pytest.mark.asyncio
async def test_closes_without_delay_when_the_flow_restarts() -> None:
    """A flow that ended early must free the player at once so the next stream can start."""
    controller = _controller(exhausted=False)

    sleep, resp = await _finish(controller)

    sleep.assert_not_awaited()
    cast("MagicMock", controller.mass).player_queues.flow_stream_played_out.assert_not_called()
    resp.force_close.assert_called_once()


@pytest.mark.asyncio
async def test_closes_without_delay_when_superseded() -> None:
    """A newer stream session owns playback, so the stale response must not linger."""
    sleep, resp = await _finish(_controller(exhausted=True), session_id="session-2")
    sleep.assert_not_awaited()
    resp.force_close.assert_called_once()


@pytest.mark.asyncio
async def test_a_lead_out_in_progress_can_be_cancelled() -> None:
    """A server shutdown cancels the handler, which must end a lead-out at once."""
    controller = _controller(exhausted=True)
    resp = MagicMock()
    task = asyncio.create_task(
        controller._finish_flow_stream(_request(), resp, QUEUE_ID, SESSION_ID, MagicMock())
    )
    await asyncio.sleep(0)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
