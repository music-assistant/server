# processors/beat_this/features.py

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

        self._log_multiplier = 1000.0

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

            # Determine the audio range needed to compute these frames
            # Frame first_frame needs samples from (first_frame * hop_length - n_fft/2)
            audio_start = max(0, first_frame * self.hop_length - self.n_fft // 2)

            # Build the audio segment to process
            if self._prev_samples is not None and audio_start < chunk_start:
                # We need some samples from the previous chunk
                prev_needed = chunk_start - audio_start
                prev_to_use = self._prev_samples[-prev_needed:]
                audio_segment = np.concatenate([prev_to_use, pcm])
            else:
                audio_segment = pcm
                audio_start = chunk_start

            # Store end samples for next chunk
            # Need up to n_fft/2 samples before the next chunk boundary
            keep_samples = self.n_fft // 2 + self.hop_length
            self._prev_samples = pcm[-keep_samples:].copy()

            # Update total samples
            self._total_samples = chunk_end

            # Compute mel spectrogram
            tensor = torch.from_numpy(audio_segment).to(self._device)
            with torch.no_grad():
                mel = self._mel_spec(tensor)
                log_mel = torch.log1p(self._log_multiplier * mel)
            features = log_mel.T.cpu().numpy().astype(np.float32)

            # Extract the frames for this chunk
            # Segment frame j corresponds to global sample audio_start + j * hop_length
            # Global frame k is centered at sample k * hop_length
            segment_first_global_frame = audio_start / self.hop_length

            start_in_segment = int(round(first_frame - segment_first_global_frame))
            end_in_segment = int(round(last_frame - segment_first_global_frame + 1))

            start_in_segment = max(0, start_in_segment)
            end_in_segment = min(len(features), end_in_segment)

            return features[start_in_segment:end_in_segment]

        return await asyncio.to_thread(_process_sync)

    async def finalize(self) -> np.ndarray:
        """Process any remaining samples to get the final frames.

        :return: Final log-mel features.
        """

        def _finalize_sync() -> np.ndarray:
            if self._prev_samples is None or len(self._prev_samples) == 0:
                return np.array([], dtype=np.float32).reshape(0, self._n_mels)

            # Compute the last frame(s) that might not have been included
            # The last frame should be at (total_samples - 1) // hop_length
            # But due to center=True, we might need one more

            # Process the final buffer
            tensor = torch.from_numpy(self._prev_samples).to(self._device)
            with torch.no_grad():
                mel = self._mel_spec(tensor)
                log_mel = torch.log1p(self._log_multiplier * mel)
            features = log_mel.T.cpu().numpy().astype(np.float32)

            # We want the last frame(s) that weren't already output
            # Since we output frames up to (chunk_end - 1) // hop_length,
            # we need frames from total_samples // hop_length onwards

            total_frames = (self._total_samples + self.hop_length - 1) // self.hop_length
            last_output_frame = (self._total_samples - 1) // self.hop_length

            if total_frames > last_output_frame + 1:
                # There's an extra frame to output
                return features[-1:]
            return np.array([], dtype=np.float32).reshape(0, self._n_mels)

        return await asyncio.to_thread(_finalize_sync)

    def reset(self) -> None:
        """Reset state for processing a new audio stream."""
        self._total_samples = 0
        self._prev_samples = None
