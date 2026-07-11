"""Smart Fades audio analysis provider."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np
import soxr
import torch
from beat_this.inference import Spect2Frames
from music_assistant_models.enums import MediaType
from torchaudio.transforms import SpectralCentroid

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.helpers.util import is_arm
from music_assistant.models.audio_analysis import AudioAnalysisData
from music_assistant.models.audio_analysis_provider import (
    ACCUMULATING_ANALYSIS_MAX_DURATION_SECONDS,
    AudioAnalysisProvider,
)

from .dbn_postprocessor import DBNDownBeatTracker
from .feature_extractor import AdvancedBeatFeatureExtractor
from .helpers import calculate_overall_bpm, decode_pcm_chunk_to_mono
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

    max_analysis_duration = ACCUMULATING_ANALYSIS_MAX_DURATION_SECONDS

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
        # Configure the inference runtime before loading any model (see the controller method).
        self.mass.streams.audio_analysis.ensure_inference_runtime_configured()
        (
            self._beat_this_model,
            self._beat_this_post_processor,
            self._skey_vqt,
            self._skey_chromanet,
            self._skey_crop,
            self._spectral_centroid,
        ) = await asyncio.to_thread(self._initialize_models)

    def _initialize_models(self) -> tuple[Any, ...]:
        """Initialize ML models (runs in a thread to avoid blocking the event loop)."""
        beat_this_model = Spect2Frames(checkpoint_path="small0", device=self._device)
        # torch aarch64 wheels advertise fbgemm in supported_engines but its kernels are x86-only.
        preference = ("qnnpack", "fbgemm") if is_arm() else ("fbgemm", "qnnpack")
        supported_engines = torch.backends.quantized.supported_engines
        quantized_engine = next((e for e in preference if e in supported_engines), None)
        if quantized_engine is not None and torch.backends.quantized.engine != quantized_engine:
            torch.backends.quantized.engine = quantized_engine
        beat_this_model.model = torch.ao.quantization.quantize_dynamic(  # type: ignore[no-untyped-call]
            beat_this_model.model, {torch.nn.Linear}, dtype=torch.qint8
        )
        beat_this_post_processor = DBNDownBeatTracker(
            beats_per_bar=[3, 4], min_bpm=55, max_bpm=215, fps=50
        )
        skey_vqt, skey_chromanet, skey_crop = load_skey_components(device=self._device)
        spectral_centroid = SpectralCentroid(sample_rate=ANALYSIS_SAMPLE_RATE, hop_length=512)
        return (
            beat_this_model,
            beat_this_post_processor,
            skey_vqt,
            skey_chromanet,
            skey_crop,
            spectral_centroid,
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

        pcm_mono = await self._run_offloaded(
            decode_pcm_chunk_to_mono, data.input_audio_format, pcm_chunk
        )
        if pcm_mono.size == 0:
            return

        # Per-chunk VQT for key detection (skip short tail chunks)
        if len(pcm_mono) >= data.input_audio_format.sample_rate:
            await self._run_offloaded(
                self._compute_musical_key_features,
                pcm_mono,
                data.input_audio_format.sample_rate,
                data,
            )

        data.pcm_buffer.append(pcm_mono)
        data.pcm_samples += len(pcm_mono)

        # calculate features in 10s blocks to avoid cpu contention
        if data.pcm_samples >= data.block_samples:
            await self._process_block(data)

    async def cancel(self, session_id: str) -> None:
        """Cancel a beat tracking session."""
        data = self._data.pop(session_id, None)
        if data:
            data.pcm_buffer.clear()
            data.beats_feature_blocks.clear()
            data.musical_key_feature_blocks.clear()
            data.features.reset()
        await super().cancel(session_id)

    async def _start_analysis(
        self,
        session_id: str,
        streamdetails: StreamDetails,
        audio_format: AudioFormat,
    ) -> bool:
        """Start beat tracking analysis for a new track."""
        if streamdetails.media_type != MediaType.TRACK:
            # We only want to analyze tracks
            return False

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
                offload=self._run_offloaded,
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

    async def _finalize(self, session_id: str) -> AudioAnalysisData | None:
        """Finalize beat tracking and store results."""
        data = self._data.pop(session_id, None)
        if not data:
            return None

        # Flush remaining buffered PCM
        if data.pcm_samples:
            await self._process_block(data, last=True)

        # Get final features with end padding
        final_feats = await data.features.finalize()
        if final_feats.size:
            data.beats_feature_blocks.append(final_feats)

        if not data.beats_feature_blocks:
            return None

        feats = np.concatenate(data.beats_feature_blocks, axis=0)
        duration = data.total_pcm_samples / ANALYSIS_SAMPLE_RATE

        # Prepare VQT features for key detection
        all_vqt = None
        if data.musical_key_feature_blocks:
            all_vqt = torch.cat(data.musical_key_feature_blocks, dim=-1)  # (1, 1, 84, T_total)
            data.musical_key_feature_blocks.clear()

        # Run beat and key inference sequentially to keep peak CPU bounded.
        beats, downbeats = await self._run_offloaded(self._infer_beat_timings, feats)
        if len(beats) < 2:
            self.logger.debug("Not enough beats detected, skipping storage")
            return None
        key, mode = await self._run_offloaded(self._infer_musical_key, all_vqt)

        bpm = calculate_overall_bpm(beats)

        # Interpolate energy and centroid to 1800 fixed bins
        rms_energy = None
        if data.energy_chunks:
            energy_all = np.concatenate(data.energy_chunks)
            if len(energy_all) >= 2:
                src_x = np.linspace(0, 1, len(energy_all))
                dst_x = np.linspace(0, 1, 1800)
                rms_energy = np.interp(dst_x, src_x, energy_all).astype(np.float32)
                peak = rms_energy.max()
                if peak > 0:
                    rms_energy = rms_energy / peak

        spectral_centroid = None
        if data.centroid_chunks:
            centroid_all = np.concatenate(data.centroid_chunks)
            if len(centroid_all) >= 2:
                src_x = np.linspace(0, 1, len(centroid_all))
                dst_x = np.linspace(0, 1, 1800)
                spectral_centroid = np.interp(dst_x, src_x, centroid_all).astype(np.float32)
                # Zero out centroid where energy is negligible (noise dominates)
                if rms_energy is not None:
                    spectral_centroid[rms_energy < 0.01] = 0.0

        analysis = AudioAnalysisData(
            bpm=bpm,
            beats=beats,
            downbeats=downbeats,
            duration=duration,
            rms_energy=rms_energy,
            spectral_centroid=spectral_centroid,
            key=key,
            mode=mode,
        )

        self.logger.debug(
            "Beat analysis for %s: BPM=%.1f, %d beats, %d downbeats, key=%s",
            data.item_id,
            bpm,
            len(beats),
            len(downbeats),
            f"{key} {mode}" if key else "unknown",
        )
        return analysis

    async def _process_block(self, data: SmartFadesData, *, last: bool = False) -> None:
        """Resample accumulated PCM buffer and extract features."""
        start_time = time.perf_counter()
        pcm_raw = np.concatenate(data.pcm_buffer)
        data.pcm_buffer.clear()
        data.pcm_samples = 0

        if data.resampler is not None:
            pcm_22k = await self._run_offloaded(data.resampler.resample_chunk, pcm_raw, last)
        else:
            pcm_22k = pcm_raw

        data.total_pcm_samples += len(pcm_22k)

        feats, _ = await asyncio.gather(
            data.features.process_pcm(pcm_22k),
            self._run_offloaded(self._compute_energy_and_spectral_centroids, pcm_22k, data),
        )

        if feats.size:
            data.beats_feature_blocks.append(feats)

        elapsed_ms = (time.perf_counter() - start_time) * 1000
        self.logger.log(VERBOSE_LOG_LEVEL, "Processed 10s of PCM chunks in %.1fms", elapsed_ms)

    def _compute_energy_and_spectral_centroids(
        self, pcm_22k: np.ndarray, data: SmartFadesData
    ) -> None:
        """Compute fine-resolution RMS energy and spectral centroid for a block."""
        sr = ANALYSIS_SAMPLE_RATE
        # RMS energy in 100ms windows, including partial final window
        window_samples = sr // 10  # 2205 samples = 100ms
        if len(pcm_22k) > 0:
            n_full = len(pcm_22k) // window_samples
            rms_list = []
            if n_full > 0:
                frames = pcm_22k[: n_full * window_samples].reshape(n_full, window_samples)
                rms_list.append(np.sqrt(np.mean(frames**2, axis=1)))
            remainder = len(pcm_22k) - n_full * window_samples
            if remainder > 0:
                tail = pcm_22k[n_full * window_samples :]
                rms_list.append(np.array([np.sqrt(np.mean(tail**2))]))
            if rms_list:
                data.energy_chunks.append(np.concatenate(rms_list).astype(np.float32))

        # Spectral centroid: keep per-frame (hop_length=512, ~43 frames/s)
        # Skip short tail buffers: STFT reflect-pad requires len > n_fft // 2.
        if len(pcm_22k) >= self._spectral_centroid.n_fft:
            pcm_tensor = torch.from_numpy(pcm_22k)
            centroid_frames = self._spectral_centroid(pcm_tensor.unsqueeze(0)).squeeze(0).numpy()
            # digitally-silent frames divide 0/0 into NaN; treat them as 0 Hz like
            # other negligible-energy frames so no non-finite value is ever stored
            np.nan_to_num(centroid_frames, copy=False, nan=0.0, posinf=0.0, neginf=0.0)
            if len(centroid_frames) > 0:
                data.centroid_chunks.append(centroid_frames.astype(np.float32))

    def _compute_musical_key_features(
        self, pcm_mono: np.ndarray, sample_rate: int, data: SmartFadesData
    ) -> None:
        """Extract VQT features for S-KEY key detection."""
        if sample_rate != ANALYSIS_SAMPLE_RATE:
            pcm_mono = soxr.resample(pcm_mono, sample_rate, ANALYSIS_SAMPLE_RATE)
        pcm_tensor = torch.from_numpy(pcm_mono)
        with torch.inference_mode():
            vqt_input = pcm_tensor.unsqueeze(0).unsqueeze(0)  # (1, 1, samples)
            vqt_out = self._skey_vqt(vqt_input)  # (1, 1, n_bins, T)
            cropped = self._skey_crop(vqt_out, torch.zeros(1))  # (1, 1, 84, T)
            data.musical_key_feature_blocks.append(cropped.cpu())

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

        # Prepare activations for DBN: sigmoid + clamp + combine
        post_start = time.perf_counter()
        beat_prob = torch.sigmoid(beat_logits).cpu().numpy()
        downbeat_prob = torch.sigmoid(downbeat_logits).cpu().numpy()
        epsilon = 1e-5
        beat_prob = beat_prob * (1 - epsilon) + epsilon / 2
        downbeat_prob = downbeat_prob * (1 - epsilon) + epsilon / 2
        combined_act = np.column_stack(
            [
                np.maximum(beat_prob - downbeat_prob, epsilon / 2),
                downbeat_prob,
            ]
        )

        dbn_out = self._beat_this_post_processor(combined_act)
        post_elapsed = (time.perf_counter() - post_start) * 1000

        beats = dbn_out[:, 0]
        downbeats = dbn_out[dbn_out[:, 1] == 1, 0]

        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "Model inference: %.1fms, postprocessing: %.1fms, detected %d beats, %d downbeats",
            model_elapsed,
            post_elapsed,
            len(beats),
            len(downbeats),
        )

        return beats, downbeats
