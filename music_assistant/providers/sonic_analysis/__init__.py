"""On-device audio analysis: librosa scalars + Microsoft CLAP zero-shot, one audio load per track."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import numpy as np
import soxr
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, ContentType
from music_assistant_models.errors import SetupFailedError

from music_assistant.helpers.datetime import utc
from music_assistant.helpers.util import (
    join_task,
    system_meets_requirements,
    verify_system_meets_requirements,
)
from music_assistant.models.audio_analysis import AudioAnalysisData, AudioAnalysisError
from music_assistant.models.audio_analysis_provider import (
    ACCUMULATING_ANALYSIS_MAX_DURATION_SECONDS,
    AnalysisSessionData,
    AudioAnalysisProvider,
)

from .clap_prompts import (
    PRECOMPUTED_EMBEDDINGS_PATH,
    SCALAR_PROMPT_PAIRS,
    hash_scalar_prompt_pairs,
    load_precomputed_prompt_embeddings,
    score_scalars,
)
from .helpers import (
    BlockFeatures,
    collapse_to_analysis,
    extract_block_features,
    merge_block_features,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.media_items import AudioFormat
    from music_assistant_models.provider import ProviderManifest
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

BLOCK_SECONDS: int = 10
# Must equal helpers._CHROMA_SR — filterbanks in helpers.py are baked at this rate.
ANALYSIS_SAMPLE_RATE: int = 22050
OVERLAP_SAMPLES: int = 2048

EXTRA_DATA_CLAP_EMBEDDING: str = "clap_embedding"

# CLAP's HTSAT audio encoder takes a fixed 7-second input at 44.1 kHz.
CLAP_WINDOW_SECONDS: int = 7
CLAP_SKIP_SECONDS: int = 45
CLAP_MIN_WINDOW_SECONDS: float = 1.0

CLAP_SAMPLING_FAST: str = "fast"
CLAP_SAMPLING_BALANCED: str = "balanced"
CLAP_SAMPLING_THOROUGH: str = "thorough"
CLAP_WINDOW_COUNTS: dict[str, int] = {
    CLAP_SAMPLING_FAST: 1,
    CLAP_SAMPLING_BALANCED: 3,
    CLAP_SAMPLING_THOROUGH: 8,
}

CONF_CLAP_SAMPLING: str = "clap_sampling"

# Sonic Analysis runs on-device CLAP inference; gate it to capable hardware.
# 4GB nominal; the gate's tolerance (meets_memory_target) admits genuine 4GB hosts,
# which report ~3.8GB after the kernel/firmware reservation.
MIN_RAM_GB: float = 4.0
MIN_CPU_CORES: int = 2
# Below the recommended thresholds the provider still runs, but we surface an
# informational notice (see get_config_entries) as it may be tight under load.
RECOMMENDED_RAM_GB: float = 6.0
RECOMMENDED_CPU_CORES: int = 4

MODEL_FAILURE_RETRY_DELAY: timedelta = timedelta(hours=24)

# How long setup waits for the model to be ready before it hands back to MA.
# Generous, because adding a provider has no retry behind it: add_provider_config drops
# the config again when the load raises, so an expiring grace costs the user a second
# attempt. It does not have to cover the whole job — the preparation runs as one tracked,
# deduplicated task that outlives the attempt, so whatever it does not cover is picked up
# by the next attempt joining that same task rather than starting over.
MODEL_SETUP_GRACE_SECONDS: int = 200

# The CLAP model, its text embeddings, and the scalar order the embeddings are in.
type _ClapModels = tuple[Any, Any, list[tuple[str, tuple[str, str]]]]


@dataclass
class SonicSessionData(AnalysisSessionData):
    """Per-session state: PCM block buffer and accumulated per-block features."""

    pcm_buffer: bytearray = field(default_factory=bytearray)
    block_bytes: int = 0
    resampler: soxr.ResampleStream | None = None
    accumulated: BlockFeatures = field(default_factory=BlockFeatures)
    total_samples: int = 0
    overlap: np.ndarray | None = None
    start_time: float = 0.0
    peak_absolute: float = 0.0
    # Per-window selective buffer for live CLAP. clap_target_starts is
    # planned at session start from streamdetails.duration + preset; the
    # buffers fill via _dispatch_clap_chunk and free on completion.
    clap_target_starts: list[int] = field(default_factory=list)
    clap_target_buffers: list[list[np.ndarray]] = field(default_factory=list)
    clap_target_complete: list[bool] = field(default_factory=list)
    clap_position_samples: int = 0
    # Inference task handles + running sums for mean-pooling at finalize.
    clap_inference_tasks: list[asyncio.Task[None]] = field(default_factory=list)
    clap_sum_embedding: np.ndarray | None = None
    clap_sum_similarities: np.ndarray | None = None
    clap_completed_count: int = 0
    # Timing accumulators for the finalize diagnostic breakdown. feature_seconds
    # sums the inline librosa decode/extract/collapse work; clap_seconds sums each
    # per-window CLAP await-to-completion span (timer starts before the offload
    # hop, so it folds in scheduling/queue wait — and since windows run concurrently
    # it is cumulative and can exceed wall-clock). clap_preset records the configured
    # sampling preset for the same line.
    feature_seconds: float = 0.0
    clap_seconds: float = 0.0
    clap_preset: str = ""


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider instance with given configuration."""
    return SonicAnalysisProvider(mass, manifest, config)


