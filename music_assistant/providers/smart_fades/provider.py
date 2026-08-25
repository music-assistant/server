"""Smart Fades audio analysis provider."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import numpy as np
import soxr
import torch
from beat_this.inference import Spect2Frames, aggregate_prediction, split_piece
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType, MediaType
from torchaudio.transforms import SpectralCentroid

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.helpers.datetime import utc
from music_assistant.helpers.util import is_arm, system_meets_requirements
from music_assistant.models.audio_analysis import AudioAnalysisData, AudioAnalysisError
from music_assistant.models.audio_analysis_provider import (
    ACCUMULATING_ANALYSIS_MAX_DURATION_SECONDS,
    AudioAnalysisProvider,
)

from .dbn_postprocessor import DBNDownBeatTracker
from .feature_extractor import AdvancedBeatFeatureExtractor
from .helpers import (
    aggregate_series_to_bins,
    calculate_overall_bpm,
    compute_band_rms_frames,
    decode_pcm_chunk_to_mono,
)
from .resources.skey_model import KEY_MAP as SKEY_KEY_MAP
from .resources.skey_model import load_skey_components
from .vocal_activity import (
    FIRERED_SAMPLE_RATE,
    FireRedFbank,
    infer_firered_chunk,
    load_firered_components,
    split_firered_features,
    vocal_activity_probabilities,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.media_items import AudioFormat
    from music_assistant_models.provider import ProviderManifest
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.mass import MusicAssistant

ANALYSIS_SAMPLE_RATE = 22050
# Below the recommended thresholds the provider still runs, but we surface an
# informational notice (see get_config_entries) as it may be tight under load.
RECOMMENDED_RAM_GB = 6.0
RECOMMENDED_CPU_CORES = 4
# Beat This predicts a long track as fixed windows. These are the values the model was trained
# and released with (30s at 50 fps, plus the loss-border frames its predictions are unreliable
# on), so a windowed prediction is identical to a whole-track one. Do not tune them: a window
# of another length puts the model off its training distribution across the whole window.
BEAT_WINDOW_FRAMES = 1500
BEAT_WINDOW_BORDER_FRAMES = 6
BEAT_WINDOW_OVERLAP_MODE = "keep_first"
# While a player streams, wait this many times a window's own compute time before starting the
# next one, so beat inference never occupies a core continuously and never holds the shared
# analysis slot for more than one window at a time.
BEAT_WINDOW_PACE_RATIO = 1.0


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
    frequency_band_chunks: dict[str, list[np.ndarray]] = field(default_factory=dict)
    musical_key_feature_blocks: list[torch.Tensor] = field(default_factory=list)
    vocal_resampler: soxr.ResampleStream | None = None
    vocal_fbank: FireRedFbank | None = None
    vocal_feature_blocks: list[np.ndarray] = field(default_factory=list)


class SmartFadesProvider(AudioAnalysisProvider):
    """Smart fades audio analysis provider using Beat This for beat tracking."""

    max_analysis_duration = ACCUMULATING_ANALYSIS_MAX_DURATION_SECONDS
    # v3: FireRed AED vocal activity
    analysis_version = 3
    has_unloadable_models = True

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

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return config entries for this provider."""
        return (
            ConfigEntry(
                key="resource_warning",
                type=ConfigEntryType.ALERT,
                required=False,
                hidden=system_meets_requirements(
                    min_memory_gb=RECOMMENDED_RAM_GB,
                    min_cpu_cores=RECOMMENDED_CPU_CORES,
                ),
            ),
        )

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider; idle models are reloaded on demand."""
        # Configure the inference runtime before loading any model (see the controller method).
        self.mass.streams.audio_analysis.ensure_inference_runtime_configured()
        await self._load_models()
        self._models_loaded = True

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
            self._clear_session_data(data)
        await super().cancel(session_id)

    async def _load_models(self) -> None:
        """Load the Beat This, S-KEY, and FireRed AED models into memory."""
        (
            self._beat_this_model,
            self._beat_this_post_processor,
            self._skey_vqt,
            self._skey_chromanet,
            self._skey_crop,
            self._spectral_centroid,
            self._firered_model,
            self._firered_cmvn_means,
            self._firered_cmvn_inverse_std,
        ) = await asyncio.to_thread(self._initialize_models)

    def _free_models(self) -> None:
        """Release the Beat This, S-KEY, and FireRed AED models."""
        self._beat_this_model = None
        self._beat_this_post_processor = None
        self._skey_vqt = None
        self._skey_chromanet = None
        self._skey_crop = None
        self._spectral_centroid = None
        self._firered_model = None
        self._firered_cmvn_means = None
        self._firered_cmvn_inverse_std = None

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
        firered_model, firered_cmvn_means, firered_cmvn_inverse_std = load_firered_components(
            device=self._device
        )
        return (
            beat_this_model,
            beat_this_post_processor,
            skey_vqt,
            skey_chromanet,
            skey_crop,
            spectral_centroid,
            firered_model,
            firered_cmvn_means,
            firered_cmvn_inverse_std,
        )

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
            vocal_resampler=soxr.ResampleStream(
                in_rate=audio_format.sample_rate,
                out_rate=FIRERED_SAMPLE_RATE,
                num_channels=1,
                dtype="float32",
            )
            if audio_format.sample_rate != FIRERED_SAMPLE_RATE
            else None,
            vocal_fbank=FireRedFbank(
                self._firered_cmvn_means,
                self._firered_cmvn_inverse_std,
            ),
        )
        self.logger.debug("Started beat tracking session %s", session_id)
        return True

    async def _finalize(self, session_id: str) -> AudioAnalysisData | None:
        """Finalize beat tracking and store results."""
        data = self._data.pop(session_id, None)
        if not data:
            return None

        try:
            if data.pcm_samples:
                await self._process_block(data, last=True)
            else:
                # The vocal resampler and fbank still need an explicit end-of-input flush.
                await self._run_offloaded(
                    self._compute_vocal_features,
                    np.empty(0, dtype=np.float32),
                    data,
                    True,
                )

            final_feats = await data.features.finalize()
            if final_feats.size:
                data.beats_feature_blocks.append(final_feats)
            if not data.beats_feature_blocks:
                return None

            feats = np.concatenate(data.beats_feature_blocks, axis=0)
            data.beats_feature_blocks.clear()
            duration = data.total_pcm_samples / ANALYSIS_SAMPLE_RATE

            all_vqt = None
            if data.musical_key_feature_blocks:
                all_vqt = torch.cat(data.musical_key_feature_blocks, dim=-1)  # (1, 1, 84, T_total)
                data.musical_key_feature_blocks.clear()

            if data.vocal_feature_blocks:
                vocal_features = np.concatenate(data.vocal_feature_blocks)
                data.vocal_feature_blocks.clear()
            else:
                vocal_features = np.empty((0, 80), dtype=np.float32)

            beat_key_result, vocal_activity = await self._run_final_inference(
                feats,
                all_vqt,
                vocal_features,
                duration,
            )
            beats, downbeats, beats_per_bar, key, mode = beat_key_result
            return self._build_analysis(
                data,
                duration,
                beats,
                downbeats,
                beats_per_bar,
                key,
                mode,
                vocal_activity,
            )
        finally:
            self._clear_session_data(data)

    def _build_analysis(
        self,
        data: SmartFadesData,
        duration: float,
        beats: np.ndarray,
        downbeats: np.ndarray,
        beats_per_bar: int,
        key: str | None,
        mode: str | None,
        vocal_activity: np.ndarray,
    ) -> AudioAnalysisData:
        """Build the final Smart Fades analysis payload."""
        bpm = calculate_overall_bpm(beats)

        # mean power per bin: point sampling aliases beat-rate ripple into the bins
        rms_energy = None
        energy_peak = 0.0
        if data.energy_chunks:
            energy_all = np.concatenate(data.energy_chunks)
            if len(energy_all) >= 2:
                rms_energy = aggregate_series_to_bins(energy_all, 1800, power=True)
                energy_peak = float(rms_energy.max())
                if energy_peak > 0:
                    rms_energy = rms_energy / energy_peak

        spectral_centroid = None
        if data.centroid_chunks:
            centroid_all = np.concatenate(data.centroid_chunks)
            if len(centroid_all) >= 2:
                spectral_centroid = aggregate_series_to_bins(centroid_all, 1800)
                # Zero out centroid where energy is negligible (noise dominates)
                if rms_energy is not None:
                    spectral_centroid[rms_energy < 0.01] = 0.0

        vocal_activity_bins = (
            aggregate_series_to_bins(vocal_activity, 1800)
            if vocal_activity.size
            else np.zeros(1800, dtype=np.float32)
        )
        extra_data: dict[str, Any] = {"vocal_activity": vocal_activity_bins.tolist()}
        if energy_peak > 0 and data.frequency_band_chunks:
            extra_data["band_rms"] = {
                name: (
                    aggregate_series_to_bins(np.concatenate(chunks), 1800, power=True) / energy_peak
                ).tolist()
                for name, chunks in data.frequency_band_chunks.items()
            }

        analysis = AudioAnalysisData(
            bpm=bpm,
            # the model stores plain float lists (numpy-free); convert the analysis arrays
            beats=beats.tolist(),
            downbeats=downbeats.tolist(),
            duration=duration,
            rms_energy=rms_energy.tolist() if rms_energy is not None else None,
            spectral_centroid=(
                spectral_centroid.tolist() if spectral_centroid is not None else None
            ),
            key=key,
            mode=mode,
            extra_data=extra_data,
            beats_per_bar=beats_per_bar or None,
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
        pcm_raw = (
            np.concatenate(data.pcm_buffer) if data.pcm_buffer else np.empty(0, dtype=np.float32)
        )
        data.pcm_buffer.clear()
        data.pcm_samples = 0

        if data.resampler is not None:
            pcm_22k = await self._run_offloaded(data.resampler.resample_chunk, pcm_raw, last)
        else:
            pcm_22k = pcm_raw

        data.total_pcm_samples += len(pcm_22k)

        if pcm_22k.size:
            feats, _, _ = await asyncio.gather(
                data.features.process_pcm(pcm_22k),
                self._run_offloaded(
                    self._compute_energy_and_spectral_centroids,
                    pcm_22k,
                    data,
                ),
                self._run_offloaded(self._compute_vocal_features, pcm_raw, data, last),
            )
        else:
            await self._run_offloaded(self._compute_vocal_features, pcm_raw, data, last)
            feats = np.empty((0, 128), dtype=np.float32)

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

            band_frames = compute_band_rms_frames(pcm_22k, sr, window_samples)
            for name, frames in band_frames.items():
                data.frequency_band_chunks.setdefault(name, []).append(frames)

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

    def _compute_vocal_features(
        self,
        pcm_raw: np.ndarray,
        data: SmartFadesData,
        last: bool,
    ) -> None:
        """Resample source PCM to 16 kHz and extract FireRed fbank features."""
        fbank = data.vocal_fbank
        resampler = data.vocal_resampler
        if fbank is None:
            return
        pcm_16k = resampler.resample_chunk(pcm_raw, last) if resampler is not None else pcm_raw
        features = fbank.process(pcm_16k)
        if features.size:
            data.vocal_feature_blocks.append(features)
        if last:
            final_features = fbank.finalize()
            if final_features.size:
                data.vocal_feature_blocks.append(final_features)

    async def _run_final_inference(
        self,
        beat_features: np.ndarray,
        key_features: torch.Tensor | None,
        vocal_features: np.ndarray,
        duration: float,
    ) -> tuple[tuple[np.ndarray, np.ndarray, int, str | None, str | None], np.ndarray]:
        """Run beat/key and vocal inference branches; the first failure cancels the other."""
        try:
            async with asyncio.TaskGroup() as task_group:
                beat_key_task = task_group.create_task(
                    self._infer_beats_and_key(beat_features, key_features)
                )
                vocal_task = task_group.create_task(
                    self._infer_vocal_activity(vocal_features, duration)
                )
        except ExceptionGroup as group:
            # Unwrap for the base class; prefer the beat/key error since it decides
            # permanent vs retryable failure recording.
            beat_key_error = None if beat_key_task.cancelled() else beat_key_task.exception()
            primary = beat_key_error or group.exceptions[0]
            for error in group.exceptions:
                if error is not primary:
                    self.logger.debug("FireRed vocal inference also failed: %s", error)
            raise primary from primary.__cause__
        return beat_key_task.result(), vocal_task.result()

    async def _infer_beats_and_key(
        self,
        beat_features: np.ndarray,
        key_features: torch.Tensor | None,
    ) -> tuple[np.ndarray, np.ndarray, int, str | None, str | None]:
        """Run beat inference followed by musical key inference."""
        beats, downbeats, beats_per_bar = await self._infer_beat_timings(beat_features)
        if len(beats) < 2:
            raise AudioAnalysisError("no rhythmic beat detected")
        key, mode = await self._run_offloaded(self._infer_musical_key, key_features)
        return beats, downbeats, beats_per_bar, key, mode

    async def _infer_vocal_activity(
        self,
        features: np.ndarray,
        duration: float,
    ) -> np.ndarray:
        """Run FireRed AED inference and return the 100 ms vocal timeline."""
        try:
            model = self._firered_model
            if model is None:
                raise RuntimeError("FireRed AED model is not loaded")
            compute_seconds = 0.0
            chunks = []
            for chunk, core_offset, core_length in split_firered_features(features):
                chunk_probabilities, elapsed = await self._run_offloaded_timed(
                    infer_firered_chunk,
                    model,
                    chunk,
                    self._device,
                )
                compute_seconds += elapsed
                chunks.append(chunk_probabilities[core_offset : core_offset + core_length])
            frame_probabilities = (
                np.concatenate(chunks) if chunks else np.empty((0, 3), dtype=np.float32)
            )
            probabilities = vocal_activity_probabilities(frame_probabilities, duration)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            # Avoid permanently suppressing beat and key results for transient model failures.
            raise AudioAnalysisError(
                f"FireRed vocal inference failed: {err}",
                retry_at=utc() + timedelta(hours=24),
            ) from err
        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "FireRed vocal inference: %.1fms compute over %d frames",
            compute_seconds * 1000,
            len(features),
        )
        return probabilities

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

    async def _infer_beat_timings(self, feats: np.ndarray) -> tuple[np.ndarray, np.ndarray, int]:
        """
        Run Beat This model inference to detect beat/downbeat timings and the meter.

        :param feats: Log-mel features for the whole track, shaped (frames, mel bins).
        """
        # Resolved once and passed down: an idle unload may clear the fields while the
        # windows below are still being dispatched.
        beat_this = self._beat_this_model
        post_processor = self._beat_this_post_processor
        assert beat_this is not None
        assert post_processor is not None

        spect = torch.from_numpy(feats).to(self._device)
        windows, starts = split_piece(
            spect,
            BEAT_WINDOW_FRAMES,
            border_size=BEAT_WINDOW_BORDER_FRAMES,
            avoid_short_end=True,
        )
        predictions = []
        model_seconds = 0.0
        for window in windows:
            prediction, elapsed = await self._run_offloaded_timed(
                self._infer_beat_window, beat_this.model, window
            )
            predictions.append(prediction)
            model_seconds += elapsed
            await self._pace_beat_windows(elapsed)

        (beats, downbeats, beats_per_bar), post_seconds = await self._run_offloaded_timed(
            self._decode_beat_timings, post_processor, predictions, starts, len(spect)
        )
        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "Model inference: %.1fms compute over %d windows, postprocessing: %.1fms, "
            "detected %d beats, %d downbeats",
            model_seconds * 1000,
            len(windows),
            post_seconds * 1000,
            len(beats),
            len(downbeats),
        )
        return beats, downbeats, beats_per_bar

    async def _pace_beat_windows(self, window_seconds: float) -> None:
        """
        Idle between beat inference windows for as long as the last one computed.

        :param window_seconds: Compute time of the window that just finished.
        """
        if not self.mass.streams.audio_analysis.playback_active():
            return
        await asyncio.sleep(window_seconds * BEAT_WINDOW_PACE_RATIO)

    def _infer_beat_window(
        self, model: torch.nn.Module, window: torch.Tensor
    ) -> dict[str, torch.Tensor]:
        """
        Run one Beat This window and return its beat and downbeat logits.

        :param model: The Beat This module to run.
        :param window: One window of log-mel features, shaped (frames, mel bins).
        """
        # inference_mode is thread-local, so it has to be entered on the worker thread.
        with torch.inference_mode():
            prediction = model(window.unsqueeze(0))
        return {"beat": prediction["beat"][0], "downbeat": prediction["downbeat"][0]}

    def _decode_beat_timings(
        self,
        post_processor: DBNDownBeatTracker,
        predictions: list[dict[str, torch.Tensor]],
        starts: np.ndarray,
        total_frames: int,
    ) -> tuple[np.ndarray, np.ndarray, int]:
        """
        Stitch per-window logits back together and decode them into beat timings.

        :param post_processor: The DBN decoder to run on the stitched activations.
        :param predictions: Per-window beat/downbeat logits, in window order.
        :param starts: Frame offset of each window, as returned by split_piece.
        :param total_frames: Frame count of the whole track.
        """
        beat_logits, downbeat_logits = aggregate_prediction(
            predictions,
            starts,
            total_frames,
            BEAT_WINDOW_FRAMES,
            BEAT_WINDOW_BORDER_FRAMES,
            BEAT_WINDOW_OVERLAP_MODE,
            self._device,
        )
        dbn_out, beats_per_bar = post_processor(
            self._beat_activations(beat_logits.float(), downbeat_logits.float())
        )
        beats = dbn_out[:, 0]
        downbeats = dbn_out[dbn_out[:, 1] == 1, 0]
        return beats, downbeats, beats_per_bar

    @staticmethod
    def _beat_activations(beat_logits: torch.Tensor, downbeat_logits: torch.Tensor) -> np.ndarray:
        """
        Convert beat/downbeat logits into the (T, 2) activations the DBN expects.

        :param beat_logits: Per-frame beat logits for the whole track.
        :param downbeat_logits: Per-frame downbeat logits for the whole track.
        """
        beat_prob = torch.sigmoid(beat_logits).cpu().numpy()
        downbeat_prob = torch.sigmoid(downbeat_logits).cpu().numpy()
        epsilon = 1e-5
        beat_prob = beat_prob * (1 - epsilon) + epsilon / 2
        downbeat_prob = downbeat_prob * (1 - epsilon) + epsilon / 2
        return np.column_stack(
            [
                np.maximum(beat_prob - downbeat_prob, epsilon / 2),
                downbeat_prob,
            ]
        )

    def _clear_session_data(self, data: SmartFadesData) -> None:
        """Release all state retained for an analysis session."""
        data.pcm_buffer.clear()
        data.beats_feature_blocks.clear()
        data.energy_chunks.clear()
        data.centroid_chunks.clear()
        data.frequency_band_chunks.clear()
        data.musical_key_feature_blocks.clear()
        data.vocal_feature_blocks.clear()
        data.resampler = None
        data.vocal_resampler = None
        data.vocal_fbank = None
