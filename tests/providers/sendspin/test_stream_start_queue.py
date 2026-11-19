"""
Tests that a buffered Sendspin stream queues audio before its first commit.

A real playback session runs against a real PushStream on a manual clock. The source
hands out one 100 ms slice at a time and the test moves the clock in between, so a
producer that lags shows up as a commit that comes late.
"""

from __future__ import annotations

import asyncio
import wave
from itertools import pairwise
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock
from uuid import uuid4

import numpy as np
import pytest
from aiosendspin.server.audio import AudioFormat as SendspinAudioFormat
from aiosendspin.server.push_stream import DEFAULT_INITIAL_DELAY_US, MAIN_CHANNEL
from music_assistant_models.dsp import ConvolutionFilter
from music_assistant_models.enums import ContentType, MediaType
from music_assistant_models.media_items.audio_format import AudioFormat

from music_assistant.helpers.dsp import filter_to_ffmpeg_params
from music_assistant.models.player import PlayerMedia
from music_assistant.providers.sendspin.playback import (
    _PRODUCER_BACKLOG_SIZE,
    _START_LOG_COMMITS,
    _START_QUEUE_US,
    _PipelineConfig,
)
from tests.providers.sendspin.test_dsp_channel_alignment import (
    DSP_ID,
    LEADER_ID,
    SAMPLE_RATE,
    _assert_aligned,
    _first_frame,
    _ramp_slice,
    _SessionHarness,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path
    from uuid import UUID

    from music_assistant.providers.sendspin.playback import _HistoryChunk

SLICE_US = 100_000
START_SLICES = _START_QUEUE_US // SLICE_US
# The time the producer needed for slices 2 to 5 of a start after a server start.
COLD_START_SLICE_TIMES_US = (157_400, 100_000, 107_700, 114_600)
MOVE_LOG = (
    "Play timeline moved %+.1f ms at commit %d of the stream: the commit before it and what "
    "follows it took %.1f ms, then getting the audio of this one ready %.1f ms; scheduled "
    "%.1f ms ahead, the first commit %.1f ms"
)
# How far ahead PushStream schedules audio when no player asks for more.
LEAD_MS = DEFAULT_INITIAL_DELAY_US / 1000
# Wall time to wait for progress that runs on a worker thread or in FFmpeg.
WAIT_S = 10.0
GROUP_ID = "universal-group"


async def _wait_for(condition: Callable[[], bool], failure: Callable[[], str]) -> None:
    """
    Wait until a condition holds.

    :param condition: The progress to wait for.
    :param failure: Describes what was reached instead, for a test that waited in vain.
    """
    try:
        async with asyncio.timeout(WAIT_S):
            while not condition():
                await asyncio.sleep(0.001)
    except TimeoutError:
        raise AssertionError(failure()) from None


class _Start:
    """A playback session whose commits run freely while the test produces slice by slice."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, live: bool = False) -> None:
        self.harness = _SessionHarness(monkeypatch, dsp_ids=set())
        self.clock = self.harness.clock
        self.live = live
        self.commit_times_us: list[int] = []
        self.commit_starts_us: list[int] = []
        self.commit_durations_us: list[int] = []
        # Clock time a commit takes after it was scheduled, by its index.
        self.commit_extra_us: dict[int, int] = {}
        self._produced = 0
        # sleep_to_limit_buffer never blocks, as for a stream that is less than 30 s ahead
        for _ in range(1000):
            self.harness.commit_permits.release()
        monkeypatch.setattr(self.harness.session, "_is_live_source", MagicMock(return_value=live))
        push_stream = self.harness.push_stream
        commit_audio = push_stream.commit_audio
        prepare_audio = push_stream.prepare_audio
        main_duration_us = 0

        def _recording_prepare(
            pcm: bytes, audio_format: SendspinAudioFormat, *, channel_id: UUID = MAIN_CHANNEL
        ) -> None:
            nonlocal main_duration_us
            if channel_id == MAIN_CHANNEL:
                main_duration_us = len(pcm) // 8 * 1_000_000 // SAMPLE_RATE
            prepare_audio(pcm, audio_format, channel_id=channel_id)

        async def _recording_commit() -> int:
            self.commit_times_us.append(self.clock.now_us())
            self.commit_durations_us.append(main_duration_us)
            start_us = await commit_audio()
            self.clock.advance_us(self.commit_extra_us.get(len(self.commit_starts_us), 0))
            self.commit_starts_us.append(start_us)
            return start_us

        monkeypatch.setattr(push_stream, "prepare_audio", _recording_prepare)
        monkeypatch.setattr(push_stream, "commit_audio", _recording_commit)

    async def play(self, media: PlayerMedia | None = None) -> None:
        """Start playback with one slice produced."""
        self.harness.allow_source(0)
        await self.harness.session.start(media or MagicMock())
        await self.produce(0)

    async def produce(self, *slice_times_us: int) -> None:
        """
        Produce one more slice per given time, each taking that long to make.

        Each slice is taken by the producer, and every commit it allows is done, before the
        next one is made.
        """
        for slice_time_us in slice_times_us:
            self.clock.advance_us(slice_time_us)
            self._produced += 1
            self.harness.allow_source(self._produced)
            await self._wait_for_slice(self._produced)

    async def finish(self) -> None:
        """Wait until playback ends with its source."""
        task = self.harness.session.playback_task
        assert task is not None
        await _wait_for(task.done, lambda: "playback did not end with its source")
        await task

    def moves_us(self) -> list[int]:
        """Return how far each commit after the first moved the play timeline."""
        return [
            later - earlier - duration_us
            for (earlier, later), duration_us in zip(
                pairwise(self.commit_starts_us), self.commit_durations_us, strict=False
            )
        ]

    def logged_moves(self) -> list[tuple[object, ...]]:
        """Return the arguments of every debug line about a moved play timeline."""
        debug = cast("MagicMock", self.harness.session.player.logger.debug)
        return [call.args[1:] for call in debug.call_args_list if call.args[0] == MOVE_LOG]

    async def _wait_for_slice(self, produced: int) -> None:
        """Wait until the producer took slice `produced` and every commit it allows is done."""
        expected_us = produced * SLICE_US if self.live or produced >= START_SLICES else 0

        def _committed_us() -> int:
            return sum(self.commit_durations_us[: len(self.commit_starts_us)])

        await _wait_for(
            lambda: self.harness.source_reads > produced and _committed_us() == expected_us,
            lambda: (
                f"after slice {produced}: {_committed_us() / 1000:.0f} ms committed "
                f"instead of {expected_us / 1000:.0f} ms"
            ),
        )


async def test_buffered_start_keeps_its_timeline_when_the_producer_lags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A producer that needs 80 ms too long for its first slices moves no timeline."""
    start = _Start(monkeypatch)
    try:
        await start.play()
        await start.produce(*COLD_START_SLICE_TIMES_US, *[5_000] * 10)

        assert start.commit_durations_us == [_START_QUEUE_US] + [SLICE_US] * 10
        assert start.moves_us() == [0] * 10
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

        assert start.commit_times_us == [5_000 * (START_SLICES - 1)]
        assert start.commit_durations_us == [_START_QUEUE_US]
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


@pytest.mark.parametrize(
    ("group_media_type", "live"),
    [
        (MediaType.RADIO, True),
        (MediaType.AUDIO_SOURCE, True),
        (MediaType.PLUGIN_SOURCE, True),
        (MediaType.TRACK, False),
    ],
)
async def test_universal_group_member_starts_like_the_media_of_the_group(
    monkeypatch: pytest.MonkeyPatch, group_media_type: MediaType, live: bool
) -> None:
    """A member of a Universal Group starts at once only when the group plays live audio."""
    start = _Start(monkeypatch, live=live)
    session = start.harness.session
    monkeypatch.delattr(session, "_is_live_source")
    group_media = PlayerMedia(uri="group-media", media_type=group_media_type)
    group_player = SimpleNamespace(own_protocol=SimpleNamespace(current_media=group_media))
    monkeypatch.setattr(
        session.player.mass.players,
        "get_player",
        lambda player_id: group_player if player_id == GROUP_ID else None,
    )
    # the media a Universal Group hands each of its members
    member_media = PlayerMedia(
        uri=f"http://127.0.0.1:8097/ugp/{GROUP_ID}.flac?player_id={LEADER_ID}",
        media_type=MediaType.FLOW_STREAM,
        source_id=GROUP_ID,
    )
    try:
        await start.play(member_media)
        await start.produce(*[5_000] * (START_SLICES - 1))

        assert start.commit_times_us[0] == (0 if live else 5_000 * (START_SLICES - 1))
    finally:
        await start.harness.close()


async def test_member_processing_at_a_high_rate_holds_the_start_queue(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    A member's FFmpeg with a convolution at 384 kHz takes the audio queued before the start.

    Its output is read by the commits only, so until the first commit the queued audio has to
    fit in FFmpeg and its pipes.
    """
    rate = 384_000
    source_slices = 30
    harness = _SessionHarness(monkeypatch, dsp_ids={LEADER_ID})
    session = harness.session
    pcm_format = AudioFormat(
        content_type=ContentType.PCM_F32LE, sample_rate=rate, bit_depth=32, channels=2
    )
    wire_format = SendspinAudioFormat(
        sample_rate=rate, bit_depth=32, channels=2, sample_type="float"
    )
    monkeypatch.setattr(
        session, "_select_session_pcm_formats", MagicMock(return_value=(pcm_format, wire_format))
    )
    with wave.open(str(tmp_path / "room.wav"), "wb") as ir_file:
        ir_file.setnchannels(2)
        ir_file.setsampwidth(2)
        ir_file.setframerate(48_000)
        decay = np.exp(-np.arange(4_800) / 600)
        noise = np.random.default_rng(0).standard_normal(4_800 * 2).reshape(-1, 2)
        ir_file.writeframes((noise * decay[:, None] * 8_000).astype("<i2").tobytes())
    filter_params = filter_to_ffmpeg_params(
        ConvolutionFilter(enabled=True, ir_id="room"), pcm_format, ir_dir=str(tmp_path)
    )
    monkeypatch.setattr(
        session,
        "_read_pipeline_config",
        lambda _player_id: _PipelineConfig(
            requires_transform=True, filter_params=tuple(filter_params)
        ),
    )
    monkeypatch.delattr(session, "_create_member_ffmpeg")
    channel_id = uuid4()
    harness.push_stream.enable_pcm_cache_for_channel(channel_id)
    session._preassigned_channels[LEADER_ID] = channel_id
    slice_pcm = np.zeros(rate // 10 * 2, dtype="<f4").tobytes()

    async def _source() -> AsyncIterator[bytes]:
        for _ in range(source_slices):
            yield slice_pcm

    monkeypatch.setattr(
        session.player.mass.streams, "get_stream", MagicMock(return_value=_source())
    )
    for _ in range(source_slices):
        harness.commit_permits.release()
    try:
        await session.start(MagicMock())
        task = session.playback_task
        assert task is not None
        await _wait_for(task.done, lambda: f"playback stalled after {harness.entered} commits")
        await task

        commits = source_slices - START_SLICES + 1
        assert harness.entered == commits
        assert len(harness.push_stream.get_cached_pcm_chunks(channel_id)) == commits
    finally:
        await harness.close()


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
        await start.finish()

        assert start.commit_durations_us == [3 * SLICE_US]
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

        await _wait_for(
            lambda: len(start.commit_starts_us) >= _PRODUCER_BACKLOG_SIZE,
            lambda: f"{len(start.commit_starts_us)} completed commits",
        )
    finally:
        await start.harness.close()


@pytest.mark.parametrize("slow_commit", [0, 1])
async def test_slow_commit_at_the_start_keeps_the_timeline(
    monkeypatch: pytest.MonkeyPatch, slow_commit: int
) -> None:
    """A commit that takes 250 ms in the first half second moves no timeline."""
    start = _Start(monkeypatch)
    start.commit_extra_us[slow_commit] = 250_000
    try:
        await start.play()
        await start.produce(*[5_000] * (START_SLICES - 1 + 5))

        assert start.moves_us() == [0] * 5
        assert start.logged_moves() == []
    finally:
        await start.harness.close()


async def test_timeline_moved_by_a_slow_producer_is_logged_with_the_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A producer that stalls longer than the queued audio lasts leaves a line in the debug log."""
    start = _Start(monkeypatch)
    try:
        await start.play()
        await start.produce(*[5_000] * (START_SLICES - 1))

        await start.produce(_START_QUEUE_US + 300_000, SLICE_US + 150_000)

        assert start.moves_us() == [300_000, 150_000]
        assert start.logged_moves() == [
            (300.0, 2, 0.0, 800.0, LEAD_MS, LEAD_MS),
            (150.0, 3, 0.0, 250.0, LEAD_MS, LEAD_MS),
        ]
    finally:
        await start.harness.close()


async def test_timeline_moved_by_a_slow_commit_is_logged_with_its_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A first commit that takes longer than the queued audio lasts leaves a line in the debug log."""
    start = _Start(monkeypatch)
    start.commit_extra_us[0] = 700_000
    try:
        await start.play()
        await start.produce(*[5_000] * (START_SLICES - 1))

        await start.produce(5_000)

        assert start.moves_us() == [205_000]
        assert start.logged_moves() == [(205.0, 2, 700.0, 5.0, LEAD_MS, LEAD_MS)]
    finally:
        await start.harness.close()


async def test_timeline_that_moves_after_the_first_commits_is_not_logged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Later moves stay out of the log, so a slow producer does not fill it."""
    start = _Start(monkeypatch)
    try:
        await start.play()
        await start.produce(*[5_000] * (_START_LOG_COMMITS + START_SLICES - 2))

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
        await harness._until(lambda: harness.source_reads > START_SLICES - 2)
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


async def test_member_joining_before_the_start_of_a_short_source_gets_its_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A member that joins before the first commit plays a source shorter than the start queue."""
    harness = _SessionHarness(monkeypatch, dsp_ids={DSP_ID})
    session = harness.session
    fanout = session._fanout_history_chunk_to_join_processors

    async def _fanout_and_wait(chunk: _HistoryChunk) -> None:
        """Hand a commit on to the joining member and wait until it processed it."""
        await fanout(chunk)
        if (state := session._join_catchup.get(DSP_ID)) is None:
            return
        assert state.fed_until_us is not None
        assert state.first_history_start_us is not None
        processor = state.processor
        fed_us = state.fed_until_us - state.first_history_start_us
        await _wait_for(
            lambda: processor.produced_output_us >= fed_us,
            lambda: f"processed {processor.produced_output_us} us of {fed_us}",
        )

    monkeypatch.setattr(session, "_fanout_history_chunk_to_join_processors", _fanout_and_wait)
    try:
        harness.allow_source(2)
        await session.start(MagicMock())
        await harness._until(lambda: harness.source_reads == 3)
        assert harness.entered == 0

        await harness.add_member(DSP_ID)
        channel_id = session._member_pipelines[DSP_ID].channel_id
        harness.allow_source(3)
        await harness._until(lambda: harness.source_reads == 4)
        harness.end_source()
        await harness.finish()

        played_us = {
            channel: sum(
                chunk.duration_us for chunk in harness.push_stream.get_cached_pcm_chunks(channel)
            )
            for channel in (MAIN_CHANNEL, channel_id)
        }
        assert played_us == {MAIN_CHANNEL: 3 * SLICE_US, channel_id: 3 * SLICE_US}
    finally:
        await harness.close()


async def test_member_joining_during_the_first_commits_gets_its_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A member that joins between the first commits gets the audio still queued behind them."""
    harness = _SessionHarness(monkeypatch, dsp_ids={DSP_ID})
    try:
        await harness.start(source_limit=START_SLICES + 2)
        await harness.commit(1)
        assert len(harness.chunks(MAIN_CHANNEL)) == 2

        await harness.add_member(DSP_ID)
        harness.allow_source(None)
        await harness.commit(30)

        assert DSP_ID not in harness.session._join_catchup
        channel_id = harness.session._member_pipelines[DSP_ID].channel_id
        _assert_aligned(harness.offsets_us(channel_id))
        main = harness.push_stream.get_cached_pcm_chunks(MAIN_CHANNEL)
        member = [
            chunk
            for chunk in harness.push_stream.get_cached_pcm_chunks(channel_id)
            if _first_frame(chunk.pcm_data) >= 0
        ]
        # the member has audio from its first chunk to the end of the stream, without a hole
        assert all(
            later.timestamp_us == earlier.timestamp_us + earlier.duration_us
            for earlier, later in pairwise(member)
        )
        assert member[-1].timestamp_us == main[-1].timestamp_us
        # the first commit carries the start queue, the third one the last slice queued
        assert member[0].timestamp_us <= main[2].timestamp_us
    finally:
        await harness.close()
