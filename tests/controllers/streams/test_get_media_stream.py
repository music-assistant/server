"""Tests for the ffmpeg input arguments StreamsAudio.get_media_stream builds."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import AudioError
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import MultiPartPath, StreamDetails

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


@pytest.fixture
def patch_two_minute_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> type[_TwoMinuteFFMpeg]:
    """Swap the real FFMpeg for the fake that emits a fixed amount of audio."""
    _TwoMinuteFFMpeg.last_instance = None
    monkeypatch.setattr(audio_mod, "FFMpeg", _TwoMinuteFFMpeg)
    return _TwoMinuteFFMpeg


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


_PCM_SAMPLE_SIZE = _make_pcm_format().pcm_sample_size


def _make_streamdetails(
    *,
    audio_format: AudioFormat,
    decoded_audio_format: AudioFormat | None = None,
    extra_input_args: list[str] | None = None,
) -> StreamDetails:
    return StreamDetails(
        provider="test_provider",
        item_id="main",
        audio_format=audio_format,
        decoded_audio_format=decoded_audio_format,
        media_type=MediaType.AUDIO_SOURCE,
        stream_type=StreamType.NAMED_PIPE,
        path="/tmp/fake-fifo",  # noqa: S108
        extra_input_args=extra_input_args or [],
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


def _recording_multi_file_stream() -> tuple[Any, list[int]]:
    """
    Build a stand-in for the concat stream plus the list of seek positions it received.

    Avoids a real ffmpeg process and temp file while still proving the seek was
    handed off to the source rather than applied through the -ss argument.
    """
    received_seeks: list[int] = []

    async def _empty_stream() -> AsyncGenerator[bytes]:
        yield b""

    # record on call rather than on first iteration: the FFMpeg double never
    # consumes the generator it is handed, so its body would never run
    def _fake_stream(
        _streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes]:
        received_seeks.append(seek_position)
        return _empty_stream()

    return _fake_stream, received_seeks


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


class _TwoMinuteFFMpeg(_FakeFFMpeg):
    """FFMpeg double that emits exactly two minutes of PCM at the format below."""

    seconds_emitted = 120

    async def iter_chunked(self, _chunk_size: int) -> AsyncGenerator[bytes]:
        for _ in range(self.seconds_emitted):
            yield b"\x00" * _PCM_SAMPLE_SIZE


def _multi_part_streamdetails() -> StreamDetails:
    """Build StreamDetails for a multi-file audiobook of two 30 minute parts."""
    return StreamDetails(
        provider="test_provider",
        item_id="audiobook-1",
        audio_format=AudioFormat(content_type=ContentType.MP3),
        media_type=MediaType.AUDIOBOOK,
        stream_type=StreamType.HTTP,
        path=[
            MultiPartPath(path="http://test.invalid/part-1.mp3", duration=1800),
            MultiPartPath(path="http://test.invalid/part-2.mp3", duration=1800),
        ],
        duration=3600,
        can_seek=True,
        allow_seek=True,
    )


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
async def test_get_media_stream_stores_measured_duration_for_full_playthrough(
    monkeypatch: pytest.MonkeyPatch,
    patch_two_minute_ffmpeg: type[_TwoMinuteFFMpeg],
) -> None:
    """A multi-file item streamed from the start gets its measured duration stored."""
    streamdetails = _multi_part_streamdetails()
    audio = _make_audio_controller()
    fake_stream, _ = _recording_multi_file_stream()
    monkeypatch.setattr(audio, "get_multi_file_stream", fake_stream)

    await _drain(audio.get_media_stream(streamdetails, _make_pcm_format()))

    assert streamdetails.duration == patch_two_minute_ffmpeg.seconds_emitted


@pytest.mark.asyncio
async def test_get_media_stream_keeps_duration_when_multi_file_seek_is_delegated(
    monkeypatch: pytest.MonkeyPatch,
    patch_two_minute_ffmpeg: type[_TwoMinuteFFMpeg],
) -> None:
    """Resuming a multi-file audiobook must not shrink its duration to the remainder."""
    streamdetails = _multi_part_streamdetails()
    audio = _make_audio_controller()
    fake_stream, received_seeks = _recording_multi_file_stream()
    monkeypatch.setattr(audio, "get_multi_file_stream", fake_stream)

    await _drain(audio.get_media_stream(streamdetails, _make_pcm_format(), seek_position=1800))

    # the concat stream consumes the seek itself, which clears the local seek
    # position before the duration writeback runs at the end of the stream
    assert received_seeks == [1800]
    assert patch_two_minute_ffmpeg.last_instance is not None
    assert "-ss" not in (patch_two_minute_ffmpeg.last_instance.extra_input_args or [])
    assert streamdetails.duration == 3600


@pytest.mark.asyncio
async def test_get_media_stream_keeps_duration_when_provider_seek_is_delegated(
    patch_two_minute_ffmpeg: type[_TwoMinuteFFMpeg],
) -> None:
    """A seekable provider stream must not shrink its duration to the remainder either."""
    streamdetails = StreamDetails(
        provider="test_provider",
        item_id="track-1",
        audio_format=AudioFormat(content_type=ContentType.OGG),
        media_type=MediaType.TRACK,
        stream_type=StreamType.CUSTOM,
        duration=240,
        can_seek=True,
        allow_seek=True,
    )
    audio = _make_audio_controller()
    provider = cast("MagicMock", audio.mass).get_provider.return_value

    await _drain(audio.get_media_stream(streamdetails, _make_pcm_format(), seek_position=90))

    # a provider that can seek receives the position and the local one is cleared,
    # so only the remaining audio reaches ffmpeg
    provider.get_audio_stream.assert_called_once_with(streamdetails, seek_position=90)
    assert patch_two_minute_ffmpeg.last_instance is not None
    assert "-ss" not in (patch_two_minute_ffmpeg.last_instance.extra_input_args or [])
    assert streamdetails.duration == 240


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


@pytest.mark.asyncio
async def test_get_media_stream_adds_realtime_pacing_for_audio_source(
    patch_ffmpeg: type[_FakeFFMpeg],
) -> None:
    """A live AudioSource gets realtime pacing with a small initial burst of headroom."""
    audio = _make_audio_controller()
    await _drain(audio.get_media_stream(_flac_streamdetails(), _make_pcm_format()))

    assert patch_ffmpeg.last_instance is not None
    assert patch_ffmpeg.last_instance.extra_input_args == [
        "-readrate",
        "1",
        "-readrate_initial_burst",
        "0.5",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_pacing_args",
    [["-readrate", "1.0", "-readrate_initial_burst", "2"], ["-re"]],
    ids=["readrate", "re"],
)
async def test_get_media_stream_respects_provider_pacing_args(
    patch_ffmpeg: type[_FakeFFMpeg],
    provider_pacing_args: list[str],
) -> None:
    """Provider-supplied -re/-readrate args suppress the automatic AudioSource pacing."""
    streamdetails = _make_streamdetails(
        audio_format=AudioFormat(
            content_type=ContentType.FLAC,
            codec_type=ContentType.FLAC,
            sample_rate=44100,
            bit_depth=16,
            channels=2,
        ),
        extra_input_args=list(provider_pacing_args),
    )
    audio = _make_audio_controller()
    await _drain(audio.get_media_stream(streamdetails, _make_pcm_format()))

    assert patch_ffmpeg.last_instance is not None
    assert patch_ffmpeg.last_instance.extra_input_args == provider_pacing_args
