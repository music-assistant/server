"""
Tests that a buffered Sendspin stream queues audio before its first commit.

A real playback session runs against a real PushStream on a manual clock. The source
hands out one 100 ms slice at a time and the test moves the clock in between, so a
producer that lags shows up as a commit that comes late.
"""

from __future__ import annotations

from itertools import pairwise
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock

from aiosendspin.server.push_stream import MAIN_CHANNEL

from music_assistant.providers.sendspin.playback import (
    _PRODUCER_BACKLOG_SIZE,
    _START_LOG_COMMITS,
    _START_QUEUE_US,
)
from tests.providers.sendspin.test_dsp_channel_alignment import (
    DSP_ID,
    _assert_aligned,
    _ramp_slice,
    _SessionHarness,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import pytest

SLICE_US = 100_000
START_SLICES = _START_QUEUE_US // SLICE_US
# The time the producer needed for slices 2 to 5 of a start after a server start.
COLD_START_SLICE_TIMES_US = (157_400, 100_000, 107_700, 114_600)
MOVE_LOG = "Play timeline moved %+.1f ms at commit %d of the stream"


class _Start:
    """A playback session whose commits run freely while the test produces slice by slice."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, live: bool = False) -> None:
        self.harness = _SessionHarness(monkeypatch, dsp_ids=set())
        self.clock = self.harness.clock
        self.commit_times_us: list[int] = []
        self.commit_starts_us: list[int] = []
        self._produced = 0
        # sleep_to_limit_buffer never blocks, as for a stream that is less than 30 s ahead
        for _ in range(1000):
            self.harness.commit_permits.release()
        monkeypatch.setattr(self.harness.session, "_is_live_source", MagicMock(return_value=live))
        commit_audio = self.harness.push_stream.commit_audio

        async def _recording_commit() -> int:
            self.commit_times_us.append(self.clock.now_us())
            start_us = await commit_audio()
            self.commit_starts_us.append(start_us)
            return start_us

        monkeypatch.setattr(self.harness.push_stream, "commit_audio", _recording_commit)

    async def play(self) -> None:
        """Start playback with one slice produced."""
        self.harness.allow_source(0)
        await self.harness.session.start(MagicMock())
        await self.produce(0)

    async def produce(self, *slice_times_us: int) -> None:
        """Produce one more slice per given time, each taking that long to make."""
        for slice_time_us in slice_times_us:
            self.clock.advance_us(slice_time_us)
            self._produced += 1
            self.harness.allow_source(self._produced)
            await self.harness.settle()

    def moves_us(self) -> list[int]:
        """Return how far each commit after the first moved the play timeline."""
        starts = self.commit_starts_us
        return [later - earlier - SLICE_US for earlier, later in pairwise(starts)]

    def logged_moves(self) -> list[tuple[object, ...]]:
        """Return the arguments of every debug line about a moved play timeline."""
        debug = cast("MagicMock", self.harness.session.player.logger.debug)
        return [call.args[1:] for call in debug.call_args_list if call.args[0] == MOVE_LOG]


async def test_buffered_start_keeps_its_timeline_when_the_producer_lags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A producer that needs 80 ms too long for its first slices moves no timeline."""
    start = _Start(monkeypatch)
    try:
        await start.play()
        await start.produce(*COLD_START_SLICE_TIMES_US, *[5_000] * 10)

        assert len(start.commit_starts_us) == START_SLICES + 10
        assert start.moves_us() == [0] * (START_SLICES + 9)
        assert start.logged_moves() == []
    finally:
        await start.harness.close()


async def test_first_commit_waits_for_half_a_second_of_audio_and_no_longer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first sound comes as much later as the producer needs to queue half a second."""
    start = _Start(monkeypatch)
    try:
        await start.play()
        await start.produce(*[5_000] * (START_SLICES - 2))
        assert start.commit_times_us == []

        await start.produce(5_000)

        assert start.commit_times_us == [5_000 * (START_SLICES - 1)] * START_SLICES
        assert start.moves_us() == [0] * (START_SLICES - 1)
    finally:
        await start.harness.close()


async def test_live_source_commits_its_first_chunk_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live source has no audio ahead, so its first slice is committed right away."""
    start = _Start(monkeypatch, live=True)
    try:
        await start.play()

        assert start.commit_times_us == [0]
    finally:
        await start.harness.close()


async def test_source_shorter_than_the_start_queue_still_plays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source that ends before half a second is queued plays all it has."""
    start = _Start(monkeypatch)
    try:
        await start.play()
        await start.produce(5_000, 5_000)
        assert start.commit_starts_us == []

        start.harness.end_source()
        await start.harness.finish()

        assert len(start.commit_starts_us) == 3
        assert start.moves_us() == [0, 0]
    finally:
        await start.harness.close()


async def test_full_queue_starts_the_stream_before_half_a_second_is_queued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source of tiny chunks that fills the queue first does not wait forever."""
    start = _Start(monkeypatch)
    tiny_chunk = _ramp_slice(0)[: len(_ramp_slice(0)) // 20]

    async def _tiny_chunks() -> AsyncIterator[bytes]:
        while True:
            yield tiny_chunk

    player = start.harness.session.player
    monkeypatch.setattr(player.mass.streams, "get_stream", MagicMock(return_value=_tiny_chunks()))
    try:
        await start.harness.session.start(MagicMock())
        await start.harness.settle()

        assert len(start.commit_starts_us) >= _PRODUCER_BACKLOG_SIZE
    finally:
        await start.harness.close()


async def test_timeline_that_moves_during_the_first_commits_is_logged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A producer that stalls longer than the queued audio lasts leaves a line in the debug log."""
    start = _Start(monkeypatch)
    try:
        await start.play()
        await start.produce(*[5_000] * (START_SLICES - 1))

        await start.produce(_START_QUEUE_US + 300_000, SLICE_US + 150_000)

        *start_moves_us, first_move_us, second_move_us = start.moves_us()
        assert start_moves_us == [0] * (START_SLICES - 1)
        assert first_move_us > 0
        assert second_move_us == 150_000
        assert start.logged_moves() == [
            (first_move_us / 1000, START_SLICES + 1),
            (150.0, START_SLICES + 2),
        ]
    finally:
        await start.harness.close()


async def test_timeline_that_moves_after_the_first_commits_is_not_logged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Later moves stay out of the log, so a slow producer does not fill it."""
    start = _Start(monkeypatch)
    try:
        await start.play()
        await start.produce(*[5_000] * (_START_LOG_COMMITS - 1))

        await start.produce(3_000_000)

        assert len(start.commit_starts_us) == _START_LOG_COMMITS + 1
        assert start.moves_us()[-1] > 0
        assert start.logged_moves() == []
    finally:
        await start.harness.close()


async def test_member_joining_while_the_first_commit_waits_gets_the_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A member with its own processing that joins before the first commit plays from the start."""
    harness = _SessionHarness(monkeypatch, dsp_ids={DSP_ID})
    try:
        harness.allow_source(START_SLICES - 2)
        await harness.session.start(MagicMock())
        await harness.settle()
        assert harness.entered == 0

        await harness.add_member(DSP_ID)
        harness.allow_source(None)
        await harness._until(harness._parked)
        await harness.commit(30)

        assert DSP_ID not in harness.session._join_catchup
        channel_id = harness.session._member_pipelines[DSP_ID].channel_id
        _assert_aligned(harness.offsets_us(channel_id))
        assert harness.chunks(channel_id)[0] == harness.chunks(MAIN_CHANNEL)[0]
    finally:
        await harness.close()


async def test_member_joining_during_the_first_commits_gets_its_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A member that joins between the first commits gets the audio still queued behind them."""
    harness = _SessionHarness(monkeypatch, dsp_ids={DSP_ID})
    try:
        await harness.start(source_limit=START_SLICES)
        await harness.commit(1)
        assert len(harness.chunks(MAIN_CHANNEL)) == 2

        await harness.add_member(DSP_ID)
        harness.allow_source(None)
        await harness.commit(30)

        assert DSP_ID not in harness.session._join_catchup
        channel_id = harness.session._member_pipelines[DSP_ID].channel_id
        _assert_aligned(harness.offsets_us(channel_id))
        main_starts = [start_us for start_us, _ in harness.chunks(MAIN_CHANNEL)]
        member_starts = [start_us for start_us, frame in harness.chunks(channel_id) if frame >= 0]
        assert member_starts == main_starts[-len(member_starts) :]
        assert member_starts[0] <= main_starts[START_SLICES - 1]
    finally:
        await harness.close()
