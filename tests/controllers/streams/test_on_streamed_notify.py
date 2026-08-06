"""
Tests for the on_streamed notification at the end of get_queue_item_stream.

``on_streamed`` only exists on MusicProvider, but a PluginProvider can own a playable
item too (AI Radio serves its spoken clips as SOUND_EFFECT items). Notifying such a
provider raised an AttributeError inside a ``finally`` block, which escaped the
generator and killed the whole flow stream mid-playback.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import (
    ContentType,
    MediaType,
    ProviderType,
    VolumeNormalizationMode,
)
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

import music_assistant.controllers.streams.audio as audio_mod
from music_assistant.controllers.streams.audio import StreamsAudio

PCM_FORMAT = AudioFormat(
    content_type=ContentType.PCM_S16LE,
    codec_type=ContentType.PCM_S16LE,
    sample_rate=44100,
    bit_depth=16,
    channels=2,
)


@pytest.fixture
def controller(monkeypatch: pytest.MonkeyPatch) -> StreamsAudio:
    """Build a StreamsAudio whose audio buffer yields no audio and completes right away."""

    class _FakeBuffer:
        has_error = False
        pcm_format = PCM_FORMAT

        @classmethod
        async def get_buffer(cls, **_kwargs: Any) -> _FakeBuffer:
            return cls()

        async def get_stream(
            self,
            output_format: AudioFormat,
            seek_position_ms: int = 0,
            filter_params: list[str] | None = None,
        ) -> AsyncGenerator[bytes]:
            empty: tuple[bytes, ...] = ()
            for chunk in empty:
                yield chunk

    monkeypatch.setattr(audio_mod, "AudioBuffer", _FakeBuffer)
    audio = StreamsAudio(MagicMock())
    mass = cast("MagicMock", audio.mass)
    mass.streams.audio_analysis.get_audio_analysis = AsyncMock(return_value=None)
    mass.streams.config.get_value.return_value = VolumeNormalizationMode.DISABLED.value
    mass.config.get_player_config = AsyncMock(return_value=MagicMock())
    return audio


def _make_queue_item(media_type: MediaType) -> MagicMock:
    streamdetails = StreamDetails(
        provider="test_provider",
        item_id="item_1",
        audio_format=PCM_FORMAT,
        media_type=media_type,
    )
    queue_item = MagicMock()
    queue_item.media_type = media_type
    queue_item.streamdetails = streamdetails
    queue_item.name = "Item 1"
    return queue_item


async def _drain(gen: AsyncGenerator[bytes]) -> None:
    async for _ in gen:
        pass


@pytest.mark.asyncio
async def test_plugin_owned_item_does_not_break_the_stream(controller: StreamsAudio) -> None:
    """A plugin provider without on_streamed is skipped instead of crashing the stream."""
    mass = cast("MagicMock", controller.mass)
    # deliberately has no on_streamed, exactly like a PluginProvider
    mass.get_provider.return_value = SimpleNamespace(type=ProviderType.PLUGIN)

    await _drain(
        controller.get_queue_item_stream(_make_queue_item(MediaType.SOUND_EFFECT), PCM_FORMAT)
    )

    mass.create_task.assert_not_called()


@pytest.mark.asyncio
async def test_music_provider_is_still_notified(controller: StreamsAudio) -> None:
    """A music provider that fully streamed its item still gets on_streamed."""
    mass = cast("MagicMock", controller.mass)
    music_prov = MagicMock(type=ProviderType.MUSIC)
    mass.get_provider.return_value = music_prov

    queue_item = _make_queue_item(MediaType.TRACK)
    await _drain(controller.get_queue_item_stream(queue_item, PCM_FORMAT))

    mass.create_task.assert_called_once()
    music_prov.on_streamed.assert_called_once_with(queue_item.streamdetails)
