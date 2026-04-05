"""Helper functions for extended audio analysis features.

Computes energy curves, spectral centroids, chroma/key detection,
and phrase boundary detection from streaming PCM audio. Uses librosa
for STFT-based features computed directly on pcm_22k blocks.
"""

from __future__ import annotations

import logging
from typing import TypedDict, cast

import librosa
import numpy as np
import numpy.typing as npt

logger = logging.getLogger(__name__)


class MusicalKeyResult(TypedDict):
    """Return type for detect_key."""

    root: str
    mode: str
    confidence: float


def compute_rms_per_second(
    pcm: npt.NDArray[np.float32],
    sr: int = 22050,
) -> npt.NDArray[np.float32]:
    """Compute RMS energy per second from PCM audio.

    No STFT involved — pure amplitude computation with zero edge effects.
    Streaming-safe: each second is independent.

    :param pcm: Audio samples as float32 array at the given sample rate.
    :param sr: Sample rate in Hz.
    :return: Array of RMS values, one per full second of audio.
    """
    n_full_seconds = len(pcm) // sr
    if n_full_seconds == 0:
        return np.array([], dtype=np.float32)
    trimmed = pcm[: n_full_seconds * sr]
    frames = trimmed.reshape(n_full_seconds, sr)
    return cast("npt.NDArray[np.float32]", np.sqrt(np.mean(frames**2, axis=1)).astype(np.float32))


def compute_stft_features(
    pcm: npt.NDArray[np.float32],
    sr: int = 22050,
    n_fft: int = 2048,
    hop_length: int = 512,
) -> tuple[npt.NDArray[np.float32], npt.NDArray[np.float32], npt.NDArray[np.float32]]:
    """Compute spectral centroid and chroma from a single librosa STFT.

    Computes one STFT on the pcm block and derives both features from it.
    Per-second averaging reduces per-frame data to one value per second.

    Boundary artifacts at 10s block edges are negligible (~1.2% on per-second
    values) and do not affect phrase detection thresholds.

    :param pcm: Audio samples as float32 array at the given sample rate.
    :param sr: Sample rate in Hz.
    :param n_fft: FFT window size.
    :param hop_length: Hop length between STFT frames.
    :return: Tuple of (centroid_per_second, chroma_per_second, bass_chroma_per_second).
             centroid_per_second shape: (T_seconds,)
             chroma_per_second shape: (T_seconds, 12)
             bass_chroma_per_second shape: (T_seconds, 12)
    """
    if len(pcm) < n_fft:
        empty_centroid = np.array([], dtype=np.float32)
        empty_chroma = np.zeros((0, 12), dtype=np.float32)
        return empty_centroid, empty_chroma, empty_chroma

    # Compute STFT once — all features derived from this
    stft_matrix = np.abs(librosa.stft(y=pcm, n_fft=n_fft, hop_length=hop_length, center=True))

    # Zero out sub-bass bins (<50Hz) before computing centroid.
    # Sub-bass rumble and DC offset bias the centroid downward without
    # contributing to perceived brightness used for crossfade alignment.
    freqs = librosa.fft_frequencies(sr=sr, n_fft=n_fft)
    centroid_stft = stft_matrix.copy()
    centroid_stft[freqs < 50] = 0

    # Spectral centroid per frame (using sub-bass filtered STFT)
    centroid_per_frame = librosa.feature.spectral_centroid(
        S=centroid_stft,
        sr=sr,
        n_fft=n_fft,
        hop_length=hop_length,
    )[0]

    # Isolate harmonic content for chroma — removes percussive transients
    # that inject broadband energy into all chroma bins.
    # margin=8 is aggressive separation (librosa "enhanced chroma" recipe).
    pcm_harmonic = librosa.effects.harmonic(pcm, margin=8)

    # Full-range chroma via CQT on harmonic signal
    chroma_per_frame = librosa.feature.chroma_cqt(
        y=pcm_harmonic,
        sr=sr,
        hop_length=hop_length,
        tuning=0.0,
    )  # shape: (12, T_frames)

    # Bass-register chroma (C2-B2, ~65-130Hz) for tonic disambiguation
    bass_chroma_per_frame = librosa.feature.chroma_cqt(
        y=pcm_harmonic,
        sr=sr,
        hop_length=hop_length,
        tuning=0.0,
        fmin=librosa.note_to_hz("C2"),
        n_octaves=1,
    )  # shape: (12, T_frames)

    # Average to per-second
    frames_per_sec = sr // hop_length
    if frames_per_sec == 0:
        frames_per_sec = 1
    n_full_seconds = len(centroid_per_frame) // frames_per_sec

    if n_full_seconds == 0:
        empty_centroid = np.array([], dtype=np.float32)
        empty_chroma = np.zeros((0, 12), dtype=np.float32)
        return empty_centroid, empty_chroma, empty_chroma

    trimmed_centroid = centroid_per_frame[: n_full_seconds * frames_per_sec]
    # Use nanmean to handle silent frames where librosa returns NaN (0/0 in weighted mean)
    centroid_per_sec = np.nanmean(trimmed_centroid.reshape(n_full_seconds, frames_per_sec), axis=1)
    # Replace any remaining NaN (fully silent seconds) with 0.0
    np.nan_to_num(centroid_per_sec, copy=False, nan=0.0)

    trimmed_chroma = chroma_per_frame[:, : n_full_seconds * frames_per_sec]
    chroma_per_sec = trimmed_chroma.reshape(12, n_full_seconds, frames_per_sec).mean(axis=2).T

    trimmed_bass = bass_chroma_per_frame[:, : n_full_seconds * frames_per_sec]
    bass_chroma_per_sec = trimmed_bass.reshape(12, n_full_seconds, frames_per_sec).mean(axis=2).T

    return (
        centroid_per_sec.astype(np.float32),
        chroma_per_sec.astype(np.float32),
        bass_chroma_per_sec.astype(np.float32),
    )