def compute_clap_target_starts(
    track_duration_s: float,
    preset_n: int,
    source_sr: int,
) -> list[int]:
    """
    Plan deterministic 7s window start offsets for the live CLAP path.

    :param track_duration_s: Total track duration in seconds.
    :param preset_n: Configured window count (Fast/Balanced/Thorough → 1/3/8).
    :param source_sr: Sample rate the live PCM stream is delivered at.
    :returns: Sample-position offsets at source_sr; length is the effective N
        (capped at what the track length supports without near-duplicates).
    """
    if track_duration_s < CLAP_MIN_WINDOW_SECONDS:
        return []
    if track_duration_s < CLAP_WINDOW_SECONDS:
        return [0]
    if track_duration_s < CLAP_SKIP_SECONDS + CLAP_WINDOW_SECONDS:
        start_seconds = (track_duration_s - CLAP_WINDOW_SECONDS) / 2.0
        return [int(start_seconds * source_sr)]

    usable_start = float(CLAP_SKIP_SECONDS)
    usable_end = track_duration_s - CLAP_WINDOW_SECONDS
    if preset_n <= 1:
        return [int(usable_start * source_sr)]

    usable_seconds = usable_end - usable_start
    max_non_overlap = int(usable_seconds // CLAP_WINDOW_SECONDS) + 1
    effective_n = max(1, min(preset_n, max_non_overlap))
    if effective_n == 1:
        return [int(usable_start * source_sr)]

    positions = np.linspace(usable_start, usable_end, effective_n)
    return [int(p * source_sr) for p in positions]


def _store_clap_embedding(analysis: AudioAnalysisData, embedding: np.ndarray) -> None:
    """Store the CLAP audio embedding on the analysis object for downstream consumers."""
    if analysis.extra_data is None:
        analysis.extra_data = {}
    analysis.extra_data[EXTRA_DATA_CLAP_EMBEDDING] = embedding.tolist()


def _dispatch_clap_chunk(
    session: SonicSessionData,
    decoded_audio: np.ndarray,
    source_sr: int,
) -> list[np.ndarray]:
    """
    Route a PCM chunk to active CLAP target windows; return any windows completed.

    :param session: Active session; target buffers are mutated in place.
    :param decoded_audio: Mono float32 PCM chunk at source_sr.
    :param source_sr: Sample rate of decoded_audio.
    """
    if not session.clap_target_starts:
        return []

    chunk_start = session.clap_position_samples
    chunk_end = chunk_start + len(decoded_audio)
    session.clap_position_samples = chunk_end

    window_samples = CLAP_WINDOW_SECONDS * source_sr
    completed: list[np.ndarray] = []

    for i, target_start in enumerate(session.clap_target_starts):
        if session.clap_target_complete[i]:
            continue
        target_end = target_start + window_samples
        if chunk_end <= target_start or chunk_start >= target_end:
            continue
        slice_start = max(0, target_start - chunk_start)
        slice_end = min(len(decoded_audio), target_end - chunk_start)
        session.clap_target_buffers[i].append(decoded_audio[slice_start:slice_end])

        accumulated = sum(len(arr) for arr in session.clap_target_buffers[i])
        if accumulated >= window_samples:
            window_audio = np.concatenate(session.clap_target_buffers[i])[:window_samples]
            session.clap_target_buffers[i] = []
            session.clap_target_complete[i] = True
            completed.append(window_audio)

    return completed


def _pcm_bytes_to_audio(audio_format: AudioFormat, pcm_chunk: bytes) -> np.ndarray:
    """
    Decode a raw PCM chunk to a mono float32 numpy array.

    :param audio_format: The audio format describing the PCM data.
    :param pcm_chunk: Raw PCM audio data.
    """
    import torch  # noqa: PLC0415

    content_type = audio_format.content_type
    writable = bytearray(pcm_chunk)

    if content_type == ContentType.PCM_F32LE:
        audio = torch.frombuffer(writable, dtype=torch.float32).clone()
    elif content_type == ContentType.PCM_F64LE:
        audio = torch.frombuffer(writable, dtype=torch.float64).clone().to(torch.float32)
    elif content_type == ContentType.PCM_S32LE:
        audio = (
            torch.frombuffer(writable, dtype=torch.int32).clone().to(torch.float32) / 2147483648.0
        )
    elif content_type == ContentType.PCM_S24LE:
        raw = torch.frombuffer(writable, dtype=torch.uint8).clone()
        raw = raw[: (raw.numel() // 3) * 3].reshape(-1, 3).to(torch.int32)
        audio = raw[:, 0] | (raw[:, 1] << 8) | (raw[:, 2] << 16)
        audio = torch.where(audio & 0x800000 != 0, audio - 0x1000000, audio)
        audio = audio.to(torch.float32) / 8388608.0
    else:
        audio = torch.frombuffer(writable, dtype=torch.int16).clone().to(torch.float32) / 32768.0

    channels = audio_format.channels
    if channels > 1:
        frame_samples = (audio.numel() // channels) * channels
        audio = audio[:frame_samples].reshape(-1, channels).mean(dim=1)

    return np.asarray(audio.numpy(), dtype=np.float32)


def _decode_resample_extract(
    audio_format: AudioFormat,
    block_bytes: bytes,
    overlap: np.ndarray | None,
    sample_rate: int,
    resampler: soxr.ResampleStream | None,
    *,
    is_last: bool = False,
) -> tuple[np.ndarray, np.ndarray, BlockFeatures | None]:
    """
    Decode PCM bytes, optionally resample, and extract block features in one offloaded call.

    :param audio_format: AudioFormat describing the PCM encoding of block_bytes.
    :param block_bytes: Raw PCM bytes for one analysis block (or the final tail).
    :param overlap: Post-resample samples from the previous block to prepend before
        feature extraction, or None for the first block.
    :param sample_rate: Sample rate expected by extract_block_features
        (must equal ANALYSIS_SAMPLE_RATE after resampling).
    :param resampler: Active ResampleStream, or None when source SR already matches
        ANALYSIS_SAMPLE_RATE.
    :param is_last: Pass True when processing the tail PCM in _finalize so the resampler
        flushes its internal delay buffer.
    :returns: A 3-tuple of (pre_resample_audio, post_resample_audio, block_features).
        pre_resample_audio is at the source sample rate (for CLAP dispatch and duration/peak
        accounting). post_resample_audio is at ANALYSIS_SAMPLE_RATE (for overlap bookkeeping).
        block_features is None when the audio is too short for meaningful extraction.
    """
    pre_resample = _pcm_bytes_to_audio(audio_format, block_bytes)
    if resampler is not None:
        post_resample = resampler.resample_chunk(pre_resample, last=is_last)
    else:
        post_resample = pre_resample
    audio_for_extract = (
        np.concatenate([overlap, post_resample]) if overlap is not None else post_resample
    )
    bf = extract_block_features(audio_for_extract, sample_rate)
    return pre_resample, post_resample, bf


class SonicAnalysisProvider(AudioAnalysisProvider):
    """Audio analysis provider running librosa scalars + CLAP zero-shot per track."""

    analysis_version: int = 1
    max_analysis_duration = ACCUMULATING_ANALYSIS_MAX_DURATION_SECONDS
    has_unloadable_models = True

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        supported_features: set[ProviderFeature] | None = None,
    ) -> None:
        """Initialize the provider."""
        super().__init__(mass, manifest, config, supported_features)
        self._clap_model: Any = None
        self._clap_text_embeddings: Any = None
        self._clap_prompt_order: list[tuple[str, tuple[str, str]]] = []
        # Checkpoint path resolved once by _fetch_model_assets. Passing it to CLAPWrapper
        # keeps every later (re)load local: hf_hub_download revalidates the entry tag over
        # the network even on a cache hit, and models are reloaded after each idle unload.
        self._clap_model_fp: str | None = None

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider."""
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
            ConfigEntry(
                key=CONF_CLAP_SAMPLING,
                type=ConfigEntryType.STRING,
                default_value=CLAP_SAMPLING_FAST,
                options=[
                    ConfigValueOption(CLAP_SAMPLING_FAST),
                    ConfigValueOption(CLAP_SAMPLING_BALANCED),
                    ConfigValueOption(CLAP_SAMPLING_THOROUGH),
                ],
                required=False,
            ),
        )

    async def handle_async_init(self) -> None:
        """
        Load the CLAP model so provider.available only goes True once analysis can run.

        Setup completes only when the model is resident, which is what the
        AudioAnalysisController honors when scheduling work. While idle the model is
        later unloaded and reloaded on demand.

        :raises SetupFailedError: When the checkpoint is still downloading, or the fetch
            failed. The download survives either way, so a reload (which MA retries on its
            own) or a fresh setup picks up where this attempt left off rather than
            starting the transfer again.
        """
        await verify_system_meets_requirements(
            feature_name="Sonic Analysis",
            min_memory_gb=MIN_RAM_GB,
            min_cpu_cores=MIN_CPU_CORES,
            require_ml_inference=True,
        )
        # Configure the inference runtime before loading the model (see the controller method).
        self.mass.streams.audio_analysis.ensure_inference_runtime_configured()
        await self._ensure_models_ready()
        self._models_loaded = True

    async def cancel(self, session_id: str) -> None:
        """Cancel pending CLAP inferences and free per-window buffers."""
        session = self._sessions.get(session_id)
        if isinstance(session, SonicSessionData):
            for task in session.clap_inference_tasks:
                if not task.done():
                    task.cancel()
            session.clap_target_buffers.clear()
        await super().cancel(session_id)

    async def process_pcm_chunk(
        self,
        session_id: str,
        pcm_chunk: bytes,
    ) -> None:
        """
        Accumulate PCM and run feature extraction once a 10-second block is full.

        :param session_id: The analysis session ID.
        :param pcm_chunk: Raw PCM audio data.
        """
        if session_id not in self._sessions:
            return
        session = self._sessions[session_id]
        assert isinstance(session, SonicSessionData)
        session.pcm_buffer.extend(pcm_chunk)
        af = session.audio_format
        if len(session.pcm_buffer) >= session.block_bytes:
            block_bytes = bytes(session.pcm_buffer[: session.block_bytes])
            del session.pcm_buffer[: session.block_bytes]
            t0 = time.monotonic()
            pre_audio, post_audio, bf = await self._run_offloaded(
                _decode_resample_extract,
                af,
                block_bytes,
                session.overlap,
                ANALYSIS_SAMPLE_RATE,
                session.resampler,
            )
            session.feature_seconds += time.monotonic() - t0
            session.total_samples += len(pre_audio)
            session.peak_absolute = max(session.peak_absolute, float(np.max(np.abs(pre_audio))))
            self._dispatch_clap_to_targets(session, pre_audio, af.sample_rate)
            session.overlap = post_audio[-OVERLAP_SAMPLES:].copy()
            if bf is not None:
                merge_block_features(session.accumulated, bf)

    async def _ensure_models_ready(self) -> None:
        """
        Bring the CLAP model into memory, waiting no longer than the grace period.

        :raises SetupFailedError: When the model is not ready in time, or preparing it
            failed. The preparation is deliberately left running in both cases, so a
            later attempt joins it instead of starting over.
        """
        # The preparation has to survive this coroutine: it is the 300s setup bound that
        # it blows, so cancelling it on our way out would redo the whole job on every
        # retry. Cancelling would not even work — the download and the model build both
        # sit in worker threads. mass.create_task tracks it for shutdown and retrieves
        # its exception; the task_id makes a later setup attempt re-attach instead.
        # Keep _prepare_models a named coroutine: a failed task is logged by its target,
        # and "_prepare_models" beats a bare to_thread coroutine object.
        task = self.mass.create_task(
            self._prepare_models(),
            task_id=f"sonic_analysis.model_setup.{self.instance_id}",
            abort_existing=False,
        )
        try:
            # The result has to come back through the task: MA builds a fresh provider
            # instance per load attempt, so an attempt that re-attaches here is not the
            # instance that started the work and cannot be handed anything directly.
            model_fp, models = await join_task(task, timeout=MODEL_SETUP_GRACE_SECONDS)
        except TimeoutError:
            if task.done():
                # A timeout the preparation itself raised, not our grace running out.
                # Only reachable if one ever escapes the conversion in the fetch, but
                # relabelling it as "still downloading" would bury a real failure.
                raise
            raise SetupFailedError(
                "The Sonic Analysis model is still downloading. "
                "It keeps running in the background; Sonic Analysis starts as soon as "
                "it is ready.",
                translation_key="model_assets_downloading",
                translation_owner=self.translation_owner,
            ) from None
        self._clap_model_fp = model_fp
        self._clap_model, self._clap_text_embeddings, self._clap_prompt_order = models

    async def _prepare_models(self) -> tuple[str, _ClapModels]:
        """
        Download the CLAP checkpoint if it is missing, then build the model around it.

        Both halves belong to one task so a setup attempt that gives up cannot leave a
        later one free to start a second download — or a second model build, which is
        the part that costs a full model's worth of memory on a host that has little.

        :returns: The checkpoint path, and the loaded model, text embeddings and
            prompt ordering.
        :raises SetupFailedError: When the checkpoint could not be fetched, or the
            shipped prompt embeddings are unusable.
        """
        model_fp: str = await asyncio.to_thread(self._download_clap_weights)
        self.logger.debug("CLAP checkpoint ready at %s", model_fp)
        return model_fp, await asyncio.to_thread(self._load_clap, model_fp)

    async def _load_models(self) -> None:
        """Load the CLAP model and prompt embeddings into memory."""
        (
            self._clap_model,
            self._clap_text_embeddings,
            self._clap_prompt_order,
        ) = await asyncio.to_thread(self._load_clap, self._clap_model_fp)

    def _download_clap_weights(self) -> str:
        """
        Fetch the CLAP checkpoint into the HuggingFace cache and return its path.

        Only ever call this off the event loop: importing the vendored wrapper pulls in
        torch and transformers, and the download itself is long and uninterruptible.

        :raises SetupFailedError: When the checkpoint could not be downloaded.
        """
        import httpx  # noqa: PLC0415
        from huggingface_hub.errors import XetDownloadError  # noqa: PLC0415

        from .vendored_clap import CLAP  # noqa: PLC0415

        try:
            # A checkpoint already on disk resolves without touching the network, so a
            # setup attempt that follows a completed download costs nothing and works
            # offline. Only a genuinely absent checkpoint reaches the transfer.
            return CLAP.cached_weights() or CLAP.download_weights()
        except (OSError, httpx.HTTPError, XetDownloadError) as err:
            # The hub reports most transport, HTTP and disk failures as OSError, but it
            # streams the body through httpx and re-raises its transport errors verbatim
            # once its own retries are spent. XetDownloadError is defensive: the pinned
            # hub never raises it, and its Xet backend surfaces failures from a Rust
            # extension whose exception types are not ours to name.
            # Only a typed MA error marks the failure retryable, so a flaky link recovers.
            raise SetupFailedError(
                f"Failed to download the Sonic Analysis model: {err}",
                translation_key="model_assets_download_failed",
                translation_owner=self.translation_owner,
            ) from err

    def _free_models(self) -> None:
        """Release the CLAP model and prompt embeddings."""
        self._clap_model = None
        self._clap_text_embeddings = None

    def _load_clap(self, model_fp: str | None) -> _ClapModels:
        """
        Load and return the CLAP model, text embeddings, and prompt ordering.

        :param model_fp: Path of the checkpoint to load. None makes the wrapper resolve
            it itself, which costs a network round trip.
        :raises SetupFailedError: When the shipped prompt embeddings are missing or no
            longer match the prompts they were computed from.
        """
        import torch  # noqa: PLC0415

        from .vendored_clap import CLAP  # noqa: PLC0415

        prompt_order: list[tuple[str, tuple[str, str]]] = list(SCALAR_PROMPT_PAIRS.items())

        cached = self._try_load_cached_prompt_embeddings()
        if cached is None:
            # Building a text-enabled model here would download the GPT2 text encoder,
            # putting a second unbounded fetch inside a step that must stay local.
            raise SetupFailedError(
                "The precomputed CLAP prompt embeddings are missing or out of date. "
                "Re-run scripts/precompute_clap_prompt_embeddings.py to regenerate them.",
                translation_key="prompt_embeddings_unavailable",
                translation_owner=self.translation_owner,
            )
        model = CLAP(model_fp=model_fp, version="2023", use_cuda=False, text_enabled=False)
        self.logger.info("CLAP model loaded; %d prompt pairs ready", len(prompt_order))
        return model, torch.from_numpy(cached), prompt_order

    def _try_load_cached_prompt_embeddings(self) -> np.ndarray | None:
        """Return shipped prompt embeddings if present and hash-current, else None."""
        try:
            cached_embeddings, cached_hash = load_precomputed_prompt_embeddings(
                PRECOMPUTED_EMBEDDINGS_PATH
            )
        except FileNotFoundError:
            self.logger.warning(
                "Precomputed CLAP prompt embeddings missing at %s; "
                "re-run scripts/precompute_clap_prompt_embeddings.py",
                PRECOMPUTED_EMBEDDINGS_PATH,
            )
            return None
        expected_hash = hash_scalar_prompt_pairs(SCALAR_PROMPT_PAIRS)
        if cached_hash != expected_hash:
            self.logger.warning(
                "Precomputed CLAP prompt embeddings hash mismatch (%s != %s); "
                "re-run scripts/precompute_clap_prompt_embeddings.py",
                cached_hash[:12],
                expected_hash[:12],
            )
            return None
        return cached_embeddings

    async def _start_analysis(
        self,
        session_id: str,
        streamdetails: StreamDetails,
        audio_format: AudioFormat,
    ) -> bool:
        """
        Initialize a new analysis session.

        :param session_id: Unique session ID from the controller.
        :param streamdetails: Stream details for the item being analyzed.
        :param audio_format: PCM format of the audio stream.
        """
        if self._clap_model is None:
            self.logger.debug(
                "Skipping analysis for %s: CLAP model not yet available",
                streamdetails.item_id,
            )
            return False
        if not streamdetails.duration:
            # Without a known duration we can't plan CLAP windows, and the result
            # would be librosa-only — unusable for similarity. Reject so the next
            # analysis attempt (with duration filled in) can succeed instead of
            # caching an incomplete record.
            self.logger.debug(
                "Skipping analysis for %s: streamdetails.duration missing or zero",
                streamdetails.item_id,
            )
            return False
        bytes_per_sample = audio_format.bit_depth // 8
        block_bytes = (
            audio_format.sample_rate * bytes_per_sample * audio_format.channels * BLOCK_SECONDS
        )
        if block_bytes <= 0:
            self.logger.warning(
                "Invalid audio format for session %s (sample_rate=%d, bit_depth=%d, channels=%d)"
                " — skipping analysis",
                session_id,
                audio_format.sample_rate,
                audio_format.bit_depth,
                audio_format.channels,
            )
            return False
        preset = str(self.config.get_value(CONF_CLAP_SAMPLING, CLAP_SAMPLING_FAST))
        preset_n = CLAP_WINDOW_COUNTS.get(preset, 1)
        target_starts = compute_clap_target_starts(
            streamdetails.duration, preset_n, audio_format.sample_rate
        )

        resampler: soxr.ResampleStream | None = None
        if audio_format.sample_rate != ANALYSIS_SAMPLE_RATE:
            resampler = soxr.ResampleStream(
                in_rate=audio_format.sample_rate,
                out_rate=ANALYSIS_SAMPLE_RATE,
                num_channels=1,
                dtype="float32",
            )
        self._sessions[session_id] = SonicSessionData(
            streamdetails=streamdetails,
            audio_format=audio_format,
            block_bytes=block_bytes,
            resampler=resampler,
            start_time=time.monotonic(),
            clap_target_starts=target_starts,
            clap_target_buffers=[[] for _ in target_starts],
            clap_target_complete=[False] * len(target_starts),
            clap_preset=preset,
        )
        self.logger.debug(
            "Started sonic analysis for %s/%s (preset=%s, %d CLAP target windows)",
            streamdetails.provider,
            streamdetails.item_id,
            preset,
            len(target_starts),
        )
        return True

    def _dispatch_clap_to_targets(
        self, session: SonicSessionData, audio: np.ndarray, source_sr: int
    ) -> None:
        """Route a decoded block to active CLAP target windows; spawn inference per completion."""
        if not session.clap_target_starts:
            return
        completed = _dispatch_clap_chunk(session, audio, source_sr)
        for window_audio in completed:
            task = self.mass.create_task(
                self._run_single_clap_window(session, window_audio, source_sr)
            )
            session.clap_inference_tasks.append(task)

    async def _run_live_clap_if_eligible(
        self, session: SonicSessionData, analysis: AudioAnalysisData
    ) -> None:
        """
        Finalize CLAP analysis, writing the scalar attributes and embedding onto the analysis.

        :param session: The analysis session.
        :param analysis: Analysis object the CLAP scalars and embedding are written onto.
        :raises AudioAnalysisError: Retryable, when fewer windows completed than were planned.
        """
        if not session.clap_target_starts:
            return
        if session.clap_inference_tasks:
            await asyncio.gather(*session.clap_inference_tasks, return_exceptions=True)
        n = session.clap_completed_count
        planned = len(session.clap_target_starts)
        sd = session.streamdetails
        if (
            n < planned
            or session.clap_sum_embedding is None
            or session.clap_sum_similarities is None
        ):
            self.logger.warning(
                "Live CLAP for %s/%s: only %d of %d planned windows completed — "
                "failing the analysis so it is retried",
                sd.provider,
                sd.item_id,
                n,
                planned,
            )
            raise AudioAnalysisError(
                f"live CLAP completed {n} of {planned} planned windows",
                retry_at=utc() + MODEL_FAILURE_RETRY_DELAY,
            )

        mean_emb = session.clap_sum_embedding / n
        norm = float(np.linalg.norm(mean_emb))
        if norm > 0:
            mean_emb = mean_emb / norm
        mean_sim = session.clap_sum_similarities / n

        for scalar_name, value in score_scalars(mean_sim).items():
            setattr(analysis, scalar_name, value)

        _store_clap_embedding(analysis, mean_emb)
        self.logger.debug(
            "Live CLAP for %s/%s: %d/%d windows completed",
            sd.provider,
            sd.item_id,
            n,
            len(session.clap_target_starts),
        )

    async def _finalize(self, session_id: str) -> AudioAnalysisData | None:
        """
        Flush remaining PCM, collapse features, and return the analysis result.

        Returns the analysis for the base class to persist, or None to skip persistence.

        :param session_id: The analysis session ID.
        """
        if session_id not in self._sessions:
            self.logger.debug("Finalize called for unknown session %s", session_id)
            return None
        session = self._sessions[session_id]
        assert isinstance(session, SonicSessionData)
        sd = session.streamdetails
        af = session.audio_format

        if session.pcm_buffer:
            t0 = time.monotonic()
            pre_audio, _post_audio, bf = await self._run_offloaded(
                _decode_resample_extract,
                af,
                bytes(session.pcm_buffer),
                session.overlap,
                ANALYSIS_SAMPLE_RATE,
                session.resampler,
                is_last=True,
            )
            session.feature_seconds += time.monotonic() - t0
            session.total_samples += len(pre_audio)
            session.peak_absolute = max(session.peak_absolute, float(np.max(np.abs(pre_audio))))
            self._dispatch_clap_to_targets(session, pre_audio, af.sample_rate)
            if bf is not None:
                merge_block_features(session.accumulated, bf)
            session.pcm_buffer.clear()

        if not session.accumulated.rms_frames:
            raise AudioAnalysisError("no usable audio frames extracted")

        self._flush_incomplete_clap_windows(session, af.sample_rate)

        t0 = time.monotonic()
        analysis = await self._run_offloaded(
            collapse_to_analysis, session.accumulated, ANALYSIS_SAMPLE_RATE
        )
        session.feature_seconds += time.monotonic() - t0

        analysis.duration = session.total_samples / af.sample_rate
        if session.peak_absolute > 0:
            analysis.true_peak = float(20.0 * np.log10(session.peak_absolute))
        else:
            analysis.true_peak = -96.0

        await self._run_live_clap_if_eligible(session, analysis)

        elapsed = time.monotonic() - session.start_time
        self.logger.debug(
            "Stored analysis for %s/%s (%.1fs elapsed: feature=%.1fs, "
            "clap=%.1fs cumulative over %d/%d windows, preset=%s)",
            sd.provider,
            sd.item_id,
            elapsed,
            session.feature_seconds,
            session.clap_seconds,
            session.clap_completed_count,
            len(session.clap_target_starts),
            session.clap_preset,
        )
        return analysis

    def _flush_incomplete_clap_windows(self, session: SonicSessionData, source_sr: int) -> None:
        """
        Dispatch every planned CLAP window that buffered audio but never filled to 7 seconds.

        :param session: The analysis session; the buffers of flushed windows are consumed.
        :param source_sr: Sample rate the buffered PCM was captured at.
        """
        # The vendored wrapper repeat-pads short input, so anything below the floor would
        # blow a sliver of audio up into a full window and embed noise.
        min_samples = int(CLAP_MIN_WINDOW_SECONDS * source_sr)
        for i, buffered in enumerate(session.clap_target_buffers):
            if session.clap_target_complete[i]:
                continue
            if sum(len(arr) for arr in buffered) < min_samples:
                continue
            window_audio = np.concatenate(buffered)
            session.clap_target_buffers[i] = []
            session.clap_target_complete[i] = True
            task = self.mass.create_task(
                self._run_single_clap_window(session, window_audio, source_sr)
            )
            session.clap_inference_tasks.append(task)

    def _single_window_inference_sync(
        self,
        window_audio: np.ndarray,
        source_sr: int,
    ) -> tuple[np.ndarray, np.ndarray] | None:
        """
        Run CLAP inference on a single 7-second window.

        :param window_audio: Mono float32 audio at source_sr.
        :param source_sr: Sample rate of window_audio.
        :returns: (1024-dim embedding, similarity logit row), or None if the model is unloaded.
        """
        import torch  # noqa: PLC0415

        model = self._clap_model
        text_embeddings = self._clap_text_embeddings
        if model is None or text_embeddings is None:
            return None
        window_tensor = torch.from_numpy(window_audio)
        audio_embs = model.get_audio_embeddings_from_tensor([window_tensor], source_sr)
        similarities = model.compute_similarity(audio_embs, text_embeddings)
        embedding = audio_embs[0].detach().cpu().numpy().astype(np.float32).reshape(-1)
        similarity_row = similarities[0].detach().cpu().numpy().astype(np.float32).reshape(-1)
        return embedding, similarity_row

    async def _run_single_clap_window(
        self,
        session: SonicSessionData,
        window_audio: np.ndarray,
        source_sr: int,
    ) -> None:
        """Run CLAP on a single window off-thread and accumulate running sums."""
        if self._clap_model is None:
            return
        t0 = time.monotonic()
        try:
            result = await self._run_offloaded(
                self._single_window_inference_sync, window_audio, source_sr
            )
        except asyncio.CancelledError:
            raise
        except Exception as err:
            # CLAP inference runs torch ops off-thread; the failure surface is broad and
            # version-dependent, so any failure just drops this window's contribution.
            self.logger.debug("CLAP single-window inference failed: %s", err)
            return
        finally:
            session.clap_seconds += time.monotonic() - t0
        if result is None:
            self.logger.debug("CLAP inference skipped — model unloaded mid-flight")
            return
        embedding, similarity_row = result
        if session.clap_sum_embedding is None:
            session.clap_sum_embedding = np.zeros_like(embedding)
            session.clap_sum_similarities = np.zeros_like(similarity_row)
        assert session.clap_sum_similarities is not None  # narrowed by line above
        session.clap_sum_embedding += embedding
        session.clap_sum_similarities += similarity_row
        session.clap_completed_count += 1
