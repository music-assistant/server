"""
Tests for skipping volume normalization when the source already did it.

A provider that hands over audio at a loudness target of its own declares that
with ``MusicProvider.delivers_normalized_audio``; correcting such a level again
would mean normalizing twice, the second time against a measurement of the
source's own output. That outcome is reported as ``SOURCE`` rather than
``DISABLED`` - the audio is levelled, just not by us - so the gates that mean
"Music Assistant applies nothing" have to accept both. Also verified: the
loudness analyzer declines such a stream, so no measurement is stored for it.
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from music_assistant_models.audio_processing import AudioNormalizationMeasurementSource
from music_assistant_models.enums import ContentType, MediaType, VolumeNormalizationMode
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.streams.audio import StreamsAudio
from music_assistant.controllers.streams.audio_processing import get_normalization_details
from music_assistant.controllers.streams.controller import (
    StreamsController,
    _volume_normalization_preference_options,
)
from music_assistant.helpers.audio import get_normalization_mode
from music_assistant.models.music_provider import MusicProvider


class _NormalizingProvider(MusicProvider):
    """A music provider that hands over audio it already levelled."""

    @property
    def delivers_normalized_audio(self) -> bool:
        """Declare the source normalization."""
        return True


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
        == VolumeNormalizationMode.SOURCE
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
        == VolumeNormalizationMode.SOURCE
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
    assert object.__new__(MusicProvider).delivers_normalized_audio is False


@pytest.mark.parametrize("mode", [VolumeNormalizationMode.DISABLED, VolumeNormalizationMode.SOURCE])
async def test_the_analyzer_declines_a_stream_it_must_not_measure(
    mode: VolumeNormalizationMode,
) -> None:
    """
    A stream we do not normalize is not measured either, so no loudness is stored.

    That is what keeps a value measured on one backend's output from being applied
    to the other's, without any erase step. SOURCE has to decline for a second
    reason: measuring there would record the source's own level as the track's.
    """
    from music_assistant.providers.loudness_analysis.provider import (  # noqa: PLC0415
        LoudnessAnalysisProvider,
    )

    provider = object.__new__(LoudnessAnalysisProvider)
    provider.logger = MagicMock()
    streamdetails = _streamdetails()
    streamdetails.volume_normalization_mode = mode
    assert await provider._start_analysis("session-1", streamdetails, PCM_FORMAT) is False


@pytest.mark.parametrize("mode", [VolumeNormalizationMode.DISABLED, VolumeNormalizationMode.SOURCE])
def test_no_headroom_is_reserved_for_normalization_we_do_not_apply(
    mode: VolumeNormalizationMode,
) -> None:
    """
    F32 headroom is only paid for when Music Assistant itself touches the level.

    A source-normalized stream keeps its native depth, exactly as a disabled one does.
    """
    audio = object.__new__(StreamsAudio)
    streamdetails = _streamdetails()
    streamdetails.volume_normalization_mode = mode

    content_type, bit_depth = audio._pick_pcm_bit_depth(
        [],
        streamdetails,
        crossfade_enabled=False,
        overlay_active=False,
    )

    assert bit_depth == 16
    assert content_type == ContentType.PCM_S16LE


def test_source_normalized_audio_reports_a_mode_without_a_measurement() -> None:
    """
    A source-normalized stream reports who levelled it and nothing more.

    The target and the measurement are ours, and neither describes what the source
    did - a stale library measurement in particular must not be presented as the
    level this audio was corrected against.
    """
    streamdetails = _streamdetails()
    streamdetails.volume_normalization_mode = VolumeNormalizationMode.SOURCE
    streamdetails.loudness = -7.2

    details = get_normalization_details(streamdetails, None)

    assert details is not None
    assert details.mode == VolumeNormalizationMode.SOURCE
    assert details.measurement_source == AudioNormalizationMeasurementSource.UNKNOWN
    assert details.target_lufs is None
    assert details.measured_lufs is None
    assert details.applied_gain_db is None


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        (object.__new__(_NormalizingProvider), True),
        (object.__new__(MusicProvider), False),
        # a plugin provider, which never declares it
        (MagicMock(), False),
    ],
)
def test_only_a_music_provider_can_claim_it_levelled_the_audio(
    provider: object, expected: bool
) -> None:
    """
    A plugin provider serves playable items too, but never answers this.

    Its live audio is taken out of the loudness path by its media type instead.
    """
    controller = cast("Any", object.__new__(StreamsController))
    controller.mass = MagicMock()
    controller.mass.get_provider.return_value = provider

    assert controller.source_normalizes_audio(_streamdetails()) is expected


def test_an_outcome_only_mode_is_not_offered_as_a_preference() -> None:
    """
    The setting says what Music Assistant should do, so outcomes are not choices.

    SOURCE is set by a source that levels its own audio and UNKNOWN is what an
    unrecognised value deserializes to; neither is something to ask a user for.
    """
    offered = {option.value for option in _volume_normalization_preference_options()}

    assert VolumeNormalizationMode.SOURCE.value not in offered
    assert VolumeNormalizationMode.UNKNOWN.value not in offered
    assert offered == {
        mode.value
        for mode in VolumeNormalizationMode
        if mode not in (VolumeNormalizationMode.SOURCE, VolumeNormalizationMode.UNKNOWN)
    }


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
