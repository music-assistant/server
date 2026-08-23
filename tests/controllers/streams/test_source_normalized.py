"""
Tests for skipping volume normalization when the source already did it.

A provider that hands over audio at a loudness target of its own declares that
with ``MusicProvider.delivers_normalized_audio``; correcting such a level again
would mean normalizing twice, the second time against a measurement of the
source's own output. Also verified: the loudness analyzer declines to measure a
stream whose normalization is disabled, so no measurement is stored for it.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from music_assistant_models.enums import ContentType, MediaType, VolumeNormalizationMode
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.helpers.audio import get_normalization_mode

PCM_FORMAT = AudioFormat(
    content_type=ContentType.PCM_S16LE,
    codec_type=ContentType.PCM_S16LE,
    sample_rate=44100,
    bit_depth=16,
    channels=2,
)


def test_a_normalized_source_is_left_alone() -> None:
    """A source that normalizes its own output takes MA out of the loudness path."""
    streamdetails = _streamdetails()
    assert (
        get_normalization_mode(
            VolumeNormalizationMode.FALLBACK_DYNAMIC, True, streamdetails, source_normalized=True
        )
        == VolumeNormalizationMode.DISABLED
    )


def test_an_unnormalized_source_still_falls_back_to_dynamic() -> None:
    """Without a measurement or a normalizing source, the dynamic fallback still applies."""
    streamdetails = _streamdetails()
    assert (
        get_normalization_mode(
            VolumeNormalizationMode.FALLBACK_DYNAMIC, True, streamdetails, source_normalized=False
        )
        == VolumeNormalizationMode.DYNAMIC
    )


def test_a_stored_measurement_does_not_override_a_normalized_source() -> None:
    """A measurement left over from before must not pull MA back into correcting."""
    streamdetails = _streamdetails()
    # e.g. measured while the source was not normalizing, or on the other backend
    streamdetails.loudness = -7.2
    assert (
        get_normalization_mode(
            VolumeNormalizationMode.FALLBACK_DYNAMIC, True, streamdetails, source_normalized=True
        )
        == VolumeNormalizationMode.DISABLED
    )
    assert (
        get_normalization_mode(
            VolumeNormalizationMode.FALLBACK_DYNAMIC, True, streamdetails, source_normalized=False
        )
        == VolumeNormalizationMode.MEASUREMENT_ONLY
    )


def test_the_queue_setting_still_wins() -> None:
    """Normalization disabled for the queue stays disabled either way."""
    streamdetails = _streamdetails()
    assert (
        get_normalization_mode(
            VolumeNormalizationMode.FALLBACK_DYNAMIC, False, streamdetails, source_normalized=True
        )
        == VolumeNormalizationMode.DISABLED
    )


def test_a_music_provider_declares_nothing_by_default() -> None:
    """The declaration is opt-in: nothing downstream verifies it."""
    from music_assistant.models.music_provider import MusicProvider  # noqa: PLC0415

    provider = object.__new__(MusicProvider)
    assert provider.delivers_normalized_audio is False


async def test_the_analyzer_declines_a_stream_it_must_not_measure() -> None:
    """
    A disabled stream is not measured, so a normalized source stores no loudness.

    That is what keeps a value measured on one backend's output from being applied
    to the other's, without any erase step.
    """
    from music_assistant.providers.loudness_analysis.provider import (  # noqa: PLC0415
        LoudnessAnalysisProvider,
    )

    provider = object.__new__(LoudnessAnalysisProvider)
    provider.logger = MagicMock()
    streamdetails = _streamdetails()
    streamdetails.volume_normalization_mode = VolumeNormalizationMode.DISABLED
    assert await provider._start_analysis("session-1", streamdetails, PCM_FORMAT) is False


def _streamdetails() -> StreamDetails:
    """Return StreamDetails for an ordinary track with a normalization target set."""
    streamdetails = StreamDetails(
        provider="test--1",
        item_id="1",
        audio_format=PCM_FORMAT,
        media_type=MediaType.TRACK,
    )
    streamdetails.target_loudness = -14.0
    return streamdetails
