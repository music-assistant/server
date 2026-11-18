"""Teardown check: an interrupted crossfade must not leave its ffmpeg mixer running."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Iterator
from contextlib import aclosing, suppress

import numpy as np
import pytest
from music_assistant_models.enums import ContentType
from music_assistant_models.media_items import AudioFormat

import music_assistant.helpers.process as process_module
from music_assistant.controllers.streams.smart_fades.fades import StandardCrossFade
from music_assistant.controllers.streams.smart_fades.mixer import SmartFadesMixer

PCM = AudioFormat(content_type=ContentType.PCM_F32LE, sample_rate=44100, bit_depth=32, channels=2)
SR = 44100


def _tone(freq: float, seconds: float, level: float = 0.2) -> bytes:
    """Return a stereo-interleaved sine tone as raw PCM bytes."""
    t = np.arange(int(SR * seconds)) / SR
    mono = (level * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    return np.repeat(mono, 2).tobytes()


class _StubStreams:
    """Minimal stand-in for the StreamsController the mixer only reads a logger from."""

    logger = logging.getLogger("test.smart_fades")


@pytest.fixture
def tracked_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[list[process_module.AsyncProcess]]:
    """Record every ffmpeg process the mixer spawns, so a test can check it was reaped."""
    procs: list[process_module.AsyncProcess] = []
    original_start = process_module.AsyncProcess.start

    async def _tracked_start(self: process_module.AsyncProcess) -> None:
        await original_start(self)
        procs.append(self)

    monkeypatch.setattr(process_module.AsyncProcess, "start", _tracked_start)
    yield procs
    # best-effort: don't let a failing test leak an ffmpeg process into the next one
    for proc in procs:
        if proc.returncode is None and proc.proc is not None:
            with suppress(ProcessLookupError):
                proc.proc.kill()


def _built_fade() -> tuple[StandardCrossFade, bytes]:
    """Build a standard crossfade over a full 4s overlap and return it with its fade-out."""
    fade = StandardCrossFade(logging.getLogger(), crossfade_duration=4)
    fade_out = _tone(220.0, 6.0)
    fade.build(len(fade_out), len(_tone(440.0, 6.0)), PCM)
    return fade, fade_out


async def _incoming(chunk_seconds: float = 0.1) -> AsyncGenerator[bytes]:
    """Yield an 8s incoming track in small chunks, enough to run the crossfade."""
    data = _tone(440.0, 8.0)
    step = int(SR * chunk_seconds) * 2 * 4  # stereo float32 frames
    for start in range(0, len(data), step):
        yield data[start : start + step]


async def _stalling_incoming(gate: asyncio.Event) -> AsyncGenerator[bytes]:
    """Yield part of the overlap, then park on ``gate`` so the mix stays mid-flight."""
    yield _tone(440.0, 0.5)
    yield _tone(440.0, 0.5, level=0.19)
    await gate.wait()
    yield b""


@pytest.mark.asyncio
async def test_break_then_aclose_reaps_ffmpeg(
    tracked_processes: list[process_module.AsyncProcess],
) -> None:
    """The single-item boundary path: consume part of the mix, then close it early."""
    mixer = SmartFadesMixer(_StubStreams())  # type: ignore[arg-type]
    fade, fade_out = _built_fade()
    mix = mixer.mix(fade, _incoming(), fade_out, PCM)

    pulled = 0
    async for _chunk in mix:
        pulled += 1
        if pulled >= 3:
            break
    assert tracked_processes, "the crossfade never spawned its ffmpeg mixer"

    # the close must reap the mixer here and now, not hang or defer to a GC finalizer
    started = asyncio.get_event_loop().time()
    async with asyncio.timeout(10):
        await mix.aclose()
    assert asyncio.get_event_loop().time() - started < 3
    assert all(proc.returncode is not None for proc in tracked_processes)


@pytest.mark.asyncio
async def test_flow_cancellation_reaps_ffmpeg(
    tracked_processes: list[process_module.AsyncProcess],
) -> None:
    """The flow path: the task consuming the mix is cancelled while it is still mixing."""
    mixer = SmartFadesMixer(_StubStreams())  # type: ignore[arg-type]
    fade, fade_out = _built_fade()
    gate = asyncio.Event()
    mix = mixer.mix(fade, _stalling_incoming(gate), fade_out, PCM)

    async def _consume() -> None:
        async with aclosing(mix):
            async for _chunk in mix:
                pass

    task = asyncio.create_task(_consume())
    # wait until the mixer ffmpeg is up and the incoming track has stalled
    async with asyncio.timeout(10):
        while not tracked_processes:
            await asyncio.sleep(0.05)
    await asyncio.sleep(0.3)

    started = asyncio.get_event_loop().time()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert asyncio.get_event_loop().time() - started < 3
    assert all(proc.returncode is not None for proc in tracked_processes)
    gate.set()


@pytest.mark.asyncio
async def test_mix_output_matches_direct_apply(
    tracked_processes: list[process_module.AsyncProcess],
) -> None:
    """The pump wrapper must not alter the audio: same bytes as consuming apply() directly."""
    mixer = SmartFadesMixer(_StubStreams())  # type: ignore[arg-type]
    fade, fade_out = _built_fade()
    via_mixer = b"".join([chunk async for chunk in mixer.mix(fade, _incoming(), fade_out, PCM)])

    fade_direct, fade_out_direct = _built_fade()
    via_apply = b"".join(
        [chunk async for chunk in fade_direct.apply(fade_out_direct, _incoming(), PCM)]
    )

    assert via_mixer == via_apply
    assert all(proc.returncode is not None for proc in tracked_processes)
