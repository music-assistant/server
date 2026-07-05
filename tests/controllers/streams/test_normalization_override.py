"""
Tests for the crossfade volume-normalization pinning (get_queue_item_stream override).

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
from music_assistant.controllers.streams.audio import StreamsAudio

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


@pytest.fixture
def audio(monkeypatch: pytest.MonkeyPatch) -> tuple[StreamsAudio, dict[str, list[str]]]:
    """
    Build a StreamsAudio with buffer/analysis/config mocked out.

    Returns the controller plus a dict whose ``filter_params`` key captures the ffmpeg
    filter chain the (faked) audio buffer was asked to apply.
    """
    captured: dict[str, list[str]] = {}

    class _FakeBuffer:
        """AudioBuffer test double: records the filter_params, yields no audio."""

        has_error = False

        @classmethod
        async def get_buffer(cls, **_kwargs: Any) -> _FakeBuffer:
            return cls()

        async def get_stream(
            self,
            output_format: AudioFormat,
            seek_position_ms: int = 0,
            filter_params: list[str] | None = None,
        ) -> AsyncGenerator[bytes]:
            captured["filter_params"] = filter_params or []
            empty: tuple[bytes, ...] = ()
            for chunk in empty:
                yield chunk

    monkeypatch.setattr(audio_mod, "AudioBuffer", _FakeBuffer)
    # when the override path is NOT taken, the mode is re-evaluated to MEASUREMENT_ONLY
    monkeypatch.setattr(
        audio_mod,
        "get_normalization_mode",
        lambda *_a, **_k: VolumeNormalizationMode.MEASUREMENT_ONLY,
    )

    controller = StreamsAudio(MagicMock())
    mass = cast("MagicMock", controller.mass)
    mass.streams.audio_analysis.get_audio_analysis = AsyncMock(
        return_value=SimpleNamespace(loudness_integrated=MEASURED_LOUDNESS, loudness_album=None)
    )
    mass.streams.config.get_value.return_value = VolumeNormalizationMode.FALLBACK_DYNAMIC.value
    mass.config.get_player_config = AsyncMock(return_value=MagicMock())
    return controller, captured


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
    audio: tuple[StreamsAudio, dict[str, list[str]]],
) -> None:
    """A DYNAMIC override keeps the body on loudnorm even though a measurement is available."""
    controller, captured = audio
    queue_item = _make_queue_item()

    await _drain(
        controller.get_queue_item_stream(
            queue_item,
            PCM_FORMAT,
            normalization_override=VolumeNormalizationMode.DYNAMIC,
        )
    )

    # DYNAMIC -> loudnorm filter, NOT a static measurement gain
    assert any(p.startswith("loudnorm") for p in captured["filter_params"])
    assert queue_item.streamdetails.volume_normalization_mode == VolumeNormalizationMode.DYNAMIC
    # the just-in-time hydration / re-evaluation is skipped entirely
    mass = cast("MagicMock", controller.mass)
    mass.streams.audio_analysis.get_audio_analysis.assert_not_called()
    mass.streams.config.get_value.assert_not_called()


@pytest.mark.asyncio
async def test_no_override_reevaluates_to_measurement_only(
    audio: tuple[StreamsAudio, dict[str, list[str]]],
) -> None:
    """Without an override, a landed measurement re-evaluates the body to MEASUREMENT_ONLY."""
    controller, captured = audio
    queue_item = _make_queue_item()

    await _drain(controller.get_queue_item_stream(queue_item, PCM_FORMAT))

    # MEASUREMENT_ONLY -> static volume gain of target - loudness = -17 - (-11.4) = -5.6 dB
    assert "volume=-5.6dB" in captured["filter_params"]
    assert (
        queue_item.streamdetails.volume_normalization_mode
        == VolumeNormalizationMode.MEASUREMENT_ONLY
    )
    # the measurement was hydrated from analysis
    cast(
        "MagicMock", controller.mass
    ).streams.audio_analysis.get_audio_analysis.assert_called_once()
