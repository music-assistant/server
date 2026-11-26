"""Tests for a live source that stops delivering audio on purpose while paused."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import AudioError
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

import music_assistant.controllers.streams.audio as audio_mod
from music_assistant.controllers.streams.audio import PAUSED_SOURCE_SILENCE_SECONDS, StreamsAudio
from music_assistant.helpers.audio import calculate_content_length
from music_assistant.models.media_capabilities import AudioSourceMixin, SourcePause

_CHUNK = b"\x00\x01" * 256


class _GatedFFMpeg:
    """FFMpeg double that delivers a chunk, holds until released, then delivers another."""

    last_instance: _GatedFFMpeg | None = None
    release: asyncio.Event

    def __init__(self, *, input_format: AudioFormat, **_kwargs: Any) -> None:
        self.input_format = input_format
        self.parsed_duration: int | None = None
        self.returncode: int | None = None
        self.log_history: list[str] = []
        self.proc = MagicMock(pid=1234)
        self.stdin_feeder_exception: Exception | None = None
        self.torn_down_via: str | None = None
        self.read_cancelled = False
        type(self).last_instance = self

    async def start(self) -> None:
        """Start the fake process."""

    async def iter_chunked(self, _chunk_size: int) -> AsyncGenerator[bytes]:
        """Yield a chunk, wait for the gate, then yield the last chunk."""
        yield _CHUNK
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.read_cancelled = True
            raise
        yield _CHUNK

    async def wait_with_timeout(self, _timeout: float) -> None:
        """Reap the fake process."""
        self.returncode = 0

    async def close(self) -> None:
        """Close the fake process."""
        self.torn_down_via = "close"
        self.returncode = 0

    async def kill(self) -> None:
        """Kill the fake process."""
        self.torn_down_via = "kill"
        self.returncode = -9


class _StartStallFFMpeg(_GatedFFMpeg):
    """FFMpeg double that never delivers its first chunk."""

    async def iter_chunked(self, _chunk_size: int) -> AsyncGenerator[bytes]:
        """Block before the first chunk."""
        await self.release.wait()
        yield _CHUNK


def _pcm_format() -> AudioFormat:
    return AudioFormat(
        content_type=ContentType.PCM_S16LE,
        codec_type=ContentType.PCM_S16LE,
        sample_rate=44100,
        bit_depth=16,
        channels=2,
    )


def _streamdetails() -> StreamDetails:
    return StreamDetails(
        provider="live_source",
        item_id="main",
        audio_format=_pcm_format(),
        decoded_audio_format=_pcm_format(),
        media_type=MediaType.AUDIO_SOURCE,
        stream_type=StreamType.NAMED_PIPE,
        path="/tmp/fake-fifo",  # noqa: S108
    )


def _audio_with_source(provider: Any) -> StreamsAudio:
    mass = MagicMock()
    mass.loop.time = MagicMock(return_value=0.0)
    mass.get_provider = MagicMock(return_value=provider)
    return StreamsAudio(mass)


def _source(paused: bool | SourcePause | Exception) -> MagicMock:
    """
    Build a live source provider that reports the given pause.

    :param paused: The pause to report (True fills it with silence, False is none),
        or an error to raise when asked.
    """
    provider = MagicMock(spec=AudioSourceMixin)
    provider.domain = "live_source"
    if isinstance(paused, Exception):
        provider.get_source_pause = MagicMock(side_effect=paused)
    elif isinstance(paused, SourcePause):
        provider.get_source_pause = MagicMock(return_value=paused)
    else:
        pause = SourcePause.FILL_SILENCE if paused else SourcePause.NONE
        provider.get_source_pause = MagicMock(return_value=pause)
    return provider


async def _collect(gen: AsyncGenerator[bytes]) -> list[bytes]:
    return [chunk async for chunk in gen]


@pytest.fixture
def gated_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> type[_GatedFFMpeg]:
    """Swap the real FFMpeg for the gated double, with short stall timeouts."""
    _GatedFFMpeg.last_instance = None
    _GatedFFMpeg.release = asyncio.Event()
    monkeypatch.setattr(audio_mod, "FFMpeg", _GatedFFMpeg)
    monkeypatch.setattr(audio_mod, "STREAM_START_TIMEOUT", 0.05)
    monkeypatch.setattr(audio_mod, "STREAM_STALL_TIMEOUT", 0.05)
    return _GatedFFMpeg


async def test_a_paused_source_keeps_its_stream(gated_ffmpeg: type[_GatedFFMpeg]) -> None:
    """A paused source outlasts the stall timeout, fed with silence, and resumes on its read."""
    audio = _audio_with_source(_source(paused=True))
    loop = asyncio.get_running_loop()
    loop.call_later(0.35, gated_ffmpeg.release.set)

    chunks = await _collect(audio.get_media_stream(_streamdetails(), _pcm_format()))

    silence = b"\x00" * calculate_content_length(_pcm_format(), PAUSED_SOURCE_SILENCE_SECONDS)
    assert chunks[0] == _CHUNK
    assert chunks[-1] == _CHUNK
    assert len(chunks) >= 4
    assert all(chunk == silence for chunk in chunks[1:-1])
    assert gated_ffmpeg.last_instance is not None
    assert gated_ffmpeg.last_instance.read_cancelled is False


async def test_a_held_source_waits_without_silence(gated_ffmpeg: type[_GatedFFMpeg]) -> None:
    """A source held together with its player keeps its stream without feeding it."""
    audio = _audio_with_source(_source(paused=SourcePause.HOLD))
    asyncio.get_running_loop().call_later(0.3, gated_ffmpeg.release.set)

    chunks = await _collect(audio.get_media_stream(_streamdetails(), _pcm_format()))

    assert chunks == [_CHUNK, _CHUNK]
    assert gated_ffmpeg.last_instance is not None
    assert gated_ffmpeg.last_instance.read_cancelled is False


async def test_a_non_pcm_stream_waits_out_a_pause_without_silence(
    gated_ffmpeg: type[_GatedFFMpeg],
) -> None:
    """Silence is only fed into PCM: other output waits out the pause on the same read."""
    audio = _audio_with_source(_source(paused=True))
    asyncio.get_running_loop().call_later(0.3, gated_ffmpeg.release.set)
    mp3 = AudioFormat(content_type=ContentType.MP3)

    chunks = await _collect(audio.get_media_stream(_streamdetails(), mp3))

    assert chunks == [_CHUNK, _CHUNK]


async def test_a_source_that_is_not_paused_still_stalls(
    gated_ffmpeg: type[_GatedFFMpeg],
) -> None:
    """Silence from a source that is not paused is a stall, as before."""
    audio = _audio_with_source(_source(paused=False))

    with pytest.raises(AudioError, match="stalled"):
        await _collect(audio.get_media_stream(_streamdetails(), _pcm_format()))

    assert gated_ffmpeg.last_instance is not None
    assert gated_ffmpeg.last_instance.read_cancelled is True
    assert gated_ffmpeg.last_instance.torn_down_via == "kill"


@pytest.mark.usefixtures("gated_ffmpeg")
async def test_stall_detection_resumes_when_the_pause_ends() -> None:
    """A source that stays silent after its pause ended is ended as stalled."""
    provider = _source(paused=True)
    asyncio.get_running_loop().call_later(
        0.2, lambda: setattr(provider.get_source_pause, "return_value", SourcePause.NONE)
    )
    audio = _audio_with_source(provider)

    with pytest.raises(AudioError, match="stalled"):
        await _collect(audio.get_media_stream(_streamdetails(), _pcm_format()))


@pytest.mark.usefixtures("gated_ffmpeg")
async def test_a_source_that_never_started_is_not_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The paused state only covers a source that delivered audio before."""
    monkeypatch.setattr(audio_mod, "FFMpeg", _StartStallFFMpeg)
    provider = _source(paused=True)
    audio = _audio_with_source(provider)

    # the source reports itself paused throughout, and still ends as stalled
    with pytest.raises(AudioError, match="stalled"):
        await _collect(audio.get_media_stream(_streamdetails(), _pcm_format()))


