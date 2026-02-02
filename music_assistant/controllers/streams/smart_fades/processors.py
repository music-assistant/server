# processors/base.py

import asyncio
import logging
import time
from typing import Any

import numpy as np
import torch
import torchaudio
from beat_this.inference import Postprocessor, Spect2Frames
from music_assistant_models.enums import ContentType
from music_assistant_models.media_items import AudioFormat

from music_assistant.controllers.streams.smart_fades.feature_extractors import (
    AdvancedBeatFeatureExtractor,
)

BEAT_THIS_ANALYSIS_AUDIO_FORMAT = AudioFormat(
    content_type=ContentType.PCM_S16LE,
    bit_depth=16,
    sample_rate=22050,
    channels=1,
)


class StreamingAnalyzerProcessor:
    async def process_pcm_chunk(self, pcm_chunk: bytes) -> None:
        raise NotImplementedError

    async def finalize(self) -> dict[str, Any]:
        raise NotImplementedError

    async def reset(self) -> None:
        raise NotImplementedError


class BeatThisStreamingProcessor(StreamingAnalyzerProcessor):
    """
    Beat This (final0) streaming analyzer.
    """

    def __init__(
        self,
        audio_format: AudioFormat,
        model_name: str = "final0",
        device: str = "cpu",
        block_seconds: float = 10.0,
    ):
        self.input_audio_format = audio_format
        self.block_samples = int(block_seconds * BEAT_THIS_ANALYSIS_AUDIO_FORMAT.sample_rate)

        self._pcm_buffer: list[np.ndarray] = []
        self._pcm_samples = 0
        self._total_pcm_samples = 0
        self._feature_blocks: list[np.ndarray] = []

        self._features = AdvancedBeatFeatureExtractor(
            sample_rate=BEAT_THIS_ANALYSIS_AUDIO_FORMAT.sample_rate,
            device=device,
        )
        self._model = Spect2Frames(checkpoint_path=model_name, device=device)
        self._post = Postprocessor(type="minimal")
        self._device = device

        # Create resampler for converting input audio to analysis format (22050Hz mono)
        # This avoids spawning ffmpeg subprocesses for each chunk
        self._resampler: torchaudio.transforms.Resample | None = None
        if audio_format.sample_rate != BEAT_THIS_ANALYSIS_AUDIO_FORMAT.sample_rate:
            self._resampler = torchaudio.transforms.Resample(
                orig_freq=audio_format.sample_rate,
                new_freq=BEAT_THIS_ANALYSIS_AUDIO_FORMAT.sample_rate,
            ).to(device)

        self.logger = logging.getLogger(__name__)

    def _resample_chunk_sync(self, pcm_chunk: bytes) -> np.ndarray:
        """Resample a PCM chunk to analysis format (22050Hz mono). Runs synchronously."""
        content_type = self.input_audio_format.content_type

        # Parse PCM bytes based on content type (clone to avoid non-writable buffer warning)
        if content_type == ContentType.PCM_F32LE:
            audio = torch.frombuffer(pcm_chunk, dtype=torch.float32).clone()
        elif content_type == ContentType.PCM_F64LE:
            audio = torch.frombuffer(pcm_chunk, dtype=torch.float64).clone().to(torch.float32)
        elif content_type == ContentType.PCM_S32LE:
            audio = torch.frombuffer(pcm_chunk, dtype=torch.int32).clone().to(torch.float32) / 2147483648.0
        else:
            # Default: PCM_S16LE (most common)
            audio = torch.frombuffer(pcm_chunk, dtype=torch.int16).clone().to(torch.float32) / 32768.0

        # Handle stereo to mono conversion
        if self.input_audio_format.channels == 2:
            audio = audio.reshape(-1, 2).mean(dim=1)

        # Resample if needed
        if self._resampler is not None:
            audio = audio.to(self._device)
            with torch.no_grad():
                audio = self._resampler(audio)
            audio = audio.cpu()

        return audio.numpy()

    async def process_pcm_chunk(self, pcm_chunk: bytes) -> None:
        # Resample to 22050Hz mono in thread pool (avoids blocking event loop)
        pcm_22k = await asyncio.to_thread(self._resample_chunk_sync, pcm_chunk)

        if pcm_22k.size == 0:
            return

        # 2. buffer resampled PCM
        self._pcm_buffer.append(pcm_22k)
        self._pcm_samples += len(pcm_22k)
        self._total_pcm_samples += len(pcm_22k)

        if self._pcm_samples >= self.block_samples:
            await self._process_block()

    async def _process_block(self) -> None:
        pcm = np.concatenate(self._pcm_buffer)
        self._pcm_buffer.clear()
        self._pcm_samples = 0

        start_time = time.perf_counter()
        feats = await self._features.process_pcm(pcm)
        elapsed_ms = (time.perf_counter() - start_time) * 1000

        if feats.size:
            self._feature_blocks.append(feats)
        self.logger.debug("Processed 10s of PCM chunks in %.1fms", elapsed_ms)

    async def finalize(self) -> dict[str, Any]:
        # 1. Process remaining buffered PCM
        if self._pcm_samples:
            await self._process_block()

        # 2. Get final features with end padding (for center=True equivalence)
        final_feats = await self._features.finalize()
        if final_feats.size:
            self._feature_blocks.append(final_feats)

        if not self._feature_blocks:
            return {}

        # 3. Concatenate features
        # NOTE: No per-track normalization - beat_this doesn't normalize features
        feats = np.concatenate(self._feature_blocks, axis=0)

        tensor = torch.from_numpy(feats).to(self._device)
        self.logger.debug(
            "Running model inference on %d frames (%.1fs of audio)",
            feats.shape[0],
            feats.shape[0] * 0.02,  # 20ms per frame (hop_length=441 at 22050Hz)
        )

        # 4. Single-shot model inference
        inference_start = time.perf_counter()
        with torch.no_grad():
            beat_logits, downbeat_logits = self._model(tensor)
            model_elapsed = (time.perf_counter() - inference_start) * 1000

            post_start = time.perf_counter()
            beats, downbeats = self._post(beat_logits, downbeat_logits)
            post_elapsed = (time.perf_counter() - post_start) * 1000

        self.logger.debug(
            "Model inference: %.1fms, postprocessing: %.1fms, detected %d beats, %d downbeats",
            model_elapsed,
            post_elapsed,
            len(beats),
            len(downbeats),
        )

        # 5. Calculate duration from total samples processed
        duration = self._total_pcm_samples / BEAT_THIS_ANALYSIS_AUDIO_FORMAT.sample_rate

        # 6. Free memory and reset state
        await self.reset()

        return {
            "beats": beats,
            "downbeats": downbeats,
            "duration": duration,
        }

    async def reset(self) -> None:
        self._pcm_buffer.clear()
        self._feature_blocks.clear()
        self._pcm_samples = 0
        self._total_pcm_samples = 0
        self._features.reset()
