"""Tests for the ffmpeg input arguments StreamsAudio.get_media_stream builds."""

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
from music_assistant.controllers.streams.audio import StreamsAudio

# input args a provider may attach to its StreamDetails (podcastfeed does exactly this).
# Kept as a tuple so the tests below can never assert against a mutated expectation.
_PROVIDER_INPUT_ARGS = ("-user_agent", "Test/1.0")


class _FakeFFMpeg:
    """FFMpeg test double that records the arguments it was constructed with."""

    last_instance: _FakeFFMpeg | None = None

    def __init__(
        self,
        *,
        audio_input: object,
        input_format: AudioFormat,
        extra_input_args: list[str] | None = None,
        **_kwargs: Any,
    ) -> None:
        self.audio_input = audio_input
        self.extra_input_args = extra_input_args
        # Mirror the real FFMpeg, which mutates this object's codec_type after probe.
        # Tests inspect the original `input_format` AudioFormat passed in to confirm
        # which one the controller picked.
        self.input_format = input_format
        self._probed_codec_type = ContentType.FLAC  # arbitrary, distinct from PCM/OGG
        self.parsed_duration: int | None = None
        self.returncode: int | None = 0
        self.log_history: list[str] = []
        self.proc = MagicMock(pid=1234)
        type(self).last_instance = self

    async def start(self) -> None:
        # Simulate ffmpeg's post-probe codec detection: real FFMpeg mutates
        # self.input_format.codec_type once it reads the input header.
        self.input_format.codec_type = self._probed_codec_type

    async def iter_chunked(self, _chunk_size: int) -> AsyncGenerator[bytes]:
        yield b"\x00\x01" * 256

    async def wait_with_timeout(self, _timeout: float) -> None:
        return None

    async def close(self) -> None:
        return None


@pytest.fixture
def patch_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> type[_FakeFFMpeg]:
    """Swap the real FFMpeg in the streams.audio module for the fake."""
    _FakeFFMpeg.last_instance = None
    monkeypatch.setattr(audio_mod, "FFMpeg", _FakeFFMpeg)
    return _FakeFFMpeg


def _make_audio_controller() -> StreamsAudio:
    """Build a StreamsAudio with just enough mass scaffolding to run get_media_stream."""
    audio = StreamsAudio(MagicMock())
    audio.mass.loop = MagicMock()
    audio.mass.loop.time = MagicMock(return_value=0.0)
    return audio


def _make_pcm_format() -> AudioFormat:
    return AudioFormat(
        content_type=ContentType.PCM_S16LE,
        codec_type=ContentType.PCM_S16LE,
        sample_rate=44100,
        bit_depth=16,
        channels=2,
    )


def _make_streamdetails(
    *,
    audio_format: AudioFormat,
    decoded_audio_format: AudioFormat | None = None,
) -> StreamDetails:
    return StreamDetails(
        provider="test_provider",
        item_id="main",
        audio_format=audio_format,
        decoded_audio_format=decoded_audio_format,
        media_type=MediaType.AUDIO_SOURCE,
        stream_type=StreamType.NAMED_PIPE,
        path="/tmp/fake-fifo",  # noqa: S108
    )


def _seekable_streamdetails() -> StreamDetails:
    """Build seekable StreamDetails carrying provider-supplied ffmpeg input args."""
    return StreamDetails(
        provider="test_provider",
        item_id="episode-1",
        audio_format=AudioFormat(content_type=ContentType.MP3),
        media_type=MediaType.PODCAST_EPISODE,
        stream_type=StreamType.HTTP,
        path="http://test.invalid/episode-1.mp3",
        duration=3600,
        can_seek=True,
        allow_seek=True,
        extra_input_args=[*_PROVIDER_INPUT_ARGS],
    )


async def _drain(gen: AsyncGenerator[bytes]) -> None:
    async for _ in gen:
        pass


class _StallingFFMpeg(_FakeFFMpeg):
    """FFMpeg double whose read never produces a chunk (frozen source)."""

    async def iter_chunked(self, _chunk_size: int) -> AsyncGenerator[bytes]:
        await asyncio.Event().wait()  # blocks until the watchdog cancels the read
        yield b""  # unreachable


class _SlowConsumerFFMpeg(_FakeFFMpeg):
    """FFMpeg double that hands over chunks instantly when asked."""

    async def iter_chunked(self, _chunk_size: int) -> AsyncGenerator[bytes]:
        for _ in range(3):
            yield b"\x00\x01" * 256


def _flac_streamdetails() -> StreamDetails:
    return _make_streamdetails(
        audio_format=AudioFormat(
            content_type=ContentType.FLAC,
            codec_type=ContentType.FLAC,
            sample_rate=44100,
            bit_depth=16,
            channels=2,
        )
    )


@pytest.mark.asyncio
async def test_get_media_stream_raises_when_source_stalls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source that stops producing audio is surfaced as an AudioError."""
    monkeypatch.setattr(audio_mod, "FFMpeg", _StallingFFMpeg)
    monkeypatch.setattr(audio_mod, "STREAM_START_TIMEOUT", 0.1)
    monkeypatch.setattr(audio_mod, "STREAM_STALL_TIMEOUT", 0.1)

    audio = _make_audio_controller()
    with pytest.raises(AudioError):
        await _drain(audio.get_media_stream(_flac_streamdetails(), _make_pcm_format()))


@pytest.mark.asyncio
async def test_get_media_stream_does_not_stall_on_slow_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A consumer slower than the stall timeout must not trip the watchdog."""
    monkeypatch.setattr(audio_mod, "FFMpeg", _SlowConsumerFFMpeg)
    monkeypatch.setattr(audio_mod, "STREAM_START_TIMEOUT", 0.1)
    monkeypatch.setattr(audio_mod, "STREAM_STALL_TIMEOUT", 0.1)

    audio = _make_audio_controller()
    chunks = 0
    async for _ in audio.get_media_stream(_flac_streamdetails(), _make_pcm_format()):
        chunks += 1
        await asyncio.sleep(0.3)  # downstream waits far longer than the stall timeout
    assert chunks == 3