@pytest.mark.usefixtures("gated_ffmpeg")
async def test_a_failing_provider_does_not_hide_a_stall() -> None:
    """A provider that fails to answer counts as not paused."""
    audio = _audio_with_source(_source(paused=RuntimeError("boom")))

    with pytest.raises(AudioError, match="stalled"):
        await _collect(audio.get_media_stream(_streamdetails(), _pcm_format()))


@pytest.mark.usefixtures("gated_ffmpeg")
async def test_a_provider_without_live_sources_is_not_asked() -> None:
    """A provider that delivers no live sources is never paused."""
    audio = _audio_with_source(MagicMock(spec=object))

    with pytest.raises(AudioError, match="stalled"):
        await _collect(audio.get_media_stream(_streamdetails(), _pcm_format()))


async def test_cancelling_a_paused_stream_cancels_its_pending_read(
    gated_ffmpeg: type[_GatedFFMpeg],
) -> None:
    """A stream torn down while paused leaves no read running behind it."""
    audio = _audio_with_source(_source(paused=True))
    task = asyncio.create_task(_collect(audio.get_media_stream(_streamdetails(), _pcm_format())))
    await asyncio.sleep(0.2)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert gated_ffmpeg.last_instance is not None
    assert gated_ffmpeg.last_instance.read_cancelled is True
    assert gated_ffmpeg.last_instance.torn_down_via == "kill"


