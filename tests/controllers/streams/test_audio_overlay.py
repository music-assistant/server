"""Tests for the audio overlay mixing in StreamsAudio."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import MediaType, StreamType
from music_assistant_models.media_items import AudioFormat, ItemMapping

from music_assistant.controllers.streams.audio import StreamsAudio, overlay_active

if TYPE_CHECKING:
    import pytest

_PCM_FORMAT = AudioFormat(sample_rate=44100, bit_depth=16, channels=2)
_MUSIC_CHUNKS = [b"chunk1", b"chunk2"]
_OVERLAY_URL = "http://ambient.example/rain.mp3"


def _make_streams_audio(provider: MagicMock | None = None) -> StreamsAudio:
    """Build a StreamsAudio whose mass resolves the overlay provider to the given mock."""
    mass = MagicMock()
    mass.get_provider = MagicMock(return_value=provider)
    return StreamsAudio(mass)


def _make_queue(
    *, enabled: bool = True, source: ItemMapping | None = None, volume: int = 100
) -> MagicMock:
    """Build a PlayerQueue double carrying just the overlay fields."""
    queue = MagicMock()
    queue.overlay_enabled = enabled
    queue.overlay_source = source
    queue.overlay_volume = volume
    return queue


def _make_source_mapping() -> ItemMapping:
    return ItemMapping(
        media_type=MediaType.SOUND_EFFECT,
        item_id="rain",
        provider="ambient",
        name="Rain",
    )


async def _music_stream() -> AsyncGenerator[bytes]:
    for chunk in _MUSIC_CHUNKS:
        yield chunk


# --- overlay_active ---


def test_overlay_active() -> None:
    """The overlay is only active when enabled AND a source is selected."""
    assert overlay_active(_make_queue(enabled=True, source=_make_source_mapping()))
    assert not overlay_active(_make_queue(enabled=True, source=None))
    assert not overlay_active(_make_queue(enabled=False, source=_make_source_mapping()))


# --- get_overlay_mixed_stream ---


async def test_passthrough_when_provider_unavailable() -> None:
    """An unavailable overlay provider degrades to unmodified music playback."""
    audio = _make_streams_audio(provider=None)
    queue = _make_queue(source=_make_source_mapping())
    result = [
        chunk async for chunk in audio.get_overlay_mixed_stream(queue, _music_stream(), _PCM_FORMAT)
    ]
    assert result == _MUSIC_CHUNKS


async def test_passthrough_when_streamdetails_fail() -> None:
    """An error resolving the overlay source degrades to unmodified music playback."""
    provider = MagicMock()
    provider.get_stream_details = AsyncMock(side_effect=RuntimeError("boom"))
    audio = _make_streams_audio(provider=provider)
    queue = _make_queue(source=_make_source_mapping())
    result = [
        chunk async for chunk in audio.get_overlay_mixed_stream(queue, _music_stream(), _PCM_FORMAT)
    ]
    assert result == _MUSIC_CHUNKS


async def test_passthrough_when_stream_type_unsupported() -> None:
    """An overlay source with a non file/url stream type degrades to unmodified playback."""
    provider = MagicMock()
    provider.get_stream_details = AsyncMock(
        return_value=MagicMock(stream_type=StreamType.CUSTOM, path=None)
    )
    audio = _make_streams_audio(provider=provider)
    queue = _make_queue(source=_make_source_mapping())
    result = [
        chunk async for chunk in audio.get_overlay_mixed_stream(queue, _music_stream(), _PCM_FORMAT)
    ]
    assert result == _MUSIC_CHUNKS


async def test_passthrough_when_local_file_missing() -> None:
    """An overlay source pointing at a non-existing file degrades to unmodified playback."""
    provider = MagicMock()
    provider.get_stream_details = AsyncMock(
        return_value=MagicMock(stream_type=StreamType.LOCAL_FILE, path="/does/not/exist.wav")
    )
    audio = _make_streams_audio(provider=provider)
    queue = _make_queue(source=_make_source_mapping())
    result = [
        chunk async for chunk in audio.get_overlay_mixed_stream(queue, _music_stream(), _PCM_FORMAT)
    ]
    assert result == _MUSIC_CHUNKS


async def test_mixes_when_source_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    """A resolvable overlay source is handed to the ffmpeg mixer with the queue's volume."""
    provider = MagicMock()
    provider.get_stream_details = AsyncMock(
        return_value=MagicMock(stream_type=StreamType.HTTP, path=_OVERLAY_URL)
    )
    audio = _make_streams_audio(provider=provider)
    queue = _make_queue(source=_make_source_mapping(), volume=55)

    mixer_kwargs: dict[str, Any] = {}

    async def _fake_mixer(**kwargs: Any) -> AsyncGenerator[bytes]:
        mixer_kwargs.update(kwargs)
        async for chunk in kwargs["audio_input"]:
            yield b"mixed:" + chunk

    monkeypatch.setattr(
        "music_assistant.controllers.streams.audio.get_ffmpeg_overlay_stream", _fake_mixer
    )
    result = [
        chunk async for chunk in audio.get_overlay_mixed_stream(queue, _music_stream(), _PCM_FORMAT)
    ]
    assert result == [b"mixed:" + chunk for chunk in _MUSIC_CHUNKS]
    assert mixer_kwargs["overlay_input"] == _OVERLAY_URL
    assert mixer_kwargs["overlay_volume"] == 55
    assert mixer_kwargs["pcm_format"] is _PCM_FORMAT
