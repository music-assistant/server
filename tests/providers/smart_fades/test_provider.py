"""Tests for the Smart Fades audio analysis provider."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import numpy as np
import pytest
from music_assistant_models.enums import ContentType, MediaType
from music_assistant_models.errors import SetupFailedError
from music_assistant_models.media_items import AudioFormat

from music_assistant.models.audio_analysis import AudioAnalysisData
from music_assistant.providers.smart_fades.provider import SmartFadesProvider

FIXTURE_DIR = Path(__file__).parent / "fixtures"
# Synthetic 120 BPM drum pattern (kick-hat-snare-hat): 44100 Hz, stereo, float32, ~15.7s
# 8 bars of 4 beats = 32 beats, downbeats on every 4th beat (kick).
# Clip ends 0.2s after the last beat to avoid end-of-track hallucination.
FIXTURE_PCM = FIXTURE_DIR / "test_120bpm_44100_2_32.pcm"

# Expected values from both beat_this File2Beats(checkpoint_path="final0", dbn=False)
# and the MA streaming pipeline (identical output, 0ms diff between the two).
# All 32 beats match ground truth within 20ms; all 8 downbeats match within 20ms.
# Zero false positives.
EXPECTED_BEATS = [
    0.000,
    0.500,
    1.000,
    1.500,
    2.020,
    2.500,
    3.000,
    3.500,
    4.020,
    4.500,
    5.020,
    5.500,
    6.020,
    6.500,
    7.000,
    7.520,
    8.020,
    8.500,
    9.000,
    9.520,
    10.020,
    10.500,
    11.000,
    11.500,
    12.020,
    12.500,
    13.020,
    13.520,
    14.020,
    14.500,
    15.020,
    15.520,
]

EXPECTED_DOWNBEATS = [
    0.000,
    2.020,
    4.020,
    6.020,
    8.020,
    10.020,
    12.020,
    14.020,
]


@pytest.fixture
def mass_mock() -> Mock:
    """Return a mock MusicAssistant instance."""
    mass = Mock()
    mass.cache = Mock()
    mass.cache.get = AsyncMock(return_value=None)
    mass.cache.set = AsyncMock()
    mass.music = Mock()
    mass.streams = Mock()
    mass.streams.audio_analysis = Mock()
    mass.streams.audio_analysis.get_audio_analysis_version = AsyncMock(return_value=None)
    mass.streams.audio_analysis.set_audio_analysis = AsyncMock()
    mass.config = Mock()
    mass.config.get = Mock(return_value={})
    return mass


@pytest.fixture
def manifest_mock() -> Mock:
    """Return a mock provider manifest."""
    manifest = Mock()
    manifest.domain = "smart_fades"
    manifest.name = "Smart Fades"
    return manifest


@pytest.fixture
def config_mock() -> Mock:
    """Return a mock provider config."""
    config = Mock()
    config.instance_id = "smart_fades_test"
    config.name = "Smart Fades Test"
    config.enabled = True
    config.get_value = Mock(return_value="GLOBAL")
    config.values = {}
    return config


@pytest.fixture
async def provider(mass_mock: Mock, manifest_mock: Mock, config_mock: Mock) -> SmartFadesProvider:
    """Return a SmartFadesProvider with mocked MA but real Beat This model."""
    prov = SmartFadesProvider(mass_mock, manifest_mock, config_mock, set())
    await prov.handle_async_init()
    return prov


async def test_beat_detection(provider: SmartFadesProvider, mass_mock: Mock) -> None:
    """Test that the provider detects correct beats and downbeats from a PCM fixture."""
    audio_format = AudioFormat(
        content_type=ContentType.PCM_F32LE,
        bit_depth=32,
        sample_rate=44100,
        channels=2,
    )

    stream_details = Mock()
    stream_details.item_id = "test_120bpm"
    stream_details.provider = "test"
    stream_details.queue_id = "test"
    stream_details.uri = "test://120bpm"
    stream_details.media_type = MediaType.TRACK
    stream_details.duration = 120

    session_id = "test:test:test_120bpm"
    await provider.start_analysis(session_id, stream_details, audio_format)

    # Feed PCM in 1-second chunks (matching the real streaming pipeline)
    pcm_data = FIXTURE_PCM.read_bytes()
    chunk_size = 44100 * 2 * 4  # 1 second at 44100 Hz, stereo, float32
    offset = 0
    while offset < len(pcm_data):
        chunk = pcm_data[offset : offset + chunk_size]
        await provider.process_pcm_chunk(session_id, chunk)
        offset += chunk_size

    await provider.finalize(session_id)

    # Verify set_audio_analysis was called with correct data
    set_aa_mock = mass_mock.streams.audio_analysis.set_audio_analysis
    set_aa_mock.assert_awaited_once()
    analysis = set_aa_mock.call_args.kwargs["analysis"]

    beats = analysis.beats
    downbeats = analysis.downbeats

    assert len(beats) == len(EXPECTED_BEATS), (
        f"Expected {len(EXPECTED_BEATS)} beats, got {len(beats)}"
    )
    assert len(downbeats) == len(EXPECTED_DOWNBEATS), (
        f"Expected {len(EXPECTED_DOWNBEATS)} downbeats, got {len(downbeats)}"
    )

    # All beats must be within 20ms (1 frame at 50fps) of expected values
    for i, (actual, expected) in enumerate(zip(beats, EXPECTED_BEATS, strict=True)):
        assert abs(float(actual) - expected) < 0.021, (
            f"Beat {i}: expected {expected:.3f}s, got {float(actual):.3f}s"
        )

    for i, (actual, expected) in enumerate(zip(downbeats, EXPECTED_DOWNBEATS, strict=True)):
        assert abs(float(actual) - expected) < 0.021, (
            f"Downbeat {i}: expected {expected:.3f}s, got {float(actual):.3f}s"
        )

    # Verify BPM is close to 120
    assert analysis.bpm is not None
    assert 115 < analysis.bpm < 125, f"Expected BPM ~120, got {analysis.bpm:.1f}"


async def test_extended_analysis_fields(provider: SmartFadesProvider, mass_mock: Mock) -> None:
    """Test that extended analysis fields (energy, centroid, key) are populated."""
    audio_format = AudioFormat(
        content_type=ContentType.PCM_F32LE,
        bit_depth=32,
        sample_rate=44100,
        channels=2,
    )

    stream_details = Mock()
    stream_details.item_id = "test_120bpm"
    stream_details.provider = "test"
    stream_details.queue_id = "test"
    stream_details.uri = "test://120bpm"
    stream_details.media_type = MediaType.TRACK
    stream_details.duration = 120

    session_id = "test:test:test_120bpm_extended"
    await provider.start_analysis(session_id, stream_details, audio_format)

    pcm_data = FIXTURE_PCM.read_bytes()
    chunk_size = 44100 * 2 * 4  # 1 second at 44100 Hz, stereo, float32
    offset = 0
    while offset < len(pcm_data):
        chunk = pcm_data[offset : offset + chunk_size]
        await provider.process_pcm_chunk(session_id, chunk)
        offset += chunk_size

    await provider.finalize(session_id)

    set_aa_mock = mass_mock.streams.audio_analysis.set_audio_analysis
    analysis = set_aa_mock.call_args.kwargs["analysis"]

    # Energy curve should be 1800 bins, normalized to [0, 1]
    assert analysis.rms_energy is not None
    assert len(analysis.rms_energy) == 1800
    assert analysis.rms_energy.max() <= 1.0
    assert analysis.rms_energy.min() >= 0.0

    # Spectral centroid should be 1800 bins with positive Hz values
    assert analysis.spectral_centroid is not None
    assert len(analysis.spectral_centroid) == 1800
    assert all(v >= 0 for v in analysis.spectral_centroid)

    # Musical key should be detected
    assert analysis.key is not None
    assert analysis.key in [
        "C",
        "C#",
        "D",
        "D#",
        "E",
        "F",
        "F#",
        "G",
        "G#",
        "A",
        "A#",
        "B",
        "Bb",
    ]
    assert analysis.mode in ["major", "minor"]

    # BPM and beats should still be correct
    assert analysis.bpm is not None
    assert 115 < analysis.bpm < 125


async def test_finalize_returns_audio_analysis_data(provider: SmartFadesProvider) -> None:
    """Test that _finalize returns an AudioAnalysisData on success."""
    audio_format = AudioFormat(
        content_type=ContentType.PCM_F32LE,
        bit_depth=32,
        sample_rate=44100,
        channels=2,
    )

    stream_details = Mock()
    stream_details.item_id = "test_finalize_return"
    stream_details.provider = "test"
    stream_details.queue_id = "test"
    stream_details.uri = "test://finalize_return"
    stream_details.media_type = MediaType.TRACK
    stream_details.duration = 120

    session_id = "test:test:test_finalize_return"
    await provider.start_analysis(session_id, stream_details, audio_format)

    pcm_data = FIXTURE_PCM.read_bytes()
    chunk_size = 44100 * 2 * 4
    offset = 0
    while offset < len(pcm_data):
        chunk = pcm_data[offset : offset + chunk_size]
        await provider.process_pcm_chunk(session_id, chunk)
        offset += chunk_size

    result = await provider._finalize(session_id)

    assert isinstance(result, AudioAnalysisData)


async def test_finalize_returns_none_on_early_exit(provider: SmartFadesProvider) -> None:
    """Test that _finalize returns None when not enough beats are detected."""
    audio_format = AudioFormat(
        content_type=ContentType.PCM_F32LE,
        bit_depth=32,
        sample_rate=44100,
        channels=2,
    )

    stream_details = Mock()
    stream_details.item_id = "test_finalize_none"
    stream_details.provider = "test"
    stream_details.queue_id = "test"
    stream_details.uri = "test://finalize_none"
    stream_details.media_type = MediaType.TRACK
    stream_details.duration = 120

    session_id = "test:test:test_finalize_none"
    await provider.start_analysis(session_id, stream_details, audio_format)

    pcm_data = FIXTURE_PCM.read_bytes()
    chunk_size = 44100 * 2 * 4
    offset = 0
    while offset < len(pcm_data):
        chunk = pcm_data[offset : offset + chunk_size]
        await provider.process_pcm_chunk(session_id, chunk)
        offset += chunk_size

    # Patch _infer_beat_timings to return fewer than 2 beats → triggers early exit
    with patch.object(
        provider,
        "_infer_beat_timings",
        return_value=(np.array([0.5]), np.array([])),
    ):
        result = await provider._finalize(session_id)

    assert result is None


async def test_digital_silence_yields_finite_spectral_centroid(
    provider: SmartFadesProvider,
) -> None:
    """Digitally-silent audio yields 0 Hz centroid frames instead of non-finite values."""
    sample_rate = 22050
    tone = np.sin(2 * np.pi * 440 * np.arange(sample_rate, dtype=np.float32) / sample_rate)
    pcm = np.concatenate([tone.astype(np.float32), np.zeros(sample_rate, dtype=np.float32)])
    data = Mock()
    data.energy_chunks = []
    data.frequency_band_chunks = {}
    data.centroid_chunks = []

    provider._compute_energy_and_spectral_centroids(pcm, data)

    assert data.centroid_chunks
    assert np.isfinite(np.concatenate(data.centroid_chunks)).all()


async def test_setup_raises_when_requirements_not_met(
    mass_mock: Mock, manifest_mock: Mock, config_mock: Mock
) -> None:
    """setup() fails (before importing the heavy provider module) when requirements aren't met."""
    from music_assistant.providers import smart_fades  # noqa: PLC0415

    with (
        patch(
            "music_assistant.providers.smart_fades.verify_system_meets_requirements",
            side_effect=SetupFailedError("unsupported system"),
        ),
        pytest.raises(SetupFailedError),
    ):
        await smart_fades.setup(mass_mock, manifest_mock, config_mock)