async def test_a_resume_restarts_the_stall_timeout_in_full(
    gated_ffmpeg: type[_GatedFFMpeg], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Audio arriving soon after a resume is not cut off by a timeout that ran while held."""
    monkeypatch.setattr(audio_mod, "STREAM_STALL_TIMEOUT", 0.1)
    monkeypatch.setattr(audio_mod, "PAUSED_SOURCE_HOLD_POLL_SECONDS", 0.01)
    provider = _source(paused=SourcePause.HOLD)
    loop = asyncio.get_running_loop()
    # resumed just before a full timeout would have run out while held, audio 30 ms later
    loop.call_later(
        0.19, lambda: setattr(provider.get_source_pause, "return_value", SourcePause.NONE)
    )
    loop.call_later(0.22, gated_ffmpeg.release.set)
    audio = _audio_with_source(provider)

    chunks = await _collect(audio.get_media_stream(_streamdetails(), _pcm_format()))

    assert len(chunks) == 2


async def test_a_pause_that_starts_during_a_read_is_seen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pause beginning mid-read gets its silence, and its time is not counted as a stall."""
    monkeypatch.setattr(audio_mod, "PAUSED_SOURCE_HOLD_POLL_SECONDS", 0.01)
    gate = asyncio.Event()

    async def _gated() -> AsyncGenerator[bytes]:
        await gate.wait()
        yield _CHUNK

    pause = [SourcePause.NONE]
    reader = audio_mod._SourceChunkReader(_gated(), lambda: pause[0], _pcm_format())
    loop = asyncio.get_running_loop()
    try:
        # paused just after the read starts: the silence follows straight away
        loop.call_later(0.01, lambda: pause.__setitem__(0, SourcePause.FILL_SILENCE))
        started = loop.time()
        silence = await reader.read(timeout=0.5, delivered_audio=True)
        assert silence
        assert not any(silence)
        assert loop.time() - started < 0.2

        # held from mid-read for longer than the timeout, then resumed: the audio
        # 0.3 s after the resume is within the fresh budget, not a stall
        pause[0] = SourcePause.NONE
        loop.call_later(0.01, lambda: pause.__setitem__(0, SourcePause.HOLD))
        loop.call_later(0.35, lambda: pause.__setitem__(0, SourcePause.NONE))
        loop.call_later(0.65, gate.set)

        assert await reader.read(timeout=0.5, delivered_audio=True) == _CHUNK
    finally:
        await reader.close()
