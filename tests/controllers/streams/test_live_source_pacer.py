"""Tests for holding a live source's reads to real time across pauses."""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import ContentType
from music_assistant_models.media_items import AudioFormat

import music_assistant.controllers.streams.audio as audio_mod
from music_assistant.controllers.streams.audio import _LiveSourcePacer
from music_assistant.helpers.audio import calculate_content_length
from music_assistant.models.media_capabilities import SourcePause

_PCM = AudioFormat(
    content_type=ContentType.PCM_S16LE,
    codec_type=ContentType.PCM_S16LE,
    sample_rate=44100,
    bit_depth=16,
    channels=2,
)
_CHUNK = b"\x00" * calculate_content_length(_PCM, 0.01)


class _Pause:
    """A source whose pause can be switched from the test."""

    def __init__(self) -> None:
        self.value = SourcePause.NONE

    def __call__(self) -> SourcePause:
        return self.value


@pytest.fixture
def signals(monkeypatch: pytest.MonkeyPatch) -> list[signal.Signals]:
    """Record the signals sent to the reader, with a short read-ahead and poll."""
    sent: list[signal.Signals] = []
    monkeypatch.setattr(audio_mod, "LIVE_SOURCE_READ_AHEAD_SECONDS", 0.05)
    monkeypatch.setattr(audio_mod, "LIVE_SOURCE_PAUSE_POLL_SECONDS", 0.005)
    monkeypatch.setattr(os, "kill", lambda _pid, sig: sent.append(sig))
    return sent


def _ffmpeg(returncode: int | None = None) -> Any:
    ffmpeg = MagicMock()
    ffmpeg.proc = MagicMock(pid=4321)
    ffmpeg.returncode = returncode
    return ffmpeg


async def _burst(seconds: float) -> AsyncGenerator[bytes]:
    """Yield the given amount of audio as fast as it is read."""
    for _ in range(round(seconds / 0.01)):
        yield _CHUNK


async def _drain(pacer: _LiveSourcePacer, chunks: AsyncGenerator[bytes]) -> float:
    loop = asyncio.get_running_loop()
    started = loop.time()
    async for _ in pacer.pace(chunks):
        pass
    return loop.time() - started


@pytest.mark.usefixtures("signals")
async def test_reads_are_held_to_real_time() -> None:
    """Audio offered faster than real time is let through at real time plus the read-ahead."""
    pacer = _LiveSourcePacer(_ffmpeg(), _Pause(), _PCM)

    elapsed = await _drain(pacer, _burst(0.4))

    assert elapsed >= 0.3


@pytest.mark.usefixtures("signals")
async def test_paused_time_does_not_count_as_played() -> None:
    """After a pause, the source is not allowed to read ahead by the length of the pause."""
    pause = _Pause()
    pacer = _LiveSourcePacer(_ffmpeg(), pause, _PCM)
    pacer.start()
    loop = asyncio.get_running_loop()
    resumed_at: list[float] = []

    async def source() -> AsyncGenerator[bytes]:
        async for chunk in _burst(0.1):
            yield chunk
        pause.value = SourcePause.HOLD
        await asyncio.sleep(0.3)
        pause.value = SourcePause.NONE
        await asyncio.sleep(0.02)
        resumed_at.append(loop.time())
        async for chunk in _burst(0.1):
            yield chunk

    await _drain(pacer, source())
    await pacer.close()

    # the second 0.1 s waits for real time: without the pause excluded it would pass at once
    assert loop.time() - resumed_at[0] >= 0.07


def _first_audio_read(pacer: _LiveSourcePacer) -> None:
    """Mark the source's first audio as read, as pace() does with the first chunk."""
    pacer._started = asyncio.get_running_loop().time()


async def test_the_reader_is_frozen_while_the_source_is_paused(
    signals: list[signal.Signals],
) -> None:
    """Ffmpeg is stopped when the source pauses and continued when it resumes."""
    pause = _Pause()
    pacer = _LiveSourcePacer(_ffmpeg(), pause, _PCM)
    _first_audio_read(pacer)
    pacer.start()

    pause.value = SourcePause.FILL_SILENCE
    await asyncio.sleep(0.03)
    pause.value = SourcePause.NONE
    await asyncio.sleep(0.03)
    await pacer.close()

    assert signals == [signal.SIGSTOP, signal.SIGCONT]


async def test_closing_lets_a_frozen_reader_run_again(signals: list[signal.Signals]) -> None:
    """A stream torn down while paused continues its ffmpeg first, so it can be stopped."""
    pause = _Pause()
    pause.value = SourcePause.HOLD
    pacer = _LiveSourcePacer(_ffmpeg(), pause, _PCM)
    _first_audio_read(pacer)
    pacer.start()
    await asyncio.sleep(0.03)

    await pacer.close()

    assert signals == [signal.SIGSTOP, signal.SIGCONT]


async def test_a_pause_before_the_first_audio_leaves_the_reader_running(
    signals: list[signal.Signals],
) -> None:
    """Nothing was counted as played yet, so the reader is not frozen and no time is lost."""
    pause = _Pause()
    pause.value = SourcePause.HOLD
    pacer = _LiveSourcePacer(_ffmpeg(), pause, _PCM)
    pacer.start()
    await asyncio.sleep(0.03)

    await pacer.close()

    assert signals == []
    assert pacer._paused_total == 0


async def test_an_exited_reader_is_not_signalled(signals: list[signal.Signals]) -> None:
    """No signal goes to an ffmpeg that already exited (its pid may be reused)."""
    pause = _Pause()
    pause.value = SourcePause.HOLD
    pacer = _LiveSourcePacer(_ffmpeg(returncode=0), pause, _PCM)
    _first_audio_read(pacer)
    pacer.start()
    await asyncio.sleep(0.03)

    await pacer.close()

    assert signals == []
