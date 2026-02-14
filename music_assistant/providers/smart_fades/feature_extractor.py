"""Log-mel spectrogram feature extractor for Beat This model."""

from __future__ import annotations

import asyncio

import numpy as np
import torch
import torchaudio


class AdvancedBeatFeatureExtractor:
    """Streaming log-mel extractor using torchaudio for exact Beat This compatibility.

    Uses the same torchaudio.transforms.MelSpectrogram as beat_this.preprocessing.LogMelSpect:
    - sample_rate=22050
    - n_fft=1024
    - hop_length=441
    - f_min=30, f_max=11000
    - n_mels=128
    - mel_scale="slaney"
    - normalized="frame_length"
    - power=1
    - center=True

    Uses a sample-precise overlap approach that extracts exactly the frames
    corresponding to each chunk's sample range.

    Assumes input PCM is already at 22050 Hz mono.
    """

    def __init__(
        self,
        sample_rate: int = 22050,
        n_fft: int = 1024,
        hop_length: int = 441,
        n_mels: int = 128,
        fmin: float = 30.0,
        fmax: float = 11000.0,
        device: str = "cpu",
    ):
        """Initialize the feature extractor.

        :param sample_rate: Audio sample rate (default 22050 Hz).
        :param n_fft: FFT window size.
        :param hop_length: Hop length between frames.
        :param n_mels: Number of mel frequency bins.
        :param fmin: Minimum frequency for mel filter.
        :param fmax: Maximum frequency for mel filter.
        :param device: Torch device to use.
        """
        self.n_fft = n_fft
        self.hop_length = hop_length
        self.sample_rate = sample_rate
        self._device = device
        self._n_mels = n_mels

        # Track the total samples accumulated so far
        self._total_samples = 0

        # Buffer to hold samples from previous chunk needed for frame computation
        # We need n_fft/2 samples before each chunk boundary to compute edge frames
        self._prev_samples: np.ndarray | None = None

        # Use torchaudio MelSpectrogram with center=True (standard beat_this approach)
        self._mel_spec = torchaudio.transforms.MelSpectrogram(
            sample_rate=sample_rate,
            n_fft=n_fft,
            hop_length=hop_length,
            f_min=fmin,
            f_max=fmax,
            n_mels=n_mels,
            mel_scale="slaney",
            normalized="frame_length",
            power=1,
            center=True,
        ).to(device)

        # Track the last global frame index output by process_pcm
        self._last_output_frame = -1

    async def process_pcm(self, pcm: np.ndarray) -> np.ndarray:
        """Process a PCM chunk and return log-mel features.

        :param pcm: Audio samples as float32 array.
        :return: Log-mel features with shape (T, n_mels).
        """

        def _process_sync() -> np.ndarray:
            chunk_start = self._total_samples
            chunk_end = chunk_start + len(pcm)

            # Determine which frames belong to this chunk
            # Frame j is centered at sample j * hop_length
            if chunk_start == 0:
                first_frame = 0
            else:
                # First frame whose center is >= chunk start (ceiling division)
                first_frame = (chunk_start + self.hop_length - 1) // self.hop_length

            # Last frame whose center is within this chunk
            last_frame = (chunk_end - 1) // self.hop_length

            # Determine the audio range needed to compute these frames.
            # Align audio_start to a hop_length boundary so segment frames
            # correspond exactly to global frame positions.
            needed_start = max(0, first_frame * self.hop_length - self.n_fft // 2)
            audio_start = (needed_start // self.hop_length) * self.hop_length

            # Build the audio segment to process
            if self._prev_samples is not None and audio_start < chunk_start:
                # We need some samples from the previous chunk
                prev_needed = chunk_start - audio_start
                prev_to_use = self._prev_samples[-prev_needed:]
                audio_segment = np.concatenate([prev_to_use, pcm])
            else:
                audio_segment = pcm
                audio_start = chunk_start

            # Store end samples for next chunk.
            # The hop-aligned start may need up to n_fft//2 + hop - 1 = 952 samples
            # before chunk_start; use n_fft (1024) for a clean margin.
            keep_samples = self.n_fft
            self._prev_samples = pcm[-keep_samples:].copy()

            # Update total samples
            self._total_samples = chunk_end

            # Compute mel spectrogram
            tensor = torch.from_numpy(audio_segment).to(self._device)
            with torch.no_grad():
                mel = self._mel_spec(tensor)
                log_mel = torch.log1p(1000.0 * mel)
            features = log_mel.T.cpu().numpy().astype(np.float32)

            # Extract frames for this chunk using integer arithmetic.
            # Since audio_start is hop-aligned, segment frames map exactly to global frames.
            segment_first_global_frame = audio_start // self.hop_length

            start_in_segment = first_frame - segment_first_global_frame
            end_in_segment = last_frame - segment_first_global_frame + 1

            start_in_segment = max(0, start_in_segment)
            end_in_segment = min(len(features), end_in_segment)

            self._last_output_frame = last_frame

            return features[start_in_segment:end_in_segment]

        return await asyncio.to_thread(_process_sync)

    async def finalize(self) -> np.ndarray:
        """Process any remaining samples to get the final frames.

        :return: Final log-mel features.
        """

        def _finalize_sync() -> np.ndarray:
            if self._prev_samples is None or len(self._prev_samples) == 0:
                return np.array([], dtype=np.float32).reshape(0, self._n_mels)

            # MelSpectrogram(center=True) produces 1 + total_samples // hop frames.
            # process_pcm outputs up to frame (total_samples - 1) // hop.
            # When total_samples is exactly divisible by hop, one frame is missed.
            total_frames = 1 + self._total_samples // self.hop_length
            extra_count = total_frames - self._last_output_frame - 1
            if extra_count <= 0:
                return np.array([], dtype=np.float32).reshape(0, self._n_mels)

            tensor = torch.from_numpy(self._prev_samples).to(self._device)
            with torch.no_grad():
                mel = self._mel_spec(tensor)
                log_mel = torch.log1p(1000.0 * mel)
            features = log_mel.T.cpu().numpy().astype(np.float32)

            return features[-extra_count:]

        return await asyncio.to_thread(_finalize_sync)

    def reset(self) -> None:
        """Reset state for processing a new audio stream."""
        self._total_samples = 0
        self._prev_samples = None
        self._last_output_frame = -1
