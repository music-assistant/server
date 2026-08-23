"""Test that audio is only ever passed through unconverted when the bytes really match."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import ContentType
from music_assistant_models.media_items import AudioFormat

from music_assistant.controllers.streams import audio_buffer as audio_buffer_module
from music_assistant.controllers.streams.audio_buffer import AudioBuffer
from music_assistant.helpers.audio import pcm_formats_match
from music_assistant.providers.airplay.stream_session import AirPlayStreamSession

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    import pytest

_S32 = AudioFormat(content_type=ContentType.PCM_S32LE, sample_rate=44100, bit_depth=32, channels=2)
_F32 = AudioFormat(content_type=ContentType.PCM_F32LE, sample_rate=44100, bit_depth=32, channels=2)


def test_integer_and_float_pcm_are_not_interchangeable() -> None:
    """
    Equality cannot be used to decide a passthrough.

    Every PCM content type renders the same ``output_format_str``, so the model's
    own ``==`` reports integer and float PCM of one depth as the same format;
    passing one through as the other reinterprets every sample.
    """
    assert _S32 == _F32  # the trap this predicate exists to avoid
    assert not pcm_formats_match(_S32, _F32)
    assert pcm_formats_match(_S32, _S32)


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
