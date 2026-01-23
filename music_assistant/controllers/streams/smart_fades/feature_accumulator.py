"""Feature Accumulator - Thread-safe storage for streaming audio features."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

import numpy as np
import numpy.typing as npt


@dataclass
class FeatureAccumulator:
    """Thread-safe accumulator for audio features extracted during streaming.

    Stores lightweight feature data (onset envelope, chroma, RMS, spectral centroid)
    incrementally as audio streams, allowing full-song analysis without holding
    entire PCM audio in memory.

    Memory usage is approximately 280KB/minute of audio:
    - Onset envelope: ~40KB/min (float32, ~170 frames/sec)
    - Chroma: ~120KB/min (12 x float32 per frame)
    - RMS: ~40KB/min (float32 per frame)
    - Spectral centroid: ~80KB/min (float64 per frame)
    """

    # Feature chunks accumulated during streaming
    onset_envelope_chunks: list[npt.NDArray[np.float32]] = field(default_factory=list)
    chroma_chunks: list[npt.NDArray[np.float32]] = field(default_factory=list)
    rms_chunks: list[npt.NDArray[np.float32]] = field(default_factory=list)
    spectral_centroid_chunks: list[npt.NDArray[np.float64]] = field(default_factory=list)

    # Track total samples processed for duration calculation
    total_samples: int = 0
    sample_rate: int = 0
    hop_length: int = 512

    # Thread safety
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def add_onset_envelope(self, envelope: npt.NDArray[np.float32]) -> None:
        """Add onset envelope chunk.

        :param envelope: Onset strength values for this chunk, shape (n_frames,).
        """
        with self._lock:
            self.onset_envelope_chunks.append(envelope)

    def add_chroma(self, chroma: npt.NDArray[np.float32]) -> None:
        """Add chroma feature chunk.

        :param chroma: Chroma features for this chunk, shape (12, n_frames).
        """
        with self._lock:
            self.chroma_chunks.append(chroma)

    def add_rms(self, rms: npt.NDArray[np.float32]) -> None:
        """Add RMS energy chunk.

        :param rms: RMS energy values for this chunk, shape (1, n_frames).
        """
        with self._lock:
            self.rms_chunks.append(rms)

    def add_spectral_centroid(self, centroid: npt.NDArray[np.float64]) -> None:
        """Add spectral centroid chunk.

        :param centroid: Spectral centroid values for this chunk, shape (1, n_frames).
        """
        with self._lock:
            self.spectral_centroid_chunks.append(centroid)

    def add_samples_processed(self, num_samples: int) -> None:
        """Track total samples processed for duration calculation.

        :param num_samples: Number of audio samples processed in this chunk.
        """
        with self._lock:
            self.total_samples += num_samples

    def get_onset_envelope(self) -> npt.NDArray[np.float32]:
        """Get concatenated onset envelope.

        :return: Full onset envelope array, shape (total_frames,).
        """
        with self._lock:
            if not self.onset_envelope_chunks:
                return np.array([], dtype=np.float32)
            return np.concatenate(self.onset_envelope_chunks)

    def get_chroma(self) -> npt.NDArray[np.float32]:
        """Get concatenated chroma features.

        :return: Full chroma array, shape (12, total_frames).
        """
        with self._lock:
            if not self.chroma_chunks:
                return np.zeros((12, 0), dtype=np.float32)
            return np.concatenate(self.chroma_chunks, axis=1)

    def get_rms(self) -> npt.NDArray[np.float32]:
        """Get concatenated RMS energy.

        :return: Full RMS array, shape (1, total_frames).
        """
        with self._lock:
            if not self.rms_chunks:
                return np.zeros((1, 0), dtype=np.float32)
            return np.concatenate(self.rms_chunks, axis=1)

    def get_spectral_centroid(self) -> npt.NDArray[np.float64]:
        """Get concatenated spectral centroid.

        :return: Full spectral centroid array, shape (1, total_frames).
        """
        with self._lock:
            if not self.spectral_centroid_chunks:
                return np.zeros((1, 0), dtype=np.float64)
            return np.concatenate(self.spectral_centroid_chunks, axis=1)

    def get_duration(self) -> float:
        """Get total duration in seconds.

        :return: Duration in seconds based on total samples processed.
        """
        if self.sample_rate == 0:
            return 0.0
        return self.total_samples / self.sample_rate

    def clear(self) -> None:
        """Clear all accumulated features."""
        with self._lock:
            self.onset_envelope_chunks.clear()
            self.chroma_chunks.clear()
            self.rms_chunks.clear()
            self.spectral_centroid_chunks.clear()
            self.total_samples = 0

    def has_sufficient_data(self, min_duration_seconds: float = 5.0) -> bool:
        """Check if sufficient data has been accumulated for analysis.

        :param min_duration_seconds: Minimum duration required for valid analysis.
        :return: True if enough data has been accumulated.
        """
        return (
            self.get_duration() >= min_duration_seconds
            and len(self.onset_envelope_chunks) > 0
            and len(self.chroma_chunks) > 0
        )
