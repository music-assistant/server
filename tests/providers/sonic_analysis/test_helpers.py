"""Unit tests for sonic_analysis helpers (feature extraction + collapse)."""

import math

import numpy as np

from music_assistant.helpers.json import json_dumps, json_loads
from music_assistant.models.audio_analysis import AudioAnalysisData
from music_assistant.providers.sonic_analysis.helpers import (
    MIN_BLOCK_SAMPLES,
    BlockFeatures,
    collapse_to_analysis,
    extract_block_features,
    merge_block_features,
)


def _make_sine(freq: float = 440.0, duration: float = 5.0, sr: int = 22050) -> np.ndarray:
    """Generate a mono sine wave for testing."""
    t = np.linspace(0, duration, int(sr * duration), endpoint=False)
    return np.sin(2 * np.pi * freq * t).astype(np.float32)


def _make_noise(duration: float = 5.0, sr: int = 22050) -> np.ndarray:
    """Generate mono white noise for testing using a fixed RNG seed."""
    rng = np.random.default_rng(42)
    return rng.standard_normal(int(sr * duration)).astype(np.float32)


def test_extract_block_features_returns_block_features() -> None:
    """Verify extract_block_features returns a BlockFeatures with correct shapes."""
    audio = _make_sine(440.0, 10.0, 22050)
    result = extract_block_features(audio, 22050)

    assert isinstance(result, BlockFeatures)
    assert len(result.chroma_frames) == 1
    assert result.chroma_frames[0].shape[0] == 12

    assert len(result.contrast_frames) == 1
    assert result.contrast_frames[0].shape[0] == 7

    assert len(result.centroid_frames) == 1
    assert len(result.flatness_frames) == 1
    assert len(result.rms_frames) == 1

    assert len(result.onset_env_frames) == 1
    assert result.onset_env_frames[0].ndim == 1


def test_extract_block_features_too_short_returns_none() -> None:
    """Verify audio shorter than MIN_BLOCK_SAMPLES returns None."""
    audio = np.zeros(MIN_BLOCK_SAMPLES - 1, dtype=np.float32)
    result = extract_block_features(audio, 22050)
    assert result is None


def test_merge_block_features() -> None:
    """Verify merging two BlockFeatures doubles all frame lists."""
    audio_a = _make_sine(440.0, 5.0, 22050)
    audio_b = _make_sine(880.0, 5.0, 22050)
    target = extract_block_features(audio_a, 22050)
    source = extract_block_features(audio_b, 22050)

    assert target is not None
    assert source is not None
    merge_block_features(target, source)

    assert len(target.chroma_frames) == 2
    assert len(target.contrast_frames) == 2
    assert len(target.centroid_frames) == 2
    assert len(target.flatness_frames) == 2
    assert len(target.rms_frames) == 2
    assert len(target.onset_env_frames) == 2


def _make_analysis(
    audio: np.ndarray | None = None, duration: float = 10.0, sr: int = 22050
) -> AudioAnalysisData:
    """Build AudioAnalysisData from a sine wave (or provided audio) via collapse_to_analysis."""
    if audio is None:
        audio = _make_sine(440.0, duration, sr)
    bf = extract_block_features(audio, sr)
    assert bf is not None
    return collapse_to_analysis(bf, sr)


def test_collapse_to_analysis_returns_audio_analysis_data() -> None:
    """Verify collapse_to_analysis returns an AudioAnalysisData instance."""
    result = _make_analysis()
    assert isinstance(result, AudioAnalysisData)


def test_collapse_to_analysis_scalars_in_unit_range() -> None:
    """All 0-1 scalar fields must be within [0.0, 1.0]."""
    result = _make_analysis()
    scalar_fields = [
        "energy",
        "brightness",
        "harmonic_complexity",
        "roughness",
        "rhythmic_regularity",
    ]
    for field_name in scalar_fields:
        value = getattr(result, field_name)
        assert value is not None, f"{field_name} should not be None"
        assert 0.0 <= value <= 1.0, f"{field_name}={value!r} is outside [0.0, 1.0]"


