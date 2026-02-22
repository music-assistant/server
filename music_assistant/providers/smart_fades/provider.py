"""Smart Fades audio analysis provider."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np
import soxr
import torch
from beat_this.inference import Postprocessor, Spect2Frames
from music_assistant_models.enums import ContentType
from music_assistant_models.media_items import AudioFormat

from music_assistant.models.audio_analysis import AudioAnalysisData
from music_assistant.models.audio_analysis_provider import AudioAnalysisProvider

from .feature_extractor import AdvancedBeatFeatureExtractor

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.provider import ProviderManifest
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.mass import MusicAssistant

ANALYSIS_SAMPLE_RATE = 22050
ANALYSIS_AUDIO_FORMAT = AudioFormat(
    content_type=ContentType.PCM_S16LE,
    bit_depth=16,
    sample_rate=ANALYSIS_SAMPLE_RATE,
    channels=1,
)


@dataclass
class SmartFadesData:
    """Per-session data for smart fades analysis."""

    item_id: str
    provider: str
    input_audio_format: AudioFormat
    block_samples: int
    features: AdvancedBeatFeatureExtractor
    resampler: soxr.ResampleStream | None = None
    pcm_buffer: list[np.ndarray] = field(default_factory=list)
    pcm_samples: int = 0
    total_pcm_samples: int = 0
    feature_blocks: list[np.ndarray] = field(default_factory=list)


class SmartFadesProvider(AudioAnalysisProvider):
    """Smart fades audio analysis provider using Beat This for beat tracking."""

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        supported_features: set[ProviderFeature],
    ) -> None:
        """Initialize the provider."""
        super().__init__(mass, manifest, config, supported_features)
        self._data: dict[str, SmartFadesData] = {}
        self._model: Spect2Frames | None = None
        self._post: Postprocessor | None = None
        self._device = "cpu"

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._model = Spect2Frames(checkpoint_path="final0", device=self._device)
        self._post = Postprocessor(type="minimal")
        self.available = True

    async def start_analysis(
        self,
        session_id: str,
        stream_details: StreamDetails,
        audio_format: AudioFormat,
    ) -> None:
        """Start beat tracking analysis for a new track.

        :param session_id: Session ID from the AudioAnalysisController.
        :param stream_details: The stream details for the item being analyzed.
        :param audio_format: PCM format of the audio stream.
        """
        await super().start_analysis(session_id, stream_details, audio_format)
        if session_id not in self._sessions:
            return

        block_seconds = 10.0

        needs_resample = audio_format.sample_rate != ANALYSIS_SAMPLE_RATE
        self._data[session_id] = SmartFadesData(
            item_id=stream_details.item_id,
            provider=stream_details.provider,
            input_audio_format=audio_format,
            block_samples=int(block_seconds * audio_format.sample_rate),
            features=AdvancedBeatFeatureExtractor(
                sample_rate=ANALYSIS_SAMPLE_RATE,
                device=self._device,
            ),
            resampler=soxr.ResampleStream(
                in_rate=audio_format.sample_rate,
                out_rate=ANALYSIS_SAMPLE_RATE,
                num_channels=1,
                dtype="float32",
            )
            if needs_resample
            else None,
        )
        self.logger.debug("Started beat tracking session %s", session_id)

    async def process_pcm_chunk(
        self,
        session_id: str,
        pcm_chunk: bytes,
    ) -> None:
        """Process a PCM chunk for beat tracking.

        :param session_id: The analysis session ID.
        :param pcm_chunk: Raw PCM audio data.
        """
        data = self._data.get(session_id)
        if not data:
            return

        pcm_mono = await asyncio.to_thread(self._decode_chunk_sync, data, pcm_chunk)
        if pcm_mono.size == 0:
            return

        data.pcm_buffer.append(pcm_mono)
        data.pcm_samples += len(pcm_mono)

        if data.pcm_samples >= data.block_samples:
            await self._process_block(data)

    async def finalize(self, session_id: str) -> None:
        """Finalize beat tracking and store results.

        :param session_id: The analysis session ID.
        """
        data = self._data.pop(session_id, None)
        if not data:
            return

        # Flush remaining buffered PCM
        if data.pcm_samples:
            await self._process_block(data, last=True)

        # Get final features with end padding
        final_feats = await data.features.finalize()
        if final_feats.size:
            data.feature_blocks.append(final_feats)

        if not data.feature_blocks:
            return

        feats = np.concatenate(data.feature_blocks, axis=0)

        self.logger.debug(
            "Running model inference on %d frames (%.1fs of audio)",
            feats.shape[0],
            feats.shape[0] * 0.02,
        )

        beats, downbeats = await asyncio.to_thread(self._run_inference_sync, feats)
        duration = data.total_pcm_samples / ANALYSIS_SAMPLE_RATE

        if len(beats) < 2:
            self.logger.debug("Not enough beats detected, skipping storage")
            return

        beat_intervals = np.diff(beats)
        bpm = float(60.0 / np.mean(beat_intervals))

        analysis = AudioAnalysisData(
            bpm=bpm,
            beats=beats,
            downbeats=downbeats,
            duration=duration,
        )

        await self.mass.music.set_audio_analysis(
            data.item_id,
            data.provider,
            self.domain,
            analysis,
            analysis_version=self.analysis_version,
        )

        self.logger.info(
            "Stored beat analysis for %s: BPM=%.1f, %d beats, %d downbeats",
            data.item_id,
            bpm,
            len(beats),
            len(downbeats),
        )

    async def cancel(self, session_id: str) -> None:
        """Cancel a beat tracking session.

        :param session_id: The analysis session ID.
        """
        data = self._data.pop(session_id, None)
        if data:
            data.pcm_buffer.clear()
            data.feature_blocks.clear()
            data.features.reset()

    @staticmethod
    def _decode_chunk_sync(data: SmartFadesData, pcm_chunk: bytes) -> np.ndarray:
        """Decode a PCM chunk to mono float32 numpy array. Runs synchronously."""
        content_type = data.input_audio_format.content_type

        if content_type == ContentType.PCM_F32LE:
            audio = torch.frombuffer(pcm_chunk, dtype=torch.float32).clone()
        elif content_type == ContentType.PCM_F64LE:
            audio = torch.frombuffer(pcm_chunk, dtype=torch.float64).clone().to(torch.float32)
        elif content_type == ContentType.PCM_S32LE:
            audio = (
                torch.frombuffer(pcm_chunk, dtype=torch.int32).clone().to(torch.float32)
                / 2147483648.0
            )
        else:
            audio = (
                torch.frombuffer(pcm_chunk, dtype=torch.int16).clone().to(torch.float32) / 32768.0
            )

        if data.input_audio_format.channels == 2:
            audio = audio.reshape(-1, 2).mean(dim=1)

        return audio.numpy()

    async def _process_block(self, data: SmartFadesData, *, last: bool = False) -> None:
        """Resample accumulated PCM buffer and extract features.

        Uses a stateful soxr resampler to eliminate block-boundary artifacts
        from per-block stateless resampling.

        :param data: Session data.
        :param last: Set True for the final block to flush the resampler.
        """
        pcm_raw = np.concatenate(data.pcm_buffer)
        data.pcm_buffer.clear()
        data.pcm_samples = 0

        if data.resampler is not None:
            pcm_22k = await asyncio.to_thread(data.resampler.resample_chunk, pcm_raw, last)
        else:
            pcm_22k = pcm_raw

        data.total_pcm_samples += len(pcm_22k)

        start_time = time.perf_counter()
        feats = await data.features.process_pcm(pcm_22k)
        elapsed_ms = (time.perf_counter() - start_time) * 1000

        if feats.size:
            data.feature_blocks.append(feats)
        self.logger.debug("Processed 10s of PCM chunks in %.1fms", elapsed_ms)

    def _run_inference_sync(self, feats: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Run model inference synchronously. Called from thread pool."""
        assert self._model is not None
        assert self._post is not None

        tensor = torch.from_numpy(feats).to(self._device)

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

        return beats, downbeats
