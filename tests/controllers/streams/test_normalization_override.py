"""Tests for the crossfade volume-normalization pinning (get_queue_item_stream override).

In per-item crossfade mode the next track is streamed twice: once as the crossfade fade-in
(at prep time) and once as its own body. If the track's loudness measurement lands between
those two calls, the body would flip from DYNAMIC (loudnorm) to MEASUREMENT_ONLY while the
already-baked crossfade intro stays on DYNAMIC, producing an audible volume jump at the seam.

``get_queue_item_stream`` accepts a ``normalization_override`` so the crossfade path can pin
the body to the same mode the intro was baked with. These tests verify that override behaviour.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ContentType, MediaType, VolumeNormalizationMode
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

import music_assistant.controllers.streams.audio as audio_mod
from music_assistant.controllers.streams.audio import CrossfadeData, StreamsAudio

PCM_FORMAT = AudioFormat(
    content_type=ContentType.PCM_S16LE,
    codec_type=ContentType.PCM_S16LE,
    sample_rate=44100,
    bit_depth=16,
    channels=2,
)

# integrated loudness as if a fresh measurement landed; with target -17 LUFS this yields a
# MEASUREMENT_ONLY gain of -17 - (-11.4) = -5.6 dB
MEASURED_LOUDNESS = -11.4
TARGET_LOUDNESS = -17.0


class _FakeBuffer:
    """AudioBuffer test double that records the filter_params it is asked to apply."""

    def __init__(self) -> None:
        self.has_error = False
        self.captured_filter_params: list[str] | None = None

    async def get_stream(
        self,
        output_format: AudioFormat,
        seek_position_ms: int = 0,
        filter_params: list[str] | None = None,
    ) -> AsyncGenerator[bytes]:
        self.captured_filter_params = filter_params
        # yield nothing: we only care about the filter chain that was built
        empty: tuple[bytes, ...] = ()
        for chunk in empty:
            yield chunk


class _FakeAudioBuffer:
    """Stand-in for the AudioBuffer class; hands back a recording buffer instance."""

    instance: _FakeBuffer | None = None

    @staticmethod
    async def get_buffer(**_kwargs: Any) -> _FakeBuffer:
        buf = _FakeBuffer()
        _FakeAudioBuffer.instance = buf
        return buf


@pytest.fixture
def audio(monkeypatch: pytest.MonkeyPatch) -> StreamsAudio:
    """Build a StreamsAudio with the buffer/analysis/config dependencies mocked out."""
    _FakeAudioBuffer.instance = None
    monkeypatch.setattr(audio_mod, "AudioBuffer", _FakeAudioBuffer)
    # when the override path is NOT taken, the mode is re-evaluated to MEASUREMENT_ONLY
    monkeypatch.setattr(
        audio_mod,
        "get_normalization_mode",
        lambda *_a, **_k: VolumeNormalizationMode.MEASUREMENT_ONLY,
    )

    controller = StreamsAudio(MagicMock())
    mass = cast("MagicMock", controller.mass)
    mass.create_task = MagicMock()
    mass.streams.audio_analysis.get_audio_analysis = AsyncMock(
        return_value=SimpleNamespace(loudness_integrated=MEASURED_LOUDNESS, loudness_album=None)
    )
    mass.config.get_core_config = AsyncMock(return_value=MagicMock())
    mass.config.get_player_config = AsyncMock(return_value=MagicMock())
    return controller


def _make_queue_item() -> MagicMock:
    streamdetails = StreamDetails(
        provider="test_provider",
        item_id="track_b",
        audio_format=PCM_FORMAT,
        media_type=MediaType.TRACK,
    )
    streamdetails.queue_id = "queue_1"
    streamdetails.target_loudness = TARGET_LOUDNESS
    streamdetails.loudness = None
    queue_item = MagicMock()
    queue_item.media_type = MediaType.TRACK
    queue_item.streamdetails = streamdetails
    queue_item.name = "Track B"
    return queue_item


async def _drain(gen: AsyncGenerator[bytes]) -> None:
    async for _ in gen:
        pass


@pytest.mark.asyncio
async def test_dynamic_override_forces_loudnorm_and_ignores_measurement(
    audio: StreamsAudio,
) -> None:
    """A DYNAMIC override keeps the body on loudnorm even though a measurement is available."""
    queue_item = _make_queue_item()

    await _drain(
        audio.get_queue_item_stream(
            queue_item,
            PCM_FORMAT,
            normalization_override=VolumeNormalizationMode.DYNAMIC,
        )
    )

    assert _FakeAudioBuffer.instance is not None
    filter_params = _FakeAudioBuffer.instance.captured_filter_params or []
    # DYNAMIC -> loudnorm filter, NOT a static measurement gain
    assert any(p.startswith("loudnorm") for p in filter_params)
    assert not any(p.startswith("volume=") for p in filter_params)
    # the pinned mode is applied verbatim
    assert queue_item.streamdetails.volume_normalization_mode == VolumeNormalizationMode.DYNAMIC
    # and the just-in-time hydration / re-evaluation is skipped entirely
    mass = cast("MagicMock", audio.mass)
    mass.streams.audio_analysis.get_audio_analysis.assert_not_called()
    mass.config.get_core_config.assert_not_called()


@pytest.mark.asyncio
async def test_no_override_reevaluates_to_measurement_only(audio: StreamsAudio) -> None:
    """Without an override, a landed measurement re-evaluates the body to MEASUREMENT_ONLY."""
    queue_item = _make_queue_item()

    await _drain(audio.get_queue_item_stream(queue_item, PCM_FORMAT))

    assert _FakeAudioBuffer.instance is not None
    filter_params = _FakeAudioBuffer.instance.captured_filter_params or []
    # MEASUREMENT_ONLY -> static volume gain of target - loudness = -17 - (-11.4) = -5.6 dB
    assert "volume=-5.6dB" in filter_params
    assert not any(p.startswith("loudnorm") for p in filter_params)
    assert (
        queue_item.streamdetails.volume_normalization_mode
        == VolumeNormalizationMode.MEASUREMENT_ONLY
    )
    # the measurement was hydrated from analysis
    cast("MagicMock", audio.mass).streams.audio_analysis.get_audio_analysis.assert_called_once()


def test_crossfade_data_defaults_normalization_mode_to_none() -> None:
    """CrossfadeData carries an optional normalization_mode, defaulting to None."""
    data = CrossfadeData(
        data=b"",
        fade_in_size=0,
        pcm_format=PCM_FORMAT,
        fade_in_pcm_format=PCM_FORMAT,
        queue_item_id="qi",
    )
    assert data.normalization_mode is None
    pinned = CrossfadeData(
        data=b"",
        fade_in_size=0,
        pcm_format=PCM_FORMAT,
        fade_in_pcm_format=PCM_FORMAT,
        queue_item_id="qi",
        normalization_mode=VolumeNormalizationMode.DYNAMIC,
    )
    assert pinned.normalization_mode == VolumeNormalizationMode.DYNAMIC