def test_collapse_to_analysis_loudness_values_finite() -> None:
    """Loudness fields must be finite floats."""
    result = _make_analysis()
    assert result.loudness_integrated is not None
    assert math.isfinite(result.loudness_integrated)
    assert result.loudness_range is not None
    assert math.isfinite(result.loudness_range)


def test_collapse_to_analysis_time_series_populated() -> None:
    """Time-series arrays must be populated and non-empty."""
    result = _make_analysis()

    assert result.rms_energy is not None
    assert len(result.rms_energy) > 0

    assert result.spectral_centroid is not None
    assert len(result.spectral_centroid) > 0


def test_collapse_to_analysis_replaces_non_finite_features() -> None:
    """All Sonic numeric output remains finite and survives a JSON round-trip."""
    features = extract_block_features(_make_sine(), 22050)
    assert features is not None
    non_finite = np.array([np.nan, np.inf, -np.inf], dtype=np.float32)
    for frames in (
        features.chroma_frames,
        features.contrast_frames,
        features.centroid_frames,
        features.flatness_frames,
        features.rms_frames,
        features.onset_env_frames,
    ):
        frames[0] = frames[0].copy()
        frames[0].flat[: len(non_finite)] = non_finite

    result = collapse_to_analysis(features, 22050)
    payload = result.to_dict()
    numeric_values = [value for value in payload.values() if isinstance(value, int | float)]
    numeric_values.extend(
        item for value in payload.values() if isinstance(value, list) for item in value
    )

    assert numeric_values
    assert all(math.isfinite(float(value)) for value in numeric_values)
    restored = AudioAnalysisData.from_dict(json_loads(json_dumps(payload)))
    assert restored == result


def test_collapse_to_analysis_deterministic() -> None:
    """Same input must produce identical output."""
    audio = _make_sine(440.0, 10.0, 22050)
    sr = 22050

    bf_a = extract_block_features(audio, sr)
    assert bf_a is not None
    result_a = collapse_to_analysis(bf_a, sr)

    bf_b = extract_block_features(audio, sr)
    assert bf_b is not None
    result_b = collapse_to_analysis(bf_b, sr)

    assert result_a.energy == result_b.energy
    assert result_a.brightness == result_b.brightness
    assert result_a.harmonic_complexity == result_b.harmonic_complexity
    assert result_a.roughness == result_b.roughness
    assert result_a.rhythmic_regularity == result_b.rhythmic_regularity
    assert result_a.loudness_integrated == result_b.loudness_integrated
    assert result_a.loudness_range == result_b.loudness_range
    np.testing.assert_array_equal(result_a.rms_energy, result_b.rms_energy)
    np.testing.assert_array_equal(result_a.spectral_centroid, result_b.spectral_centroid)


def test_collapse_to_analysis_noise_vs_sine_differ() -> None:
    """Noise should produce higher roughness and brightness than a pure sine tone."""
    sr = 22050
    duration = 10.0

    sine_result = _make_analysis(audio=_make_sine(440.0, duration, sr), sr=sr)
    noise_result = _make_analysis(audio=_make_noise(duration, sr), sr=sr)

    assert sine_result.roughness is not None
    assert noise_result.roughness is not None
    assert noise_result.roughness > sine_result.roughness, (
        f"Expected noise roughness ({noise_result.roughness}) > "
        f"sine roughness ({sine_result.roughness})"
    )

    assert sine_result.brightness is not None
    assert noise_result.brightness is not None
    assert noise_result.brightness > sine_result.brightness, (
        f"Expected noise brightness ({noise_result.brightness}) > "
        f"sine brightness ({sine_result.brightness})"
    )


def test_collapse_to_analysis_overlay_owned_fields_are_none() -> None:
    """Fields owned by overlay providers must be left None by sonic_analysis."""
    result = _make_analysis()
    assert result.bpm is None
    assert result.key is None
    assert result.mode is None
    assert result.danceability is None
    assert result.valence is None
    assert result.arousal is None
    assert result.instrumentalness is None
    assert result.acousticness is None
    assert result.speechiness is None
