"""Smart Fades audio analysis provider."""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np
import soxr
import torch
import torchaudio
from beat_this.inference import Postprocessor, Spect2Frames

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.models.audio_analysis import AudioAnalysisData
from music_assistant.models.audio_analysis_provider import AudioAnalysisProvider

from .feature_extractor import AdvancedBeatFeatureExtractor
from .helpers import calculate_overall_bpm, decode_pcm_chunk
from .resources.skey_model import KEY_MAP as SKEY_KEY_MAP
from .resources.skey_model import load_skey_components

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.media_items import AudioFormat
    from music_assistant_models.provider import ProviderManifest
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.mass import MusicAssistant

ANALYSIS_SAMPLE_RATE = 22050


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
    beats_feature_blocks: list[np.ndarray] = field(default_factory=list)
    energy_chunks: list[np.ndarray] = field(default_factory=list)
    centroid_chunks: list[np.ndarray] = field(default_factory=list)
    musical_key_feature_blocks: list[torch.Tensor] = field(default_factory=list)


class SmartFadesProvider(AudioAnalysisProvider):
    """Smart fades audio analysis provider using Beat This for beat tracking."""

    # --- public methods ---

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
        self._device = "cpu"

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        torch.set_num_threads(os.cpu_count() or 4)
        torch.backends.quantized.engine = "qnnpack"
        self._beat_this_model = Spect2Frames(checkpoint_path="small0", device=self._device)
        self._beat_this_model.model = torch.ao.quantization.quantize_dynamic(  # type: ignore[no-untyped-call]
            self._beat_this_model.model, {torch.nn.Linear}, dtype=torch.qint8
        )
        self._beat_this_post_processor = Postprocessor(type="minimal")
        self._skey_vqt, self._skey_chromanet, self._skey_crop = load_skey_components(
            device=self._device
        )
        self._spectral_centroid = torchaudio.transforms.SpectralCentroid(
            sample_rate=ANALYSIS_SAMPLE_RATE, hop_length=512
        )

    async def process_pcm_chunk(
        self,
        session_id: str,
        pcm_chunk: bytes,
    ) -> None:
        """Process a PCM chunk for beat tracking."""
        data = self._data.get(session_id)
        if not data:
            return

        pcm_mono = await asyncio.to_thread(decode_pcm_chunk, data.input_audio_format, pcm_chunk)
        if pcm_mono.size == 0:
            return

        # Per-chunk VQT for key detection (skip short tail chunks)
        if len(pcm_mono) >= data.input_audio_format.sample_rate:
            if data.input_audio_format.sample_rate != ANALYSIS_SAMPLE_RATE:
                chunk_22k = soxr.resample(
                    pcm_mono, data.input_audio_format.sample_rate, ANALYSIS_SAMPLE_RATE
                )
            else:
                chunk_22k = pcm_mono
            await asyncio.to_thread(self._compute_musical_key_features, chunk_22k, data)

        data.pcm_buffer.append(pcm_mono)
        data.pcm_samples += len(pcm_mono)

        # calculate features in 10s blocks to avoid cpu contention
        if data.pcm_samples >= data.block_samples:
            await self._process_block(data)

    async def cancel(self, session_id: str) -> None:
        """Cancel a beat tracking session.

        :param session_id: The analysis session ID.
        """
        data = self._data.pop(session_id, None)
        if data:
            data.pcm_buffer.clear()
            data.beats_feature_blocks.clear()
            data.musical_key_feature_blocks.clear()
            data.features.reset()
        await super().cancel(session_id)

    # --- private methods ---

    async def _start_analysis(
        self,
        session_id: str,
        streamdetails: StreamDetails,
        audio_format: AudioFormat,
    ) -> bool:
        """Start beat tracking analysis for a new track."""
        block_seconds = 10.0

        needs_resample = audio_format.sample_rate != ANALYSIS_SAMPLE_RATE
        self._data[session_id] = SmartFadesData(
            item_id=streamdetails.item_id,
            provider=streamdetails.provider,
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
        return True

    async def _finalize(self, session_id: str) -> None:
        """Finalize beat tracking and store results."""
        data = self._data.pop(session_id, None)
        if not data:
            return

        # Flush remaining buffered PCM
        if data.pcm_samples:
            await self._process_block(data, last=True)

        # Get final features with end padding
        final_feats = await data.features.finalize()
        if final_feats.size:
            data.beats_feature_blocks.append(final_feats)

        if not data.beats_feature_blocks:
            return

        feats = np.concatenate(data.beats_feature_blocks, axis=0)
        duration = data.total_pcm_samples / ANALYSIS_SAMPLE_RATE

        # Prepare VQT features for key detection
        all_vqt = None
        if data.musical_key_feature_blocks:
            all_vqt = torch.cat(data.musical_key_feature_blocks, dim=-1)  # (1, 1, 84, T_total)
            data.musical_key_feature_blocks.clear()

        # Run beat and key inference concurrently in separate threads
        beat_task = asyncio.to_thread(self._infer_beat_timings, feats)
        key_task = asyncio.to_thread(self._infer_musical_key, all_vqt)
        (beats, downbeats), (key, mode) = await asyncio.gather(beat_task, key_task)

        if len(beats) < 2:
            self.logger.debug("Not enough beats detected, skipping storage")
            return

        bpm = calculate_overall_bpm(beats)

        # Build extended analysis fields
        rms_energy_per_second = None
        if data.energy_chunks:
            rms_energy_per_second = np.concatenate(data.energy_chunks)
            peak = rms_energy_per_second.max()
            if peak > 0:
                rms_energy_per_second = rms_energy_per_second / peak

        spectral_centroid_per_second = None
        if data.centroid_chunks:
            spectral_centroid_per_second = np.concatenate(data.centroid_chunks)

        analysis = AudioAnalysisData(
            bpm=bpm,
            beats=beats,
            downbeats=downbeats,
            duration=duration,
            rms_energy_per_second=rms_energy_per_second,
            spectral_centroid_per_second=spectral_centroid_per_second,
            key=key,
            mode=mode,
        )

        await self.mass.streams.audio_analysis.set_audio_analysis(
            data.item_id,
            data.provider,
            self.domain,
            analysis,
            analysis_version=self.analysis_version,
        )

        self.logger.debug(
            "Stored beat analysis for %s: BPM=%.1f, %d beats, %d downbeats, key=%s",
            data.item_id,
            bpm,
            len(beats),
            len(downbeats),
            f"{key} {mode}" if key else "unknown",
        )

    async def _process_block(self, data: SmartFadesData, *, last: bool = False) -> None:
        """Resample accumulated PCM buffer and extract features."""
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
            data.beats_feature_blocks.append(feats)

        await asyncio.to_thread(self._compute_energy_and_spectral_centroids, pcm_22k, data)
        self.logger.log(VERBOSE_LOG_LEVEL, "Processed 10s of PCM chunks in %.1fms", elapsed_ms)

    def _compute_energy_and_spectral_centroids(
        self, pcm_22k: np.ndarray, data: SmartFadesData
    ) -> None:
        """Compute RMS energy and spectral centroid per second for a 10s block."""
        sr = ANALYSIS_SAMPLE_RATE
        n_full_seconds = len(pcm_22k) // sr
        if n_full_seconds > 0:
            frames = pcm_22k[: n_full_seconds * sr].reshape(n_full_seconds, sr)
            rms = np.sqrt(np.mean(frames**2, axis=1)).astype(np.float32)
            data.energy_chunks.append(rms)

        pcm_tensor = torch.from_numpy(pcm_22k)
        centroid_frames = self._spectral_centroid(pcm_tensor.unsqueeze(0)).squeeze(0).numpy()
        hop_length = 512
        frames_per_sec = sr // hop_length
        if frames_per_sec > 0:
            n_secs = len(centroid_frames) // frames_per_sec
            if n_secs > 0:
                trimmed = centroid_frames[: n_secs * frames_per_sec]
                centroid_per_sec = (
                    trimmed.reshape(n_secs, frames_per_sec).mean(axis=1).astype(np.float32)
                )
                data.centroid_chunks.append(centroid_per_sec)

    def _compute_musical_key_features(self, pcm_22k: np.ndarray, data: SmartFadesData) -> None:
        """Extract VQT features for S-KEY key detection."""
        pcm_tensor = torch.from_numpy(pcm_22k)
        start = time.perf_counter()
        with torch.no_grad():
            vqt_input = pcm_tensor.unsqueeze(0).unsqueeze(0)  # (1, 1, samples)
            vqt_out = self._skey_vqt(vqt_input)  # (1, 1, n_bins, T)
            cropped = self._skey_crop(vqt_out, torch.zeros(1))  # (1, 1, 84, T)
            data.musical_key_feature_blocks.append(cropped.cpu())
        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "VQT feature extraction: %.1fms (%d frames)",
            (time.perf_counter() - start) * 1000,
            cropped.shape[-1],
        )

    def _infer_musical_key(
        self, vqt_features: torch.Tensor | None
    ) -> tuple[str | None, str | None]:
        """Run S-KEY ChromaNet inference to detect musical key."""
        if vqt_features is None or vqt_features.shape[-1] < 128:
            return None, None
        start = time.perf_counter()
        with torch.no_grad():
            logits = self._skey_chromanet(vqt_features.to(self._device))
            key_idx = int(logits.argmax(dim=-1).item())
            key_name = SKEY_KEY_MAP[key_idx]  # e.g. "C# Major"
            parts = key_name.split()
        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "ChromaNet key inference: %.1fms, detected key=%s %s",
            (time.perf_counter() - start) * 1000,
            parts[0],
            parts[1],
        )
        return parts[0], parts[1].lower()

    def _infer_beat_timings(self, feats: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Run Beat This model inference to detect beat and downbeat timings."""
        assert self._beat_this_model is not None
        assert self._beat_this_post_processor is not None

        tensor = torch.from_numpy(feats).to(self._device)

        inference_start = time.perf_counter()
        with torch.inference_mode():
            beat_logits, downbeat_logits = self._beat_this_model(tensor)
            model_elapsed = (time.perf_counter() - inference_start) * 1000

            post_start = time.perf_counter()
            beats, downbeats = self._beat_this_post_processor(beat_logits, downbeat_logits)
            post_elapsed = (time.perf_counter() - post_start) * 1000

        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "Model inference: %.1fms, postprocessing: %.1fms, detected %d beats, %d downbeats",
            model_elapsed,
            post_elapsed,
            len(beats),
            len(downbeats),
        )

        return beats, downbeats
