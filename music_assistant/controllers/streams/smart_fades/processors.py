# processors/base.py

import logging
from typing import Any

import numpy as np
import torch
from beat_this.inference import Postprocessor, Spect2Frames
from music_assistant_models.enums import ContentType
from music_assistant_models.media_items import AudioFormat

from music_assistant.controllers.streams.smart_fades.feature_extractors import (
    AdvancedBeatFeatureExtractor,
)
from music_assistant.helpers.audio import resample_pcm_audio

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
        self._feature_blocks: list[np.ndarray] = []

        self._features = AdvancedBeatFeatureExtractor(
            sample_rate=BEAT_THIS_ANALYSIS_AUDIO_FORMAT.sample_rate,
            device=device,
        )
        self._model = Spect2Frames(checkpoint_path=model_name, device=device)
        self._post = Postprocessor(type="minimal")
        self._device = device

        self.logger = logging.getLogger(__name__)

    async def process_pcm_chunk(self, pcm_chunk: bytes) -> None:
        # 1. resample immediately
        pcm = await resample_pcm_audio(
            pcm_chunk, self.input_audio_format, BEAT_THIS_ANALYSIS_AUDIO_FORMAT
        )

        # Convert S16LE to float32 in [-1, 1] range
        pcm_22k = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0

        if pcm_22k.size == 0:
            return

        # 2. buffer resampled PCM
        self._pcm_buffer.append(pcm_22k)
        self._pcm_samples += len(pcm_22k)

        if self._pcm_samples >= self.block_samples:
            await self._process_block()

    async def _process_block(self) -> None:
        self.logger.debug("Processing 10s of pcm chunks.")
        pcm = np.concatenate(self._pcm_buffer)
        self._pcm_buffer.clear()
        self._pcm_samples = 0

        feats = await self._features.process_pcm(pcm)
        if feats.size:
            self._feature_blocks.append(feats)

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

        # 4. Single-shot model inference
        with torch.no_grad():
            beat_logits, downbeat_logits = self._model(tensor)
            beats, downbeats = self._post(beat_logits, downbeat_logits)

        # 5. Free memory and reset state
        await self.reset()

        return {
            "beats": beats,
            "downbeats": downbeats,
        }

    async def reset(self) -> None:
        self._pcm_buffer.clear()
        self._feature_blocks.clear()
        self._pcm_samples = 0
        self._features.reset()
