"""Log-mel spectrogram feature extractor for Beat This model."""

from __future__ import annotations

import asyncio
import math
from typing import TYPE_CHECKING, Any

import numpy as np
import torch
import torchaudio

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


class AdvancedBeatFeatureExtractor:
    """
    Streaming log-mel extractor using torchaudio for Beat This compatibility.

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
    corresponding to each chunk's sample range. Delays the last few frames
    of each chunk so they can be recomputed with real forward context from
    the next chunk, avoiding reflect-padding artifacts at block boundaries.

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
        offload: Callable[..., Awaitable[Any]] | None = None,
    ):
        """
        Initialize the feature extractor.

        :param sample_rate: Audio sample rate (default 22050 Hz).
        :param n_fft: FFT window size.
        :param hop_length: Hop length between frames.
        :param n_mels: Number of mel frequency bins.
        :param fmin: Minimum frequency for mel filter.
        :param fmax: Maximum frequency for mel filter.
        :param device: Torch device to use.
        :param offload: Awaitable runner for the blocking mel extraction. When given (the
            provider passes its concurrency-bounded runner), it is used instead of a plain
            asyncio.to_thread so the work counts against the host's analysis CPU cap.
        """
        self._offload = offload
        self.n_fft = n_fft
        self.hop_length = hop_length
        self.sample_rate = sample_rate
        self._device = device
        self._n_mels = n_mels

        # Number of frames to delay at the end of each chunk so they can be
        # recomputed with real forward context from the next chunk.
        # These frames need n_fft/2 samples of forward context that we don't
        # have yet; delaying lets the next chunk provide it.
        self._frames_to_delay = math.ceil((n_fft // 2) / hop_length)

        # Track the total samples accumulated so far
        self._total_samples = 0

        # Buffer to hold samples from previous chunk needed for frame computation.
        # Must be large enough to cover backward context for delayed frames:
        # n_fft//2 (mel window half) + _frames_to_delay * hop_length (delayed range)
        # + hop_length (hop-alignment margin).
        self._keep_samples = n_fft + self._frames_to_delay * hop_length
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
        """
        Process a PCM chunk and return log-mel features.

        :param pcm: Audio samples as float32 array.
        :return: Log-mel features with shape (T, n_mels).
        """

        def _process_sync() -> np.ndarray:
            chunk_start = self._total_samples
            chunk_end = chunk_start + len(pcm)

            # Determine which frames belong to this chunk.
            # Frame j is centered at sample j * hop_length.
            # If we have delayed frames from the previous chunk, start from there.
            if chunk_start == 0:
                first_frame = 0
            elif self._last_output_frame >= 0:
                first_frame = self._last_output_frame + 1
            else:
                first_frame = (chunk_start + self.hop_length - 1) // self.hop_length

            # Last frame whose center is within this chunk
            last_frame = (chunk_end - 1) // self.hop_length

            # Delay the last frames so they can be recomputed with forward
            # context from the next chunk. This avoids reflect-padding artifacts
            # at block boundaries.
            output_last_frame = last_frame - self._frames_to_delay

            if output_last_frame < first_frame:
                # Not enough frames to output anything yet; store samples and wait
                if self._prev_samples is not None:
                    combined = np.concatenate([self._prev_samples, pcm])
                else:
                    combined = pcm
                self._prev_samples = combined[-self._keep_samples :].copy()
                self._total_samples = chunk_end
                return np.array([], dtype=np.float32).reshape(0, self._n_mels)

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

            self._prev_samples = audio_segment[-self._keep_samples :].copy()

            # Update total samples
            self._total_samples = chunk_end

            # Compute mel spectrogram
            tensor = torch.from_numpy(audio_segment).to(self._device)
            with torch.inference_mode():
                mel = self._mel_spec(tensor)
                log_mel = torch.log1p(1000.0 * mel)
            features = log_mel.T.cpu().numpy()

            # Extract frames for this chunk using integer arithmetic.
            # Since audio_start is hop-aligned, segment frames map exactly to global frames.
            segment_first_global_frame = audio_start // self.hop_length

            start_in_segment = first_frame - segment_first_global_frame
            end_in_segment = output_last_frame - segment_first_global_frame + 1

            start_in_segment = max(0, start_in_segment)
            end_in_segment = min(len(features), end_in_segment)

            self._last_output_frame = output_last_frame

            return features[start_in_segment:end_in_segment]

        if self._offload is not None:
            offloaded: np.ndarray = await self._offload(_process_sync)
            return offloaded
        return await asyncio.to_thread(_process_sync)

    async def finalize(self) -> np.ndarray:
        """
        Flush delayed frames and process any remaining samples.

        :return: Final log-mel features.
        """

        def _finalize_sync() -> np.ndarray:
            if self._prev_samples is None or len(self._prev_samples) == 0:
                return np.array([], dtype=np.float32).reshape(0, self._n_mels)

            # mel_spec reflect-pad requires len > n_fft // 2
            if len(self._prev_samples) <= self.n_fft // 2:
                return np.array([], dtype=np.float32).reshape(0, self._n_mels)

            # MelSpectrogram(center=True) produces 1 + total_samples // hop frames.
            total_frames = 1 + self._total_samples // self.hop_length
            extra_count = total_frames - self._last_output_frame - 1
            if extra_count <= 0:
                return np.array([], dtype=np.float32).reshape(0, self._n_mels)

            tensor = torch.from_numpy(self._prev_samples).to(self._device)
            with torch.inference_mode():
                mel = self._mel_spec(tensor)
                log_mel = torch.log1p(1000.0 * mel)
            features = log_mel.T.cpu().numpy()

            return features[-extra_count:]

        if self._offload is not None:
            offloaded: np.ndarray = await self._offload(_finalize_sync)
            return offloaded
        return await asyncio.to_thread(_finalize_sync)

    def reset(self) -> None:
        """Reset state for processing a new audio stream."""
        self._total_samples = 0
        self._prev_samples = None
        self._last_output_frame = -1