@pytest.mark.asyncio
async def test_get_media_stream_prefers_decoded_audio_format(
    patch_ffmpeg: type[_FakeFFMpeg],
) -> None:
    """When decoded_audio_format is set, ffmpeg receives that as input_format."""
    source_format = AudioFormat(
        content_type=ContentType.OGG,
        codec_type=ContentType.VORBIS,
        sample_rate=44100,
        bit_depth=16,
        channels=2,
        bit_rate=320,
    )
    decoded_format = AudioFormat(
        content_type=ContentType.PCM_S16LE,
        codec_type=ContentType.PCM_S16LE,
        sample_rate=44100,
        bit_depth=16,
        channels=2,
    )
    streamdetails = _make_streamdetails(
        audio_format=source_format, decoded_audio_format=decoded_format
    )

    audio = _make_audio_controller()
    await _drain(audio.get_media_stream(streamdetails, _make_pcm_format()))

    assert patch_ffmpeg.last_instance is not None
    assert patch_ffmpeg.last_instance.input_format is decoded_format


@pytest.mark.asyncio
async def test_get_media_stream_falls_back_to_audio_format(
    patch_ffmpeg: type[_FakeFFMpeg],
) -> None:
    """When decoded_audio_format is not set, ffmpeg receives audio_format as input_format."""
    source_format = AudioFormat(
        content_type=ContentType.FLAC,
        codec_type=ContentType.FLAC,
        sample_rate=44100,
        bit_depth=16,
        channels=2,
    )
    streamdetails = _make_streamdetails(audio_format=source_format)

    audio = _make_audio_controller()
    await _drain(audio.get_media_stream(streamdetails, _make_pcm_format()))

    assert patch_ffmpeg.last_instance is not None
    assert patch_ffmpeg.last_instance.input_format is source_format


@pytest.mark.asyncio
@pytest.mark.usefixtures("patch_ffmpeg")
async def test_get_media_stream_does_not_overwrite_source_codec_when_decoded_format_set() -> None:
    """audio_format.codec_type stays authoritative when decoded_audio_format is set."""
    source_format = AudioFormat(
        content_type=ContentType.OGG,
        codec_type=ContentType.VORBIS,
        sample_rate=44100,
        bit_depth=16,
        channels=2,
        bit_rate=320,
    )
    decoded_format = AudioFormat(
        content_type=ContentType.PCM_S16LE,
        codec_type=ContentType.PCM_S16LE,
        sample_rate=44100,
        bit_depth=16,
        channels=2,
    )
    streamdetails = _make_streamdetails(
        audio_format=source_format, decoded_audio_format=decoded_format
    )

    audio = _make_audio_controller()
    await _drain(audio.get_media_stream(streamdetails, _make_pcm_format()))

    assert streamdetails.audio_format.codec_type is ContentType.VORBIS


@pytest.mark.asyncio
@pytest.mark.usefixtures("patch_ffmpeg")
async def test_get_media_stream_writes_back_codec_when_no_decoded_format() -> None:
    """Without decoded_audio_format, ffmpeg's probed codec_type is written back."""
    source_format = AudioFormat(
        content_type=ContentType.FLAC,
        # Start with UNKNOWN so we can see the post-probe writeback take effect.
        codec_type=ContentType.UNKNOWN,
        sample_rate=44100,
        bit_depth=16,
        channels=2,
    )
    streamdetails = _make_streamdetails(audio_format=source_format)

    audio = _make_audio_controller()
    await _drain(audio.get_media_stream(streamdetails, _make_pcm_format()))

    # _FakeFFMpeg's start() mutates input_format.codec_type to FLAC; with no
    # decoded format that AudioFormat is the same object as streamdetails.audio_format,
    # so the controller's writeback path is exercised end-to-end.
    assert streamdetails.audio_format.codec_type is ContentType.FLAC


@pytest.mark.asyncio
async def test_get_media_stream_keeps_caller_extra_input_args_intact(
    patch_ffmpeg: type[_FakeFFMpeg],
) -> None:
    """Per-call input args must not leak back onto the caller's StreamDetails."""
    streamdetails = _seekable_streamdetails()
    audio = _make_audio_controller()

    await _drain(audio.get_media_stream(streamdetails, _make_pcm_format(), seek_position=30))

    assert patch_ffmpeg.last_instance is not None
    assert patch_ffmpeg.last_instance.extra_input_args == [*_PROVIDER_INPUT_ARGS, "-ss", "30"]
    assert streamdetails.extra_input_args == [*_PROVIDER_INPUT_ARGS]

    # StreamDetails are cached on the queue item and reach this method again on a
    # retry, another seek or from the background analyzer: every call must build its
    # args from the provider's list alone instead of stacking onto the previous call's.
    await _drain(audio.get_media_stream(streamdetails, _make_pcm_format(), seek_position=600))

    assert patch_ffmpeg.last_instance.extra_input_args == [*_PROVIDER_INPUT_ARGS, "-ss", "600"]
    assert streamdetails.extra_input_args == [*_PROVIDER_INPUT_ARGS]
