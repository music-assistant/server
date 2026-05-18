"""
Convert a Sendspin VisualizerFrame into a WLED V2 audio-sync frame.

Sendspin pre-computes per-frame audio features (loudness, dominant peak
frequency, log-spaced spectrum bins) and ships them as
`aiosendspin.models.visualizer.VisualizerFrame` objects. This analyzer
maps those features onto the 6 wire-format fields the WLED V2 protocol
expects, applying a small amount of stateful post-processing (a global
AGC envelope for the spectrum, exponential smoothing for sampleSmth,
and rolling-stats beat detection for samplePeak).

The analyzer is intentionally MA-decoupled: it imports nothing from
`music_assistant.*`. Its only runtime dependency outside the bridge
package is `aiosendspin` for the `VisualizerFrame` type.
"""

from __future__ import annotations

from collections import deque
from math import exp
from typing import TYPE_CHECKING

from .encoder import WledV2Frame

if TYPE_CHECKING:
    from aiosendspin.models.visualizer import VisualizerFrame

# WLED V2 fftResult[16]: 16 uint8 GEQ bins covering the audible band.
WLED_FFT_BANDS = 16
# Default visualizer cadence in Hz we ask Sendspin to deliver at.
DEFAULT_VISUALIZER_RATE_HZ = 43
# Loudness on the wire is uint16 (0-65535). Map to the 0-255 scale the
# firmware capture uses for sampleRaw / sampleSmth.
_LOUDNESS_MAX = 65535.0
_SAMPLE_SCALE = 255.0
# AGC envelope floor — keeps very small absolute spectrum values from being
# amplified to full scale during near-silence.
_AGC_FLOOR = 1.0
# Exponential-smoothing alpha for sampleSmth.
_SAMPLE_SMTH_ALPHA = 0.3
# Beat detection: tolerance and history.
_PEAK_THRESHOLD_STD = 1.5
_PEAK_HISTORY_FRAMES = 16
_MIN_PEAK_HISTORY = 4


class WledAudioAnalyzer:
    """
    Stateful transformation of `VisualizerFrame`s into `WledV2Frame`s.

    A fresh analyzer is constructed per playback session so each track
    starts with a clean AGC envelope and an empty peak-detect history.
    """

    def __init__(
        self,
        agc_release_frames: int = DEFAULT_VISUALIZER_RATE_HZ,
        peak_history_frames: int = _PEAK_HISTORY_FRAMES,
        peak_threshold_std: float = _PEAK_THRESHOLD_STD,
        sample_smth_alpha: float = _SAMPLE_SMTH_ALPHA,
    ) -> None:
        """
        Build a fresh analyzer with default smoothing constants.

        :param agc_release_frames: Frames over which the global AGC envelope
            decays. At ~43 Hz, 43 frames ≈ 1 s.
        :param peak_history_frames: Rolling-window length for beat detection.
        :param peak_threshold_std: Std-deviations above rolling mean to flag a peak.
        :param sample_smth_alpha: Exponential-smoothing alpha for sampleSmth.
        """
        self._agc_envelope: float = _AGC_FLOOR
        self._agc_decay: float = exp(-1.0 / max(1, agc_release_frames))
        self._smth_alpha: float = sample_smth_alpha
        self._smth_value: float = 0.0
        self._peak_history: deque[float] = deque(maxlen=peak_history_frames)
        self._peak_threshold_std: float = peak_threshold_std

    def process_frame(self, frame: VisualizerFrame) -> WledV2Frame | None:
        """
        Map one `VisualizerFrame` to a `WledV2Frame` ready for encoding.

        :param frame: A Sendspin visualizer frame containing loudness,
            f_peak, and a spectrum list.
        :return: A ready-to-encode V2 frame, or None if the input lacks a
            usable spectrum (in which case the caller should skip the emit).
        """
        spectrum_raw = frame.spectrum
        if spectrum_raw is None:
            return None

        # Pad / truncate to exactly 16 bins.
        spectrum: list[int]
        if len(spectrum_raw) >= WLED_FFT_BANDS:
            spectrum = list(spectrum_raw[:WLED_FFT_BANDS])
        else:
            spectrum = list(spectrum_raw) + [0] * (WLED_FFT_BANDS - len(spectrum_raw))

        # sampleRaw — uint16 loudness mapped into 0-255 float.
        loudness = float(frame.loudness or 0)
        sample_raw = min(_SAMPLE_SCALE, loudness / _LOUDNESS_MAX * _SAMPLE_SCALE)

        # sampleSmth — exponentially-smoothed sampleRaw.
        self._smth_value = (
            self._smth_alpha * sample_raw + (1.0 - self._smth_alpha) * self._smth_value
        )
        sample_smth = self._smth_value

        # Global AGC over the 16 spectrum bins; track the loudest current bin
        # with exponential release so quiet bands still get a fair share of
        # the 0-255 range without saturating every band on Hann-window leakage.
        max_bin = max(spectrum)
        self._agc_envelope = max(self._agc_envelope * self._agc_decay, float(max_bin))
        denom = max(self._agc_envelope, _AGC_FLOOR)
        fft_bands = bytes(min(255, round(value * _SAMPLE_SCALE / denom)) for value in spectrum)

        # samplePeak — current sampleRaw exceeds rolling mean + N·stddev.
        sample_peak = 0
        if len(self._peak_history) >= _MIN_PEAK_HISTORY:
            mean = sum(self._peak_history) / len(self._peak_history)
            var = sum((x - mean) ** 2 for x in self._peak_history) / len(self._peak_history)
            std = max(var**0.5, 1e-3)
            if sample_raw > mean + self._peak_threshold_std * std:
                sample_peak = 1
        self._peak_history.append(sample_raw)

        # FFT_MajorPeak — pass-through (Sendspin already gives us Hz).
        fft_major_peak_hz = float(frame.f_peak or 0)
        # FFT_Magnitude — Sendspin doesn't expose an absolute magnitude, so
        # use the loudest spectrum-bin value as a proxy. Receivers that key
        # off magnitude treat it as a relative scale anyway.
        fft_magnitude = float(max_bin)

        return WledV2Frame(
            sample_raw=sample_raw,
            sample_smth=sample_smth,
            sample_peak=sample_peak,
            fft_bands=fft_bands,
            fft_magnitude=fft_magnitude,
            fft_major_peak_hz=fft_major_peak_hz,
        )
