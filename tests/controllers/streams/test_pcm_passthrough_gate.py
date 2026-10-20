"""Test that audio is only ever passed through unconverted when the bytes really match."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.streams import audio_buffer as audio_buffer_module
from music_assistant.controllers.streams.audio_buffer import AudioBuffer
from music_assistant.providers.airplay.stream_session import AirPlayStreamSession

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    import pytest

    from music_assistant.controllers.streams.audio import StreamsAudio

_S32 = AudioFormat(content_type=ContentType.PCM_S32LE, sample_rate=44100, bit_depth=32, channels=2)
_F32 = AudioFormat(content_type=ContentType.PCM_F32LE, sample_rate=44100, bit_depth=32, channels=2)


def test_integer_and_float_pcm_are_not_interchangeable() -> None:
    """
    The gates below rely on the model telling PCM encodings apart.

    Integer and float PCM of one depth share their rate, depth and channel count;
    passing one through as the other reinterprets every sample.
    """
    assert _S32 != _F32
    same_as_s32 = AudioFormat(
        content_type=ContentType.PCM_S32LE, sample_rate=44100, bit_depth=32, channels=2
    )
    assert same_as_s32 == _S32


async def test_the_buffer_converts_rather_than_reinterprets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A buffer of integer PCM asked for float PCM has to run its conversion."""
    buffer = AudioBuffer(_S32)
    took: list[str] = []

    async def _fake_ffmpeg_stream(**_kwargs: object) -> AsyncGenerator[bytes]:
        took.append("ffmpeg")
        yield b""

    async def _fake_raw_stream(**_kwargs: object) -> AsyncGenerator[bytes]:
        took.append("raw")
        yield b""

    monkeypatch.setattr(audio_buffer_module, "get_ffmpeg_stream", _fake_ffmpeg_stream)
    monkeypatch.setattr(buffer, "get_raw_stream", _fake_raw_stream)

    async for _ in buffer.get_stream(output_format=_F32):
        pass
    assert took == ["ffmpeg"]

    took.clear()
    async for _ in buffer.get_stream(output_format=_S32):
        pass
    assert took == ["raw"]


def test_a_warm_airplay_replace_refuses_a_differently_encoded_source() -> None:
    """A live session must not absorb a source it would then mislabel."""
    session = object.__new__(AirPlayStreamSession)
    session.pcm_format = _F32
    session.sync_clients = []
    assert session.can_replace([], _S32) is False
    assert session.can_replace([], _F32) is True


async def _which_path(advertised: AudioFormat, arriving: AudioFormat | None) -> str:
    """Return which branch of the AudioSource gate a live source is routed through."""
    from music_assistant.controllers.streams.audio import StreamsAudio  # noqa: PLC0415

    took: list[str] = []

    async def _bytes() -> AsyncGenerator[bytes]:
        yield b"\x00" * 8

    def _fake_open(_streamdetails: object) -> AsyncGenerator[bytes]:
        took.append("raw")
        return _bytes()

    async def _fake_ffmpeg(**_kwargs: object) -> AsyncGenerator[bytes]:
        took.append("ffmpeg")
        yield b"\x00" * 8

    controller = cast(
        "StreamsAudio",
        SimpleNamespace(_open_audio_source_generator=_fake_open, get_media_stream=_fake_ffmpeg),
    )
    streamdetails = StreamDetails(
        provider="test--1",
        item_id="1",
        audio_format=advertised,
        decoded_audio_format=arriving,
        media_type=MediaType.AUDIO_SOURCE,
        stream_type=StreamType.NAMED_PIPE,
        path="/fake/fifo",
    )
    async for _ in StreamsAudio._iter_audio_source_pcm(controller, streamdetails, _S32):
        pass
    return took[0]


async def test_a_codec_advertising_live_source_keeps_ffmpeg_in_the_path() -> None:
    """
    The gate reads the advertised format on purpose, not the arriving one.

    A live source that advertises a codec relies on ffmpeg to notice its
    producer going away - shairport-sync leaves the pipe behind on an unclean
    disconnect, which a direct read would simply reopen and wait on.
    """
    advertised = AudioFormat(
        content_type=ContentType.OGG,
        codec_type=ContentType.VORBIS,
        sample_rate=44100,
        bit_depth=16,
        channels=2,
        bit_rate=160,
    )
    assert await _which_path(advertised, _S32) == "ffmpeg"


async def test_a_pcm_advertising_live_source_is_read_directly() -> None:
    """A source that states the PCM it delivers is handed through unconverted."""
    assert await _which_path(_S32, None) == "raw"
