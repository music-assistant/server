"""
Tests that a member's DSP channel carries the same content as MAIN at every timestamp.

The source is a ramp whose sample value is its frame index plus one, the member DSP is an
identity FIFO, and the real PushStream records every committed chunk per channel, so
the content offset of a DSP channel against MAIN can be read off its PCM cache.
"""

from __future__ import annotations

import asyncio
import struct
from collections import Counter, deque
from contextlib import suppress
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import numpy as np
from aiosendspin.clock import ManualClock
from aiosendspin.server.audio import AudioFormat as SendspinAudioFormat
from aiosendspin.server.push_stream import MAIN_CHANNEL, PushStream
from music_assistant_models.enums import ContentType
from music_assistant_models.media_items.audio_format import AudioFormat

from music_assistant.providers.sendspin import playback
from music_assistant.providers.sendspin.playback import (
    _PRODUCER_BUFFER_LIMIT_US,
    SendspinPlaybackSession,
    _PipelineConfig,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from uuid import UUID

    import pytest

SAMPLE_RATE = 48000
SLICE_FRAMES = 4800
PCM_FORMAT = AudioFormat(
    content_type=ContentType.PCM_F32LE, sample_rate=SAMPLE_RATE, bit_depth=32, channels=2
)
SENDSPIN_FORMAT = SendspinAudioFormat(
    sample_rate=SAMPLE_RATE, bit_depth=32, channels=2, sample_type="float"
)
LEADER_ID = "leader"
DSP_ID = "dsp-member"
PLAIN_ID = "plain-member"


def _ramp_slice(index: int) -> bytes:
    """Return slice `index` of a stereo ramp whose value is frame index + 1."""
    start = index * SLICE_FRAMES + 1
    frames = np.arange(start, start + SLICE_FRAMES, dtype=np.float32)
    return np.repeat(frames, 2).tobytes()


def _first_frame(pcm: bytes) -> int:
    """Return the source frame index a chunk starts with, or -1 for silence."""
    return int(struct.unpack_from("<f", pcm)[0]) - 1


class _FifoFFMpeg:
    """Identity DSP: ffmpeg output equals its input, byte for byte."""

    def __init__(
        self, on_start: Callable[[], Awaitable[None]] | None, *, yielding_writes: bool
    ) -> None:
        self._reader = asyncio.StreamReader(limit=2**30)
        self._on_start = on_start
        self._yielding_writes = yielding_writes
        self.on_write: Callable[[], None] | None = None
        self._eof = False
        self._closed = False

    async def start(self) -> None:
        if self._on_start is not None:
            await self._on_start()

    async def write(self, data: bytes) -> None:
        if self.on_write is not None:
            self.on_write()
        if self._yielding_writes:
            # Yield like the stdin drain of a real ffmpeg, so concurrent writers interleave.
            await asyncio.sleep(0)
        if not self._eof and not self._closed:
            self._reader.feed_data(data)

    async def write_eof(self) -> None:
        if not self._eof:
            self._eof = True
            self._reader.feed_eof()

    async def read(self, n: int) -> bytes:
        if self._closed:
            return b""
        return await self._reader.read(n)

    async def readexactly(self, n: int) -> bytes:
        if self._closed:
            return b""
        try:
            return await self._reader.readexactly(n)
        except asyncio.IncompleteReadError as err:
            return err.partial

    async def close(self) -> None:
        self._closed = True
        await self.write_eof()


class _SessionHarness:
    """Drives a real playback session one commit at a time against a real PushStream."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        dsp_ids: set[str],
        *,
        yielding_writes: bool = False,
    ) -> None:
        self.clock = ManualClock()
        self.push_stream = PushStream(
            loop=asyncio.get_running_loop(),
            clock=self.clock,
            group=SimpleNamespace(clients=[]),  # type: ignore[arg-type]
        )
        monkeypatch.setattr(self.push_stream, "sleep_to_limit_buffer", self._sleep_to_limit_buffer)
        self.commit_permits = asyncio.Semaphore(0)
        self.released = 0
        self.entered = 0
        self.returned = 0
        self._source_limit: int | None = None
        self.source_ended = False
        self._source_wakeup = asyncio.Event()
        self.start_hooks: deque[Callable[[], Awaitable[None]] | None] = deque()
        self.ffmpegs: list[_FifoFFMpeg] = []
        self._yielding_writes = yielding_writes

        player = MagicMock(player_id=LEADER_ID)
        player.mass.streams.get_stream = MagicMock(side_effect=lambda *_a, **_k: self._source())
        self.session = SendspinPlaybackSession(player)
        session = self.session
        monkeypatch.setattr(playback, "import_module_in_thread", AsyncMock())
        monkeypatch.setattr(session, "_get_start_streamdetails", MagicMock(return_value=None))
        monkeypatch.setattr(session, "_follow_session_sample_rate", AsyncMock())
        monkeypatch.setattr(
            session,
            "_select_session_pcm_formats",
            MagicMock(return_value=(PCM_FORMAT, SENDSPIN_FORMAT)),
        )
        monkeypatch.setattr(session, "_is_live_source", MagicMock(return_value=False))
        monkeypatch.setattr(
            session, "_create_push_stream", MagicMock(return_value=self.push_stream)
        )
        monkeypatch.setattr(session, "_wait_for_buffer_drain", AsyncMock())
        monkeypatch.setattr(session, "_stop_push_stream", MagicMock())
        monkeypatch.setattr(
            session,
            "_read_pipeline_config",
            lambda player_id: _PipelineConfig(
                requires_transform=player_id in dsp_ids,
                output_channels="stereo",
                filter_params=("volume=1.0",) if player_id in dsp_ids else (),
            ),
        )
        monkeypatch.setattr(session, "_create_member_ffmpeg", self._create_ffmpeg)

    async def settle(self) -> None:
        """Let every other task run until it blocks, as a real 100 ms sleep would."""
        for _ in range(200):
            await asyncio.sleep(0)

    async def start(self, source_limit: int | None = None) -> None:
        """Start playback and wait until the first commit is done."""
        self.allow_source(source_limit)
        await self.session.start(MagicMock())
        await self._until(self._parked)
        await self.settle()

    async def commit(self, count: int) -> None:
        """Let the consumer finish `count` more commits, one sleep at a time."""
        for _ in range(count):
            await self._until(self._parked)
            self.released += 1
            self.commit_permits.release()
            await self._until(self._parked)
            await self.settle()

    async def add_member(self, player_id: str) -> None:
        """Add a member on a pre-assigned channel whose committed PCM is cached."""
        channel_id = uuid4()
        self.push_stream.enable_pcm_cache_for_channel(channel_id)
        self.session._preassigned_channels[player_id] = channel_id
        await self.session.add_member(player_id)

    async def finish(self) -> None:
        """Let the consumer commit until playback ends."""
        task = self.session.playback_task
        assert task is not None
        for _ in range(1000):
            if task.done():
                await task
                return
            self.commit_permits.release()
            await self.settle()
        raise AssertionError("playback did not end")

    def allow_source(self, limit: int | None) -> None:
        """Let the source produce slices up to `limit`, or without limit for None."""
        self._source_limit = limit
        self._source_wakeup.set()

    def end_source(self) -> None:
        """End the source once it has produced every allowed slice."""
        self.source_ended = True
        self._source_wakeup.set()

    def release_commit_soon(self) -> None:
        """Schedule the consumer's sleep to end in the next event loop iteration."""
        self.released += 1
        asyncio.get_running_loop().call_soon(self.commit_permits.release)

    async def close(self) -> None:
        task = self.session.playback_task
        if task is not None:
            self.session._cancel_requested = True
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task

    def chunks(self, channel_id: UUID) -> list[tuple[int, int]]:
        return [
            (chunk.timestamp_us, _first_frame(chunk.pcm_data))
            for chunk in self.push_stream.get_cached_pcm_chunks(channel_id)
        ]

    def offsets_us(self, channel_id: UUID) -> Counter[int]:
        """Count chunks by content offset against MAIN, positive meaning the member is early."""
        main = self.chunks(MAIN_CHANNEL)
        main_ts0, main_frame0 = main[0]
        for ts, frame in main:
            assert frame == main_frame0 + (ts - main_ts0) * SAMPLE_RATE // 1_000_000
        offsets: Counter[int] = Counter()
        for ts, frame in self.chunks(channel_id):
            if frame < 0:
                continue
            expected = main_frame0 + (ts - main_ts0) * SAMPLE_RATE // 1_000_000
            offsets[(frame - expected) * 1_000_000 // SAMPLE_RATE] += 1
        return offsets

    def _create_ffmpeg(self, _params: object) -> _FifoFFMpeg:
        ffmpeg = _FifoFFMpeg(
            self.start_hooks.popleft() if self.start_hooks else None,
            yielding_writes=self._yielding_writes,
        )
        self.ffmpegs.append(ffmpeg)
        return ffmpeg

    async def _source(self) -> AsyncIterator[bytes]:
        index = 0
        while True:
            while self._source_limit is not None and index >= self._source_limit:
                if self.source_ended:
                    return
                self._source_wakeup.clear()
                await self._source_wakeup.wait()
            yield _ramp_slice(index)
            index += 1

    async def _sleep_to_limit_buffer(self, max_buffer_us: int) -> None:
        assert max_buffer_us == _PRODUCER_BUFFER_LIMIT_US
        self.entered += 1
        await self.commit_permits.acquire()
        self.returned += 1

    async def _until(self, condition: Callable[[], bool]) -> None:
        for _ in range(20_000):
            if condition():
                return
            await asyncio.sleep(0)
        raise AssertionError("playback loop stalled")

    def _parked(self) -> bool:
        return self.returned == self.released and self.entered == self.released + 1


def _assert_aligned(offsets: Counter[int]) -> None:
    assert offsets, "no DSP content was committed"
    assert set(offsets) == {0}, (
        f"DSP channel content offset vs MAIN, us -> chunks (positive = member early): "
        f"{dict(sorted(offsets.items()))}"
    )


async def test_join_fanout_before_history_feed_keeps_alignment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DSP joiner stays aligned when the next commit's fanout beats the history feed."""
    harness = _SessionHarness(monkeypatch, dsp_ids={DSP_ID})
    try:
        await harness.start()
        await harness.commit(20)

        async def end_consumer_sleep_with_catchup_start() -> None:
            # The consumer's sleep ends in the same loop iteration the catch-up ffmpeg start
            # completes, so it runs right after add_member and before the history feed resumes.
            resumed = asyncio.get_running_loop().create_future()
            asyncio.get_running_loop().call_soon(resumed.set_result, None)
            harness.release_commit_soon()
            await resumed

        # add_member starts the member's live pipeline ffmpeg first, then the catch-up ffmpeg.
        harness.start_hooks.extend([None, end_consumer_sleep_with_catchup_start])
        await harness.add_member(DSP_ID)
        await harness.commit(30)

        assert DSP_ID not in harness.session._join_catchup
        channel_id = harness.session._member_pipelines[DSP_ID].channel_id
        _assert_aligned(harness.offsets_us(channel_id))
    finally:
        await harness.close()


async def test_promotion_flushes_join_backlog_before_live_pushes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A promoted DSP joiner stays aligned when live pushes arrive during the backlog flush."""
    harness = _SessionHarness(monkeypatch, dsp_ids={DSP_ID}, yielding_writes=True)
    try:
        await harness.start()
        await harness.commit(20)
        await harness.add_member(DSP_ID)
        await harness.commit(80)

        assert DSP_ID not in harness.session._join_catchup
        channel_id = harness.session._member_pipelines[DSP_ID].channel_id
        _assert_aligned(harness.offsets_us(channel_id))
    finally:
        await harness.close()


async def test_leader_switch_to_dsp_member_keeps_alignment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the leader leaves, the DSP member taking over keeps playing in sync."""
    harness = _SessionHarness(monkeypatch, dsp_ids={DSP_ID})
    try:
        await harness.add_member(DSP_ID)
        await harness.add_member(PLAIN_ID)
        await harness.start()
        await harness.commit(20)
        _assert_aligned(harness.offsets_us(harness.session._member_pipelines[DSP_ID].channel_id))

        # SendspinPlayer.set_members on the old leader, then the new leader's membership sync.
        await harness.session.transfer_to(MagicMock(player_id=DSP_ID))
        await harness.session.sync_members({PLAIN_ID})
        # PlayerV1Role.get_audio_requirements re-resolves its channel on every call.
        leader_channel = harness.session._resolve_channel_for_player(DSP_ID)
        harness.push_stream.enable_pcm_cache_for_channel(leader_channel)
        await harness.commit(30)

        _assert_aligned(harness.offsets_us(leader_channel))
    finally:
        await harness.close()


async def test_join_before_first_history_append_keeps_alignment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DSP member joining before the first commit reaches history plays in sync."""
    harness = _SessionHarness(monkeypatch, dsp_ids={DSP_ID})
    try:
        # The producer is 4 slices ahead of the first commit, which is not in history yet.
        await harness.start(source_limit=5)
        assert not harness.session._history

        await harness.add_member(DSP_ID)
        harness.allow_source(None)
        await harness.commit(30)

        channel_id = harness.session._member_pipelines[DSP_ID].channel_id
        _assert_aligned(harness.offsets_us(channel_id))
    finally:
        await harness.close()


async def test_source_end_during_promotion_keeps_join_backlog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DSP joiner promoted while the source ends still plays the end of the stream."""
    harness = _SessionHarness(monkeypatch, dsp_ids={DSP_ID}, yielding_writes=True)
    try:
        await harness.start(source_limit=40)
        await harness.commit(20)
        await harness.add_member(DSP_ID)
        channel_id = harness.session._member_pipelines[DSP_ID].channel_id
        catchup_ffmpeg = harness.ffmpegs[-1]

        # End the source while the promotion flushes the join backlog into the catch-up ffmpeg.
        def end_source_once_promoted() -> None:
            if DSP_ID not in harness.session._join_catchup:
                harness.end_source()

        catchup_ffmpeg.on_write = end_source_once_promoted
        await harness.finish()

        assert harness.source_ended
        _assert_aligned(harness.offsets_us(channel_id))
        assert harness.chunks(channel_id)[-1] == harness.chunks(MAIN_CHANNEL)[-1]
    finally:
        await harness.close()


async def test_promoted_dsp_joiner_has_audio_at_every_main_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A promoted DSP joiner carries audio, not silence, at the history/live seam."""
    harness = _SessionHarness(monkeypatch, dsp_ids={DSP_ID})
    try:
        await harness.start()
        await harness.commit(20)
        await harness.add_member(DSP_ID)
        await harness.commit(30)

        assert DSP_ID not in harness.session._join_catchup
        channel_id = harness.session._member_pipelines[DSP_ID].channel_id
        # The ramp source has no zero samples, so any zero sample is inserted silence.
        dsp_has_audio = {
            chunk.timestamp_us: bool(np.frombuffer(chunk.pcm_data, dtype=np.float32).all())
            for chunk in harness.push_stream.get_cached_pcm_chunks(channel_id)
        }
        first_audio_us = min(ts for ts, has_audio in dsp_has_audio.items() if has_audio)
        without_audio = [
            ts
            for ts, _ in harness.chunks(MAIN_CHANNEL)
            if ts >= first_audio_us and not dsp_has_audio.get(ts, False)
        ]
        assert not without_audio, (
            f"DSP channel silent or missing at MAIN timestamps: {without_audio}"
        )
    finally:
        await harness.close()
