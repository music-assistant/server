"""Sonic Analysis provider for Music Assistant.

On-device audio analysis combining two engines driven off a single
audio load per track:

- librosa-derived measurements (extract_block_features / collapse_to_analysis)
  producing energy, loudness_integrated/range, brightness,
  harmonic_complexity, roughness, rhythmic_regularity, rms_energy +
  spectral_centroid time series.

- Microsoft CLAP zero-shot inference (vendored msclap) producing
  Platt-calibrated danceability, valence, arousal, instrumentalness,
  acousticness, plus the raw 1024-dim audio embedding which is persisted
  under audio_analysis.extra_data["clap_embedding"] for downstream
  plugins (e.g. sonic_clap) to consume.

This provider previously lived as two separate providers (sonic_analysis
and clap_analysis). They were merged because each was loading the audio
file independently — wasteful double I/O for network-attached libraries.
The merged analyze_file loads once and feeds both feature engines.

Live-playback path (process_pcm_chunk / _finalize via AudioAnalysisController)
produces librosa outputs and — for non-filesystem providers — runs CLAP
on a deterministic 7-second window, so streaming providers (Tidal,
Spotify, Qobuz, etc.) also get the 5 soft scalars and the persisted
1024-dim embedding.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np
import torch
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType

from music_assistant.models.audio_analysis import AudioAnalysisData
from music_assistant.models.audio_analysis_provider import (
    AnalysisSessionData,
    AudioAnalysisProvider,
)

from .clap_prompts import (
    CALIBRATION,
    PRECOMPUTED_EMBEDDINGS_PATH,
    SCALAR_PROMPT_PAIRS,
    hash_scalar_prompt_pairs,
    load_precomputed_prompt_embeddings,
)
from .helpers import (
    BlockFeatures,
    collapse_to_analysis,
    extract_block_features,
    merge_block_features,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.media_items import AudioFormat
    from music_assistant_models.provider import ProviderManifest
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

ANALYZE_FILE_SAMPLE_RATE: int = 22050

BLOCK_SECONDS: int = 10
OVERLAP_SAMPLES: int = 2048

# Key under audio_analysis.extra_data where the 1024-dim CLAP audio
# embedding is persisted. Downstream plugins (sonic_clap) read this to
# build search indexes; sonic_analysis does not own any usearch index.
EXTRA_DATA_CLAP_EMBEDDING: str = "clap_embedding"

# CLAP's HTSAT audio encoder has a fixed 7-second input at 44.1 kHz —
# not a knob, architecturally load-bearing. We always feed it exactly
# one 7s window per call; repeated calls return identical embeddings
# because we slice deterministically before handing it off (instead of
# letting the vendored wrapper's random.randrange pick).
CLAP_WINDOW_SECONDS: int = 7
# Skip the first 45s of each track (intros, buildups, sparse openers)
# and sample past that. For Fast (N=1) this lands the single window at
# [45s, 52s); for multi-window modes this is where the sampled region
# begins. 45s (vs the original 30s) is more conservative — empirically
# fewer tracks slip into a window that's still in their intro region.
CLAP_SKIP_SECONDS: int = 45

# Sampling presets — one enum value in the provider config maps to a
# window count. More windows → more representative embeddings (mean-
# pooled) and scalars (mean-pooled logits) at linear CPU cost.
CLAP_SAMPLING_FAST: str = "fast"
CLAP_SAMPLING_BALANCED: str = "balanced"
CLAP_SAMPLING_THOROUGH: str = "thorough"
CLAP_WINDOW_COUNTS: dict[str, int] = {
    CLAP_SAMPLING_FAST: 1,
    CLAP_SAMPLING_BALANCED: 3,
    CLAP_SAMPLING_THOROUGH: 8,
}

# Live-path PCM buffer cap in seconds (at session sample rate). Sized to
# cover the Thorough preset's spread (~8 windows + intro skip ~= 86s);
# smaller presets just ignore the extra. At 48 kHz stereo this is ~17 MB
# per session — bounded so podcasts don't pin memory indefinitely.
CLAP_LIVE_BUFFER_SECONDS: float = 90.0

CONF_CLAP_SAMPLING: str = "clap_sampling"

# Upstream's AudioAnalysisController runs analyze_file for tracks from
# these provider domains only. For those, the background scan fills in
# CLAP scalars and the persisted embedding — running CLAP again in the
# live path would be redundant. Streaming providers (Tidal, Spotify,
# Qobuz, etc.) never hit analyze_file, so live CLAP is the only way
# they get the 5 soft scalars + the 1024-dim embedding in extra_data.
FILESYSTEM_PROVIDER_DOMAINS: frozenset[str] = frozenset(
    {"filesystem_local", "filesystem_smb", "filesystem_nfs"}
)


@dataclass
class SonicSessionData(AnalysisSessionData):
    """Per-session state: PCM block buffer and accumulated per-block features."""

    pcm_buffer: bytearray = field(default_factory=bytearray)
    block_samples: int = 0
    accumulated: BlockFeatures = field(default_factory=BlockFeatures)
    total_samples: int = 0
    overlap: np.ndarray | None = None
    start_time: float = 0.0
    peak_absolute: float = 0.0
    waveform_peaks: list[float] = field(default_factory=list)
    # Accumulated raw PCM for live CLAP inference in _finalize, capped
    # at CLAP_LIVE_MAX_SECONDS of source-SR mono audio. Only populated
    # when the CLAP model loaded successfully; otherwise left empty.
    clap_audio: list[np.ndarray] = field(default_factory=list)
    clap_audio_samples: int = 0


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider instance with given configuration."""
    return SonicAnalysisProvider(mass, manifest, config)


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider.

    :param mass: MusicAssistant instance.
    :param instance_id: id of an existing provider instance (None if new instance setup).
    :param action: action key called from config entries UI.
    :param values: the (intermediate) raw values for config entries sent with the action.
    """
    return (
        ConfigEntry(
            key=CONF_CLAP_SAMPLING,
            type=ConfigEntryType.STRING,
            label="CLAP quality (windows per track)",
            description=(
                "How many 7-second audio windows CLAP analyzes per "
                "track. Choices are benchmarked against external "
                "references (Essentia, LAION-CLAP, MERT-based "
                "Music2Emo) on a 50-track ground-truth set with "
                "bootstrap confidence intervals. More windows cost "
                "proportionally more CPU.\n\n"
                "• Fast — 1 window. Default. Best-measured for "
                "danceability, arousal, and valence; tied or near-"
                "tied for the other attributes in most sources.\n"
                "• Balanced — 3 windows (~2.4x CPU). Slightly "
                "better at classifying acousticness; a solid all-"
                "around middle choice.\n"
                "• Thorough — 8 windows (~6.6x CPU). Meaningfully "
                "better at instrumentalness classification — vocals "
                "appear intermittently across a track and a single "
                "window can miss them if it lands in an instrumental "
                "bridge. Pick this if 'find me instrumental tracks' "
                "quality specifically matters to you."
            ),
            default_value=CLAP_SAMPLING_FAST,
            options=[
                ConfigValueOption("Fast (1 window)", CLAP_SAMPLING_FAST),
                ConfigValueOption("Balanced (3 windows, 2.4x CPU)", CLAP_SAMPLING_BALANCED),
                ConfigValueOption("Thorough (8 windows, 6.6x CPU)", CLAP_SAMPLING_THOROUGH),
            ],
            required=False,
        ),
    )


def select_clap_window(audio: np.ndarray, source_sr: int) -> np.ndarray | None:
    """Deterministically select the 7-second slice CLAP will see (single-window).

    :param audio: Mono float32 audio at source_sr.
    :param source_sr: Sample rate of audio in Hz.
    :returns: The 7-second window (at source_sr) to feed CLAP, or None
        if audio is too short (< 1s) to be meaningful.

    Selection order:
      1. Preferred: samples [45s, 52s) — skips typical intros.
      2. Fallback (track shorter than 52s): the middle 7 seconds.
      3. Short-track fallback (track shorter than 7s but >= 1s): the
         whole clip; CLAP's wrapper pads by repeat to reach its fixed
         7-second input.
      4. Anything under 1s: return None so the caller skips inference.
    """
    skip_n = CLAP_SKIP_SECONDS * source_sr
    window_n = CLAP_WINDOW_SECONDS * source_sr
    needed_full = skip_n + window_n
    n = len(audio)
    if n >= needed_full:
        return audio[skip_n : skip_n + window_n]
    if n >= window_n:
        start = (n - window_n) // 2
        return audio[start : start + window_n]
    if n >= source_sr:
        return audio
    return None


def select_clap_windows(audio: np.ndarray, source_sr: int, n_windows: int) -> list[np.ndarray]:
    """Return up to n_windows deterministic 7s slices spanning the track.

    :param audio: Mono float32 audio at source_sr.
    :param source_sr: Sample rate of audio in Hz.
    :param n_windows: Target number of windows. >= 1.
    :returns: List of window arrays (possibly shorter than n_windows if the
        track is too short). Empty list if audio is too short for CLAP at all.

    For n_windows == 1, delegates to select_clap_window (the "skip 45s,
    take next 7s" rule with short-track fallback). For n_windows > 1,
    evenly spaces n_windows window-start positions from the 45s mark to
    the latest position that still fits a 7s window — so the first
    window starts at the same place as the single-window rule, and the
    last ends right at the track's tail. Windows may overlap slightly on
    short tracks; on long tracks there are gaps between them.
    """
    if n_windows <= 1:
        single = select_clap_window(audio, source_sr)
        return [single] if single is not None else []

    window_n = CLAP_WINDOW_SECONDS * source_sr
    skip_n = CLAP_SKIP_SECONDS * source_sr
    usable_start = skip_n
    usable_end = len(audio) - window_n
    if usable_end <= usable_start:
        # Not enough audio past the intro for multi-window spacing —
        # fall back to the single-window rule (which handles shorter
        # tracks via middle-7s / whole-clip fallbacks).
        single = select_clap_window(audio, source_sr)
        return [single] if single is not None else []

    positions = np.linspace(usable_start, usable_end, n_windows).astype(int)
    return [audio[p : p + window_n] for p in positions]


def run_clap_inference(
    model: Any,
    text_embeddings: Any,
    prompt_order: list[tuple[str, tuple[str, str]]],
    audio: np.ndarray,
    source_sr: int,
    n_windows: int = 1,
) -> tuple[dict[str, float], np.ndarray]:
    """Run a CLAP forward pass with deterministic multi-window mean pooling.

    :param model: A loaded CLAP wrapper (msclap CLAPWrapper instance).
    :param text_embeddings: Pre-computed prompt text embeddings (shape
        [2*len(prompt_order), d_proj]), ordered pos/neg/pos/neg/... per
        prompt pair.
    :param prompt_order: List of (scalar_name, (pos_prompt, neg_prompt))
        tuples in the same order the text_embeddings were computed.
    :param audio: Mono float32 audio at source_sr.
    :param source_sr: Sample rate of audio in Hz.
    :param n_windows: Number of 7s windows to sample and mean-pool.
    :returns: Tuple of (scalar_dict, 1024-dim mean-pooled audio embedding).

    Module-level so both the provider's live/analyze_file paths and the
    clap-spike eval harness can call the same code. Windows are sliced
    deterministically via select_clap_windows, fed as one batch to the
    CLAP audio encoder, and the resulting per-window embeddings are
    mean-pooled + L2-renormalized so cosine similarity in downstream
    indexes (sonic_clap) is well-defined. Per-window similarity logits
    are also mean-pooled before Platt calibration, so the scalars
    reflect the whole sampled region rather than any single window.
    """
    windows = select_clap_windows(audio, source_sr, n_windows)
    if not windows:
        msg = "audio too short for CLAP inference"
        raise ValueError(msg)

    window_tensors = [torch.from_numpy(w).to(dtype=torch.float32) for w in windows]
    audio_embs = model.get_audio_embeddings_from_tensor(window_tensors, source_sr)
    similarities = model.compute_similarity(audio_embs, text_embeddings)
    # audio_embs shape (N, d_proj); similarities shape (N, 2 * len(prompt_order))

    mean_sim = similarities.mean(dim=0).detach().cpu()
    scalars: dict[str, float] = {}
    for idx, (scalar_name, _) in enumerate(prompt_order):
        pos_logit = float(mean_sim[idx * 2])
        neg_logit = float(mean_sim[idx * 2 + 1])
        a, b = CALIBRATION[scalar_name]
        margin = pos_logit - neg_logit
        scalars[scalar_name] = 1.0 / (1.0 + math.exp(-(a * margin + b)))

    mean_emb = audio_embs.mean(dim=0)
    mean_emb = mean_emb / torch.norm(mean_emb)
    emb_np = mean_emb.detach().cpu().numpy().astype(np.float32).reshape(-1)
    return scalars, emb_np


def _store_clap_embedding(analysis: AudioAnalysisData, embedding: np.ndarray) -> None:
    """Persist the 1024-dim CLAP audio embedding under analysis.extra_data."""
    if analysis.extra_data is None:
        analysis.extra_data = {}
    analysis.extra_data[EXTRA_DATA_CLAP_EMBEDDING] = embedding.astype(np.float32).tolist()


def _pcm_bytes_to_audio(
    pcm_data: bytes,
    sample_rate: int,
    bit_depth: int,
    channels: int,
) -> np.ndarray:
    """Convert raw PCM bytes to a mono float32 numpy array.

    :param pcm_data: Raw PCM audio bytes.
    :param sample_rate: Sample rate in Hz (unused in conversion, kept for API symmetry).
    :param bit_depth: Bits per sample (16, 24, or 32).
    :param channels: Number of audio channels.
    """
    _ = sample_rate
    if bit_depth == 16:
        samples = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32)
        samples /= 32768.0
    elif bit_depth == 24:
        num_samples = len(pcm_data) // 3
        raw = np.frombuffer(pcm_data[: num_samples * 3], dtype=np.uint8).reshape(-1, 3)
        i32 = (
            raw[:, 0].astype(np.int32)
            | (raw[:, 1].astype(np.int32) << 8)
            | (raw[:, 2].astype(np.int32) << 16)
        )
        i32[i32 >= 0x800000] -= 0x1000000
        samples = i32.astype(np.float32) / 8388608.0
    elif bit_depth == 32:
        samples = np.frombuffer(pcm_data, dtype=np.int32).astype(np.float32)
        samples /= 2147483648.0
    else:
        msg = f"Unsupported bit depth: {bit_depth}"
        raise ValueError(msg)

    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return samples


class SonicAnalysisProvider(AudioAnalysisProvider):
    """Provider that extracts sonic features from audio streams.

    On file-based analysis (analyze_file), loads the audio once and runs
    both the librosa-based feature pipeline and Microsoft CLAP zero-shot
    inference against the same audio tensor. On live playback, only the
    librosa pipeline participates — CLAP is too expensive per-track for
    real-time, and its outputs are better filled in via the background
    scan when the track gets enqueued.
    """

    analysis_version: int = 1

    # CLAP state — loaded in loaded_in_mass when the provider is enabled.
    # Graceful degradation: if load fails, librosa-only mode is still
    # fully functional.
    _clap_model: Any = None
    _clap_text_embeddings: Any = None
    _clap_prompt_order: list[tuple[str, tuple[str, str]]] = []
    # API-command unregister handles, populated by loaded_in_mass and
    # invoked on unload so the provider doesn't leak handlers between
    # config-driven reloads.
    _unregister_handles: list[Callable[[], None]] = []

    async def loaded_in_mass(self) -> None:
        """Load the CLAP model and prompt embeddings."""
        try:
            (
                self._clap_model,
                self._clap_text_embeddings,
                self._clap_prompt_order,
            ) = await asyncio.to_thread(self._load_clap)
            self.logger.info(
                "CLAP model loaded; %d prompt pairs ready",
                len(self._clap_prompt_order),
            )
        except Exception as err:
            self.logger.warning("CLAP model load failed (librosa-only mode): %s", err)
            self._clap_model = None

        self._unregister_handles = [
            self.mass.register_api_command("sonic_analysis/status", self._handle_status),
            self.mass.register_api_command(
                "sonic_analysis/analyzed_tracks", self._handle_analyzed_tracks
            ),
            self.mass.register_api_command(
                "sonic_analysis/export_analysis", self._handle_export_analysis
            ),
        ]

    def _load_clap(
        self,
    ) -> tuple[Any, Any, list[tuple[str, tuple[str, str]]]]:
        """Construct the CLAP audio encoder and load prompt text embeddings.

        Always prefers the shipped precomputed prompt embeddings (skips
        the ~500MB GPT2 text encoder download). Falls back to a full live
        load if the cache is missing or its prompts-hash drifts from the
        current SCALAR_PROMPT_PAIRS.

        :returns: (model, text_embedding_matrix, prompt_order)
        """
        from .vendored_clap import CLAP  # noqa: PLC0415

        prompt_order: list[tuple[str, tuple[str, str]]] = list(SCALAR_PROMPT_PAIRS.items())

        cached = self._try_load_cached_prompt_embeddings()
        if cached is not None:
            model = CLAP(version="2023", use_cuda=False, text_enabled=False)
            return model, torch.from_numpy(cached), prompt_order

        model = CLAP(version="2023", use_cuda=False, text_enabled=True)
        flat_prompts: list[str] = []
        for _scalar, (pos, neg) in prompt_order:
            flat_prompts.extend([pos, neg])
        text_embeddings = model.get_text_embeddings(flat_prompts)  # type: ignore[no-untyped-call]
        return model, text_embeddings, prompt_order

    def _try_load_cached_prompt_embeddings(self) -> np.ndarray | None:
        """Return shipped prompt embeddings if present and hash-current, else None."""
        try:
            cached_embeddings, cached_hash = load_precomputed_prompt_embeddings(
                PRECOMPUTED_EMBEDDINGS_PATH
            )
        except FileNotFoundError:
            self.logger.warning(
                "Precomputed CLAP prompt embeddings missing at %s; loading full text encoder",
                PRECOMPUTED_EMBEDDINGS_PATH,
            )
            return None
        expected_hash = hash_scalar_prompt_pairs(SCALAR_PROMPT_PAIRS)
        if cached_hash != expected_hash:
            self.logger.warning(
                "Precomputed CLAP prompt embeddings hash mismatch (%s != %s); "
                "loading full text encoder. Re-run scripts/precompute_clap_prompt_embeddings.py.",
                cached_hash[:12],
                expected_hash[:12],
            )
            return None
        return cached_embeddings

    async def unload(self, is_removed: bool = False) -> None:
        """Release the CLAP model and unregister API handlers."""
        for unregister in self._unregister_handles:
            try:
                unregister()
            except Exception as err:
                self.logger.debug("API command unregister failed: %s", err)
        self._unregister_handles = []
        self._clap_model = None
        self._clap_text_embeddings = None
        await super().unload(is_removed)

    async def _handle_status(self) -> dict[str, Any]:
        """Return a snapshot of the analysis provider's runtime state.

        :returns: Dict with provider_loaded, clap_model_loaded,
            analyzed_tracks_count, analysis_version.
        """
        analyzed_tracks_count = 0
        if self.mass.music.database is not None:
            rows = await self.mass.music.database.get_rows_from_query(
                "SELECT COUNT(*) AS c FROM audio_analysis WHERE aa_provider_domain = :domain",
                {"domain": self.domain},
            )
            analyzed_tracks_count = int(rows[0]["c"]) if rows else 0
        return {
            "provider_loaded": True,
            "clap_model_loaded": self._clap_model is not None,
            "analyzed_tracks_count": analyzed_tracks_count,
            "analysis_version": self.analysis_version,
        }

    async def _handle_analyzed_tracks(
        self,
        search: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Return paginated list of tracks analyzed by this provider.

        :param search: Optional case-insensitive substring filter on
            track name, artist, or item_id.
        :param limit: Max results per page.
        :param offset: Pagination offset (ignored when search is set).
        """
        assert self.mass.music.database is not None
        rows = await self.mass.music.database.get_rows(
            "audio_analysis",
            {"aa_provider_domain": self.domain},
            limit=0,
        )
        seen: set[tuple[str, str]] = set()
        entries: list[tuple[str, str]] = []
        for row in rows:
            key = (row["item_id"], row["provider"])
            if key in seen:
                continue
            seen.add(key)
            entries.append(key)

        async def _resolve(item_id: str, provider: str) -> dict[str, Any]:
            try:
                t = await self.mass.music.tracks.get(item_id, provider)
                artists = ", ".join(a.name for a in getattr(t, "artists", []) or [])
                return {"item_id": item_id, "name": t.name, "artist": artists}
            except Exception:
                return {"item_id": item_id, "name": "(unknown)", "artist": ""}

        if search:
            resolved = await asyncio.gather(*[_resolve(iid, prov) for iid, prov in entries])
            q = search.lower()
            tracks = [
                t
                for t in resolved
                if q in t["name"].lower() or q in t["artist"].lower() or q in t["item_id"]
            ]
            total = len(tracks)
            page = tracks[offset : offset + limit]
        else:
            total = len(entries)
            page_entries = entries[offset : offset + limit]
            page = list(await asyncio.gather(*[_resolve(iid, prov) for iid, prov in page_entries]))

        return {"total": total, "offset": offset, "limit": limit, "items": page}

    async def _handle_export_analysis(
        self,
        limit: int = 100,
        offset: int = 0,
        random_pick: int = 0,
    ) -> dict[str, Any]:
        """Export analyzed tracks with their full scalar analysis data.

        :param limit: Max tracks per page.
        :param offset: Pagination offset (ignored when random_pick > 0).
        :param random_pick: When > 0, return a random sample of this size
            instead of an offset/limit page.
        """
        assert self.mass.music.database is not None
        rows = await self.mass.music.database.get_rows(
            "audio_analysis",
            {"aa_provider_domain": self.domain},
            limit=0,
        )

        export_fields = [
            "bpm",
            "key",
            "mode",
            "energy",
            "danceability",
            "loudness_integrated",
            "loudness_range",
            "true_peak",
            "brightness",
            "harmonic_complexity",
            "roughness",
            "rhythmic_regularity",
            "duration",
            "instrumentalness",
            "valence",
            "arousal",
            "acousticness",
        ]

        seen: set[tuple[str, str]] = set()
        all_entries: list[tuple[str, str, dict[str, Any]]] = []
        for row in rows:
            key = (row["item_id"], row["provider"])
            if key in seen:
                continue
            seen.add(key)
            try:
                data = AudioAnalysisData.from_dict(json.loads(row["analysis_data"]))
            except (json.JSONDecodeError, TypeError, KeyError):
                continue
            fields: dict[str, Any] = {}
            for field_name in export_fields:
                val = getattr(data, field_name, None)
                if val is not None:
                    fields[field_name] = round(val, 4) if isinstance(val, float) else val
            if data.extra_data:
                fields["extra_data"] = data.extra_data
            all_entries.append((row["item_id"], row["provider"], fields))

        total = len(all_entries)
        if random_pick > 0:
            import random  # noqa: PLC0415

            page_entries = random.sample(all_entries, min(random_pick, total))
        else:
            page_entries = all_entries[offset : offset + limit]

        async def _resolve(item_id: str, provider: str, fields: dict[str, Any]) -> dict[str, Any]:
            entry: dict[str, Any] = {
                "item_id": item_id,
                "provider": provider,
                "name": "(unknown)",
                "artist": "",
            }
            try:
                track = await self.mass.music.tracks.get(item_id, provider)
                entry["name"] = track.name
                entry["artist"] = ", ".join(a.name for a in getattr(track, "artists", []) or [])
            except Exception as err:
                self.logger.debug("Failed to resolve track %s/%s: %s", provider, item_id, err)
            entry.update(fields)
            return entry

        items = list(
            await asyncio.gather(*[_resolve(iid, prov, f) for iid, prov, f in page_entries])
        )
        return {"total": total, "offset": offset, "limit": limit, "items": items}

    async def _start_analysis(
        self,
        session_id: str,
        streamdetails: StreamDetails,
        audio_format: AudioFormat,
    ) -> bool:
        """Initialize a new sonic analysis session.

        :param session_id: Unique session ID created by the controller.
        :param streamdetails: Details about the stream being analyzed.
        :param audio_format: PCM format of the audio stream.
        """
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
        base = self._sessions[session_id]
        self._sessions[session_id] = SonicSessionData(
            streamdetails=base.streamdetails,
            audio_format=base.audio_format,
            block_samples=block_bytes,
            start_time=time.monotonic(),
        )
        self.logger.debug(
            "Started sonic analysis for %s/%s", streamdetails.provider, streamdetails.item_id
        )
        return True

    async def process_pcm_chunk(
        self,
        session_id: str,
        pcm_chunk: bytes,
    ) -> None:
        """Accumulate PCM and extract features when a 10-second block is full.

        :param session_id: The analysis session ID.
        :param pcm_chunk: Raw PCM audio data.
        """
        if session_id not in self._sessions:
            return
        session = self._sessions[session_id]
        assert isinstance(session, SonicSessionData)
        session.pcm_buffer.extend(pcm_chunk)
        af = session.audio_format
        while len(session.pcm_buffer) >= session.block_samples:
            block_bytes = bytes(session.pcm_buffer[: session.block_samples])
            del session.pcm_buffer[: session.block_samples]
            audio = _pcm_bytes_to_audio(block_bytes, af.sample_rate, af.bit_depth, af.channels)
            session.total_samples += len(audio)
            block_peak = float(np.max(np.abs(audio)))
            session.peak_absolute = max(session.peak_absolute, block_peak)
            session.waveform_peaks.append(block_peak)
            self._maybe_buffer_clap_audio(session, audio, af.sample_rate)
            if session.overlap is not None:
                audio = np.concatenate([session.overlap, audio])
            session.overlap = audio[-OVERLAP_SAMPLES:].copy()
            bf = await asyncio.to_thread(extract_block_features, audio, af.sample_rate)
            if bf is not None:
                merge_block_features(session.accumulated, bf)

    def _maybe_buffer_clap_audio(
        self, session: SonicSessionData, audio: np.ndarray, source_sr: int
    ) -> None:
        """Append up to CLAP_LIVE_BUFFER_SECONDS of audio to the session buffer.

        No-op when the CLAP model didn't load. Buffer is capped so long
        tracks/podcasts don't pin memory. We buffer from time-zero of the
        session (not from the 30s mark) because select_clap_window needs
        the earlier samples for its short-track fallback.
        """
        if self._clap_model is None:
            return
        limit = int(CLAP_LIVE_BUFFER_SECONDS * source_sr)
        remaining = limit - session.clap_audio_samples
        if remaining <= 0:
            return
        take = audio[:remaining] if len(audio) > remaining else audio
        session.clap_audio.append(np.asarray(take, dtype=np.float32))
        session.clap_audio_samples += len(take)

    async def _run_live_clap_if_eligible(
        self, session: SonicSessionData, analysis: AudioAnalysisData
    ) -> None:
        """Run CLAP on buffered live audio and populate scalars + extra_data embedding.

        Gated on: CLAP model loaded, source is a non-filesystem provider
        (filesystem tracks get CLAP via analyze_file instead), and at
        least 1 second of audio was buffered. Populates the 5 CLAP
        scalars directly on analysis and stores the 1024-dim embedding
        under extra_data["clap_embedding"]. Always clears the buffer
        before returning so memory is released regardless of outcome.
        """
        sd = session.streamdetails
        af = session.audio_format
        if (
            self._clap_model is None
            or sd.provider in FILESYSTEM_PROVIDER_DOMAINS
            or session.clap_audio_samples < af.sample_rate
        ):
            session.clap_audio.clear()
            session.clap_audio_samples = 0
            return
        buffered = (
            np.concatenate(session.clap_audio)
            if len(session.clap_audio) > 1
            else session.clap_audio[0]
        )
        window = select_clap_window(buffered, af.sample_rate)
        if window is None:
            session.clap_audio.clear()
            session.clap_audio_samples = 0
            return
        try:
            clap_start = time.monotonic()
            clap_scalars, clap_emb = await asyncio.to_thread(
                self._run_clap_inference, window, af.sample_rate
            )
            clap_elapsed = time.monotonic() - clap_start
        except Exception as err:
            self.logger.debug(
                "_finalize: live CLAP failed for %s/%s: %s",
                sd.provider,
                sd.item_id,
                err,
            )
        else:
            for field_name, value in clap_scalars.items():
                setattr(analysis, field_name, value)
            _store_clap_embedding(analysis, clap_emb)
            self.logger.debug(
                "_finalize: live CLAP for %s/%s (%.1fs buffered, %.2fs inference)",
                sd.provider,
                sd.item_id,
                session.clap_audio_samples / af.sample_rate,
                clap_elapsed,
            )
        session.clap_audio.clear()
        session.clap_audio_samples = 0

    async def _finalize(self, session_id: str) -> None:
        """Process remaining PCM, collapse features, and store analysis data.

        :param session_id: The analysis session ID.
        """
        if session_id not in self._sessions:
            return
        session = self._sessions[session_id]
        assert isinstance(session, SonicSessionData)
        sd = session.streamdetails
        af = session.audio_format

        # Flush any remaining PCM as a final partial block
        if session.pcm_buffer:
            audio = _pcm_bytes_to_audio(
                bytes(session.pcm_buffer), af.sample_rate, af.bit_depth, af.channels
            )
            session.total_samples += len(audio)
            block_peak = float(np.max(np.abs(audio)))
            session.peak_absolute = max(session.peak_absolute, block_peak)
            session.waveform_peaks.append(block_peak)
            self._maybe_buffer_clap_audio(session, audio, af.sample_rate)
            if session.overlap is not None:
                audio = np.concatenate([session.overlap, audio])
            bf = await asyncio.to_thread(extract_block_features, audio, af.sample_rate)
            if bf is not None:
                merge_block_features(session.accumulated, bf)
            session.pcm_buffer.clear()

        if not session.accumulated.rms_frames:
            self.logger.debug("No feature blocks for session %s, skipping", session_id)
            return

        analysis = await asyncio.to_thread(
            collapse_to_analysis, session.accumulated, af.sample_rate
        )

        # Fill in fields that need session-level state
        analysis.duration = session.total_samples / af.sample_rate
        if session.peak_absolute > 0:
            analysis.true_peak = float(20.0 * np.log10(session.peak_absolute))
        else:
            analysis.true_peak = -96.0

        # AudioAnalysisData has no wave_form field upstream — the per-block
        # waveform peaks accumulated in session.waveform_peaks aren't persisted
        # by this provider. Drop the dead computation to avoid mypy warnings
        # and the wasted allocation.

        await self._run_live_clap_if_eligible(session, analysis)

        await self.mass.streams.audio_analysis.set_audio_analysis(
            item_id=sd.item_id,
            provider_instance_id_or_domain=sd.provider,
            aa_provider_domain=self.domain,
            analysis=analysis,
            analysis_version=self.analysis_version,
            media_type=sd.media_type,
        )
        elapsed = time.monotonic() - session.start_time
        self.logger.debug(
            "Stored analysis for %s/%s (%.1fs elapsed)",
            sd.provider,
            sd.item_id,
            elapsed,
        )


    def _run_clap_inference(
        self,
        audio_np: np.ndarray,
        source_sr: int = ANALYZE_FILE_SAMPLE_RATE,
    ) -> tuple[dict[str, float], np.ndarray]:
        """Run CLAP inference on pre-loaded audio using the configured preset.

        :param audio_np: Mono float32 audio at source_sr.
        :param source_sr: Sample rate of audio_np.
        :returns: Tuple of (scalar dict, 1024-dim mean-pooled audio embedding).
        """
        preset = str(self.config.get_value(CONF_CLAP_SAMPLING, CLAP_SAMPLING_FAST))
        n_windows = CLAP_WINDOW_COUNTS.get(preset, 1)
        return run_clap_inference(
            self._clap_model,
            self._clap_text_embeddings,
            self._clap_prompt_order,
            audio_np,
            source_sr,
            n_windows,
        )