# Sha'ath key profiles (libKeyFinder / Mixxx) — empirically tuned for
# Pearson-correlation key-finding on pop, rock, and electronic music.
# Better genre fit than classical-trained Albrecht-Shanahan profiles.
_KEY_PROFILE_MAJOR = np.array(
    [7.24, 3.50, 3.58, 2.85, 5.82, 4.56, 2.45, 6.99, 3.39, 4.56, 4.07, 4.46]
)
_KEY_PROFILE_MINOR = np.array(
    [7.00, 3.14, 4.36, 5.40, 3.67, 4.09, 3.91, 6.20, 3.63, 2.87, 5.35, 3.83]
)
_NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def detect_key(  # noqa: PLR0915
    chroma_per_second: npt.NDArray[np.float32],
    duration: float,  # noqa: ARG001
    energy_per_second: npt.NDArray[np.float32] | None = None,
    bass_chroma_per_second: npt.NDArray[np.float32] | None = None,
) -> MusicalKeyResult:
    """Detect musical key using Sha'ath profiles with Pearson correlation.

    Filters out the first and last 10 seconds of chroma data to avoid
    intro/outro skew from ambient pads or sparse instrumentation.
    When energy_per_second is provided, weights chroma by RMS energy so
    loud sections (choruses, drops) contribute more than quiet breakdowns.
    When bass_chroma_per_second is provided, blends bass-register chroma
    into the main chroma to help disambiguate tonic from dominant.

    :param chroma_per_second: Array of shape (T_seconds, 12) with chroma energy per second.
    :param duration: Total track duration in seconds.
    :param energy_per_second: Optional RMS energy per second for energy-weighted aggregation.
    :param bass_chroma_per_second: Optional bass-register chroma for tonic disambiguation.
    :return: Dict with keys 'root', 'mode', 'confidence' (MusicalKey-compatible).
    """
    if len(chroma_per_second) == 0:
        return {"root": "C", "mode": "major", "confidence": 0.0}

    if len(chroma_per_second) > 20:
        trimmed = chroma_per_second[10:-10]
        trimmed_energy = energy_per_second[10:-10] if energy_per_second is not None else None
        trimmed_bass = (
            bass_chroma_per_second[10:-10] if bass_chroma_per_second is not None else None
        )
    else:
        trimmed = chroma_per_second
        trimmed_energy = energy_per_second
        trimmed_bass = bass_chroma_per_second

    # Energy-weighted chroma aggregation: loud sections contribute more
    if trimmed_energy is not None and len(trimmed_energy) == len(trimmed):
        weights = trimmed_energy[:, np.newaxis]  # (T, 1) for broadcasting
        mean_chroma = (trimmed * weights).sum(axis=0)
        weight_sum = weights.sum()
        mean_chroma = mean_chroma / weight_sum if weight_sum > 0 else trimmed.mean(axis=0)
    else:
        mean_chroma = trimmed.mean(axis=0)

    # Blend bass-register chroma to help disambiguate tonic from dominant.
    # The tonic almost always dominates the bass, while the dominant does not.
    if trimmed_bass is not None and len(trimmed_bass) == len(trimmed):
        mean_bass = trimmed_bass.mean(axis=0)
        bass_norm = mean_bass.sum()
        if bass_norm > 1e-10:
            mean_bass = mean_bass / bass_norm
            mean_chroma = 0.8 * mean_chroma + 0.2 * mean_bass

    if mean_chroma.sum() < 1e-10:
        return {"root": "C", "mode": "major", "confidence": 0.0}

    # Log raw chroma distribution before normalization
    chroma_labels = " ".join(f"{_NOTE_NAMES[i]}={mean_chroma[i]:.3f}" for i in range(12))
    top_bins = sorted(range(12), key=lambda i: mean_chroma[i], reverse=True)[:4]
    top_str = ", ".join(f"{_NOTE_NAMES[i]}({mean_chroma[i]:.3f})" for i in top_bins)
    logger.debug("detect_key: raw chroma=[%s], top 4: %s", chroma_labels, top_str)

    # L2-normalize chroma vector for numerical stability
    norm = np.linalg.norm(mean_chroma)
    if norm > 0:
        mean_chroma = mean_chroma / norm

    best_corr = -2.0
    best_root = 0
    best_mode = "major"

    all_scores: list[tuple[float, str, str]] = []
    for shift in range(12):
        rotated = np.roll(mean_chroma, -shift)
        corr_major = float(np.corrcoef(rotated, _KEY_PROFILE_MAJOR)[0, 1])
        corr_minor = float(np.corrcoef(rotated, _KEY_PROFILE_MINOR)[0, 1])
        all_scores.append((corr_major, _NOTE_NAMES[shift], "major"))
        all_scores.append((corr_minor, _NOTE_NAMES[shift], "minor"))
        if corr_major > best_corr:
            best_corr = corr_major
            best_root = shift
            best_mode = "major"
        if corr_minor > best_corr:
            best_corr = corr_minor
            best_root = shift
            best_mode = "minor"

    # Log top 5 key candidates
    all_scores.sort(reverse=True)
    top5 = ", ".join(f"{n} {m}({c:.3f})" for c, n, m in all_scores[:5])
    logger.debug("detect_key: top 5 candidates: %s", top5)

    # Map realistic correlation range [0.3, 0.9] to confidence [0, 1]
    confidence = max(0.0, min(1.0, (best_corr - 0.3) / 0.6))

    return {
        "root": _NOTE_NAMES[best_root],
        "mode": best_mode,
        "confidence": round(confidence, 3),
    }
