"""Tests for smart fades analysis helper functions."""

import numpy as np

from music_assistant.providers.smart_fades.analysis_helpers import (
    compute_rms_per_second,
    compute_stft_features,
    detect_key,
)


def test_compute_rms_per_second_sine_wave() -> None:
    """RMS of a sine wave at known amplitude should be amplitude / sqrt(2)."""
    sr = 22050
    duration = 5  # seconds
    amplitude = 0.5
    t = np.linspace(0, duration, sr * duration, endpoint=False, dtype=np.float32)
    sine = amplitude * np.sin(2 * np.pi * 440 * t)

    rms = compute_rms_per_second(sine, sr)

    assert len(rms) == duration
    expected_rms = amplitude / np.sqrt(2)
    for val in rms:
        assert abs(val - expected_rms) < 0.01, f"Expected ~{expected_rms:.3f}, got {val:.3f}"


def test_compute_rms_per_second_silence() -> None:
    """RMS of silence should be zero."""
    sr = 22050
    silence = np.zeros(sr * 3, dtype=np.float32)

    rms = compute_rms_per_second(silence, sr)

    assert len(rms) == 3
    for val in rms:
        assert val == 0.0


def test_compute_rms_per_second_partial_second() -> None:
    """Samples less than 1 second should return empty array."""
    sr = 22050
    short = np.ones(sr // 2, dtype=np.float32)

    rms = compute_rms_per_second(short, sr)

    assert len(rms) == 0


def test_compute_stft_features_sine_wave() -> None:
    """Spectral centroid of a 440 Hz sine should be close to 440 Hz."""
    sr = 22050
    duration = 5
    t = np.linspace(0, duration, sr * duration, endpoint=False, dtype=np.float32)
    sine = np.sin(2 * np.pi * 440 * t)

    centroid_per_sec, chroma_per_sec, _bass_chroma_per_sec = compute_stft_features(sine, sr)

    assert len(centroid_per_sec) == duration
    # Spectral centroid of a pure 440 Hz tone should be ~440 Hz
    for val in centroid_per_sec:
        assert 400 < val < 480, f"Expected centroid ~440 Hz, got {val:.1f}"

    # Chroma should have 12 bins per second
    assert chroma_per_sec.shape == (duration, 12)
    # 440 Hz = A4, which is chroma bin 9 (A). Should be dominant.
    mean_chroma = chroma_per_sec.mean(axis=0)
    assert np.argmax(mean_chroma) == 9, (
        f"Expected A (bin 9) dominant, got bin {np.argmax(mean_chroma)}"
    )


def test_compute_stft_features_empty() -> None:
    """Empty audio should return empty arrays."""
    sr = 22050
    empty = np.array([], dtype=np.float32)
    centroid, chroma, bass_chroma = compute_stft_features(empty, sr)
    assert len(centroid) == 0
    assert chroma.shape[1] == 12
    assert bass_chroma.shape[1] == 12


def test_compute_stft_features_harmonic_isolation() -> None:
    """HPSS should produce cleaner chroma even with percussive noise."""
    sr = 22050
    duration = 5
    t = np.linspace(0, duration, sr * duration, endpoint=False, dtype=np.float32)
    sine = np.sin(2 * np.pi * 440 * t)
    rng = np.random.default_rng(42)
    clicks = np.zeros_like(sine)
    for i in range(0, len(clicks), sr // 2):
        clicks[i : i + 100] = rng.standard_normal(min(100, len(clicks) - i))
    mixed = (sine + 0.5 * clicks).astype(np.float32)

    _centroid, chroma, _bass_chroma = compute_stft_features(mixed, sr)

    mean_chroma = chroma.mean(axis=0)
    assert np.argmax(mean_chroma) == 9, (
        f"Expected A (bin 9) dominant, got bin {np.argmax(mean_chroma)}"
    )
    sorted_chroma = np.sort(mean_chroma)[::-1]
    assert sorted_chroma[0] > sorted_chroma[1] * 1.2, (
        "A bin should be at least 20% stronger than next bin after HPSS"
    )


def test_detect_key_c_major() -> None:
    """Chroma weighted toward C, E, G should detect C major."""
    chroma = np.zeros((20, 12), dtype=np.float32)
    chroma[:, 0] = 1.0  # C
    chroma[:, 4] = 0.8  # E
    chroma[:, 7] = 0.6  # G

    key = detect_key(chroma, duration=20.0)

    assert key["root"] == "C"
    assert key["mode"] == "major"
    assert key["confidence"] > 0.5


def test_detect_key_a_minor() -> None:
    """Chroma weighted toward A, C, E should detect A minor."""
    chroma = np.zeros((20, 12), dtype=np.float32)
    chroma[:, 9] = 1.0  # A
    chroma[:, 0] = 0.8  # C
    chroma[:, 4] = 0.6  # E

    key = detect_key(chroma, duration=20.0)

    assert key["root"] == "A"
    assert key["mode"] == "minor"
    assert key["confidence"] > 0.5


def test_detect_key_filters_intro_outro() -> None:
    """First and last 10s should be excluded from key detection."""
    chroma = np.zeros((30, 12), dtype=np.float32)
    # First/last 10s: F major (F=5, A=9, C=0)
    chroma[:10, 5] = 1.0
    chroma[:10, 9] = 0.8
    chroma[:10, 0] = 0.6
    chroma[20:, 5] = 1.0
    chroma[20:, 9] = 0.8
    chroma[20:, 0] = 0.6
    # Middle 10s: C major
    chroma[10:20, 0] = 1.0
    chroma[10:20, 4] = 0.8
    chroma[10:20, 7] = 0.6

    key = detect_key(chroma, duration=30.0)

    assert key["root"] == "C"
    assert key["mode"] == "major"


def test_detect_key_bass_tonic_disambiguation() -> None:
    """Bass chroma should disambiguate tonic from dominant.

    Full-range chroma is ambiguous between F major and C major.
    Bass chroma clearly shows F as the bass note (tonic).
    """
    chroma = np.zeros((20, 12), dtype=np.float32)
    # F major triad with C nearly as strong as F (tonic-dominant ambiguity)
    chroma[:, 5] = 0.9  # F
    chroma[:, 9] = 0.7  # A
    chroma[:, 0] = 0.85  # C — almost as strong as F

    # Bass chroma: F dominates the bass register
    bass_chroma = np.zeros((20, 12), dtype=np.float32)
    bass_chroma[:, 5] = 1.0  # F strong in bass
    bass_chroma[:, 0] = 0.2  # C weak in bass

    key = detect_key(chroma, duration=20.0, bass_chroma_per_second=bass_chroma)

    assert key["root"] == "F", f"Expected F major, got {key['root']} {key['mode']}"
    assert key["mode"] == "major"
