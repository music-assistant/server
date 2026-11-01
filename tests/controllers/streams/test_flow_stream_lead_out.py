"""Tests for how a fully served flow stream is closed off."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from unittest.mock import MagicMock, patch

import pytest
from music_assistant_models.player_queue import PlayLogEntry

from music_assistant.controllers.streams.constants import (
    FLOW_STREAM_LEAD_OUT_MAX_SECONDS,
    FLOW_STREAM_LEAD_OUT_SECONDS,
)
from music_assistant.controllers.streams.controller import StreamsController

SESSION_ID = "session-1"
QUEUE_ID = "queue-1"


def _controller(
    *,
    exhausted: bool,
    streamed: tuple[float, ...] = (),
    elapsed: float | None = None,
) -> tuple[StreamsController, MagicMock, MagicMock]:
    """
    Build a streams controller whose queue reports the given end-of-queue state.

    :param exhausted: Whether the flow stream played the queue to its end.
    :param streamed: Seconds streamed per item of the flow stream.
    :param elapsed: Position the player reports in the flow stream.
    :return: The controller, its mocked MusicAssistant and the player.
    """
    mass = MagicMock()
    mass.closing = False
    mass.config.get_raw_core_config_value.return_value = "GLOBAL"
    mass.player_queues.flow_queue_exhausted = MagicMock(
        side_effect=lambda qid, sid: exhausted and (qid, sid) == (QUEUE_ID, SESSION_ID)
    )
    queue_data = MagicMock()
    queue_data.flow_mode_stream_log = [
        PlayLogEntry(queue_item_id=f"item-{i}", seconds_streamed=s) for i, s in enumerate(streamed)
    ]
    mass.player_queues.queue_data_or_none.return_value = queue_data
    player = MagicMock()
    player.corrected_elapsed_time = elapsed
    return StreamsController(mass), mass, player


async def _finish(
    controller: StreamsController,
    player: MagicMock,
    session_id: str = SESSION_ID,
    on_sleep: Callable[[float], None] | None = None,
) -> tuple[float, MagicMock]:
    """
    Finish a flow stream against a stub response on a fake clock.

    :return: The seconds the connection was held open, and the response.
    """
    clock = [0.0]

    async def _sleep(delay: float) -> None:
        clock[0] += delay
        if on_sleep is not None:
            on_sleep(clock[0])

    resp = MagicMock()
    loop = asyncio.get_running_loop()
    with (
        patch.object(loop, "time", new=lambda: clock[0]),
        patch("music_assistant.controllers.streams.controller.asyncio.sleep", new=_sleep),
    ):
        await controller._finish_flow_stream(resp, QUEUE_ID, session_id, player)
    return clock[0], resp


@pytest.mark.asyncio
async def test_holds_connection_open_while_the_player_plays_out_its_buffer() -> None:
    """After an album the player is far behind the stream; the hold must cover all of it."""
    # 30 minutes streamed, the player reports it has rendered 29m30s of it
    controller, _, player = _controller(exhausted=True, streamed=(600.0, 1200.0), elapsed=1770.0)
    held, _ = await _finish(controller, player)
    assert held == pytest.approx(30 + FLOW_STREAM_LEAD_OUT_SECONDS)


@pytest.mark.asyncio
async def test_holds_the_margin_when_the_player_is_caught_up() -> None:
    """A player that already rendered all it got still gets the base grace."""
    controller, _, player = _controller(exhausted=True, streamed=(120.0,), elapsed=125.0)
    held, _ = await _finish(controller, player)
    assert held == pytest.approx(FLOW_STREAM_LEAD_OUT_SECONDS)


@pytest.mark.asyncio
async def test_holds_the_margin_when_the_player_reports_no_position() -> None:
    """Without a reported position there is nothing to measure; keep the base grace."""
    controller, _, player = _controller(exhausted=True, streamed=(120.0,), elapsed=None)
    held, _ = await _finish(controller, player)
    assert held == pytest.approx(FLOW_STREAM_LEAD_OUT_SECONDS)


@pytest.mark.asyncio
async def test_hold_is_capped_for_a_player_stuck_at_the_start() -> None:
    """A position that never moved must not keep the connection open for the whole queue."""
    controller, _, player = _controller(exhausted=True, streamed=(3600.0,), elapsed=0.0)
    held, _ = await _finish(controller, player)
    assert held == pytest.approx(FLOW_STREAM_LEAD_OUT_MAX_SECONDS)


@pytest.mark.asyncio
async def test_closes_the_connection_after_the_lead_out() -> None:
    """The player only learns the stream ended once the connection is really closed."""
    controller, _, player = _controller(exhausted=True, streamed=(600.0,), elapsed=580.0)
    _, resp = await _finish(controller, player)
    resp.force_close.assert_called_once()


@pytest.mark.asyncio
async def test_closes_without_delay_when_the_flow_restarts() -> None:
    """A flow that ended early must free the player at once so the next stream can start."""
    controller, _, player = _controller(exhausted=False, streamed=(600.0,), elapsed=580.0)
    held, resp = await _finish(controller, player)
    assert held == 0
    resp.force_close.assert_called_once()


@pytest.mark.asyncio
async def test_closes_without_delay_when_superseded() -> None:
    """A newer stream session owns playback, so the stale response must not linger."""
    controller, _, player = _controller(exhausted=True, streamed=(600.0,), elapsed=580.0)
    held, resp = await _finish(controller, player, session_id="session-2")
    assert held == 0
    resp.force_close.assert_called_once()


@pytest.mark.asyncio
async def test_lets_go_when_playback_moves_on_during_the_hold() -> None:
    """New playback (or a stop) during the hold ends it within a second."""
    controller, mass, player = _controller(exhausted=True, streamed=(600.0,), elapsed=560.0)

    def _new_session_at_5s(now: float) -> None:
        if now >= 5:
            mass.player_queues.flow_queue_exhausted.side_effect = lambda *_: False

    held, resp = await _finish(controller, player, on_sleep=_new_session_at_5s)
    assert held == pytest.approx(5)
    resp.force_close.assert_called_once()


@pytest.mark.asyncio
async def test_lets_go_when_the_server_shuts_down() -> None:
    """A hold in flight must not stall a restart of the server."""
    controller, mass, player = _controller(exhausted=True, streamed=(600.0,), elapsed=560.0)

    def _shutdown_at_3s(now: float) -> None:
        if now >= 3:
            mass.closing = True

    held, resp = await _finish(controller, player, on_sleep=_shutdown_at_3s)
    assert held == pytest.approx(3)
    resp.force_close.assert_called_once()
