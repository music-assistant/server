"""Tests for the Smart Fades audio analysis provider."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Generator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import numpy as np
import pytest
import torch
from music_assistant_models.enums import ContentType, MediaType
from music_assistant_models.errors import SetupFailedError
from music_assistant_models.media_items import AudioFormat
from torchaudio.transforms import SpectralCentroid

from music_assistant.models.audio_analysis import AudioAnalysisData, AudioAnalysisError
from music_assistant.providers.smart_fades.provider import ANALYSIS_SAMPLE_RATE, SmartFadesProvider
from music_assistant.providers.smart_fades.vocal_activity import (
    FIRERED_MEL_BINS,
    infer_firered_chunk,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures"
# Synthetic 120 BPM drum pattern (kick-hat-snare-hat): 44100 Hz, stereo, float32, ~15.7s
# 8 bars of 4 beats = 32 beats, downbeats on every 4th beat (kick).
# Clip ends 0.2s after the last beat to avoid end-of-track hallucination.
FIXTURE_PCM = FIXTURE_DIR / "test_120bpm_44100_2_32.pcm"

# Expected beat grid for the deterministic 120 BPM fixture.
EXPECTED_BEATS = [
    0.000,
    0.500,
    1.000,
    1.500,
    2.020,
    2.500,
    3.000,
    3.500,
    4.020,
    4.500,
    5.020,
    5.500,
    6.020,
    6.500,
    7.000,
    7.520,
    8.020,
    8.500,
    9.000,
    9.520,
    10.020,
    10.500,
    11.000,
    11.500,
    12.020,
    12.500,
    13.020,
    13.520,
    14.020,
    14.500,
    15.020,
    15.520,
]

EXPECTED_DOWNBEATS = [
    0.000,
    2.020,
    4.020,
    6.020,
    8.020,
    10.020,
    12.020,
    14.020,
]


@pytest.fixture
def mass_mock() -> Mock:
    """Return a mock MusicAssistant instance."""
    mass = Mock()
    mass.cache = Mock()
    mass.cache.get = AsyncMock(return_value=None)
    mass.cache.set = AsyncMock()
    mass.music = Mock()
    mass.streams = Mock()
    mass.streams.audio_analysis = Mock()
    mass.streams.audio_analysis.get_audio_analysis_version = AsyncMock(return_value=None)
    mass.streams.audio_analysis.set_audio_analysis = AsyncMock()
    mass.config = Mock()
    mass.config.get = Mock(return_value={})
    return mass


@pytest.fixture
def manifest_mock() -> Mock:
    """Return a mock provider manifest."""
    manifest = Mock()
    manifest.domain = "smart_fades"
    manifest.name = "Smart Fades"
    return manifest


@pytest.fixture
def config_mock() -> Mock:
    """Return a mock provider config."""
    config = Mock()
    config.instance_id = "smart_fades_test"
    config.name = "Smart Fades Test"
    config.enabled = True
    config.get_value = Mock(return_value="GLOBAL")
    config.values = {}
    return config


@pytest.fixture
async def provider(mass_mock: Mock, manifest_mock: Mock, config_mock: Mock) -> SmartFadesProvider:
    """Return a SmartFadesProvider without loading external model assets."""
    prov = SmartFadesProvider(mass_mock, manifest_mock, config_mock, set())
    with patch.object(prov, "_load_models", new=AsyncMock()):
        await prov.handle_async_init()
    prov._spectral_centroid = SpectralCentroid(
        sample_rate=ANALYSIS_SAMPLE_RATE,
        hop_length=512,
    )
    prov._firered_model = Mock()
    prov._firered_cmvn_means = np.zeros(FIRERED_MEL_BINS, dtype=np.float64)
    prov._firered_cmvn_inverse_std = np.ones(FIRERED_MEL_BINS, dtype=np.float64)
    return prov


@pytest.fixture
def feature_provider(provider: SmartFadesProvider) -> Generator[SmartFadesProvider]:
    """Return a provider with model-backed feature extraction disabled."""
    with (
        patch.object(provider, "_compute_musical_key_features"),
        patch.object(provider, "_compute_vocal_features"),
    ):
        yield provider


@pytest.fixture
def deterministic_provider(
    feature_provider: SmartFadesProvider,
) -> Generator[SmartFadesProvider]:
    """Return a provider with deterministic model inference."""
    with (
        patch.object(
            feature_provider,
            "_infer_beat_timings",
            return_value=(
                np.asarray(EXPECTED_BEATS, dtype=np.float32),
                np.asarray(EXPECTED_DOWNBEATS, dtype=np.float32),
                4,
            ),
        ),
        patch.object(
            feature_provider,
            "_infer_musical_key",
            return_value=("C", "major"),
        ),
        patch.object(
            feature_provider,
            "_infer_vocal_activity",
            new=AsyncMock(return_value=np.zeros(2, dtype=np.float32)),
        ),
    ):
        yield feature_provider


async def test_beat_analysis_pipeline(
    deterministic_provider: SmartFadesProvider,
    mass_mock: Mock,
) -> None:
    """Test that the provider stores beats and downbeats from a PCM fixture."""
    audio_format = AudioFormat(
        content_type=ContentType.PCM_F32LE,
        bit_depth=32,
        sample_rate=44100,
        channels=2,
    )

    stream_details = Mock()
    stream_details.item_id = "test_120bpm"
    stream_details.provider = "test"
    stream_details.queue_id = "test"
    stream_details.uri = "test://120bpm"
    stream_details.media_type = MediaType.TRACK
    stream_details.duration = 120

    session_id = "test:test:test_120bpm"
    await deterministic_provider.start_analysis(session_id, stream_details, audio_format)

    # Feed PCM in 1-second chunks (matching the real streaming pipeline)
    pcm_data = FIXTURE_PCM.read_bytes()
    chunk_size = 44100 * 2 * 4  # 1 second at 44100 Hz, stereo, float32
    offset = 0
    while offset < len(pcm_data):
        chunk = pcm_data[offset : offset + chunk_size]
        await deterministic_provider.process_pcm_chunk(session_id, chunk)
        offset += chunk_size

    await deterministic_provider.finalize(session_id)

    # Verify set_audio_analysis was called with correct data
    set_aa_mock = mass_mock.streams.audio_analysis.set_audio_analysis
    set_aa_mock.assert_awaited_once()
    analysis = set_aa_mock.call_args.kwargs["analysis"]

    beats = analysis.beats
    downbeats = analysis.downbeats

    assert len(beats) == len(EXPECTED_BEATS), (
        f"Expected {len(EXPECTED_BEATS)} beats, got {len(beats)}"
    )
    assert len(downbeats) == len(EXPECTED_DOWNBEATS), (
        f"Expected {len(EXPECTED_DOWNBEATS)} downbeats, got {len(downbeats)}"
    )

    # All beats must be within 20ms (1 frame at 50fps) of expected values
    for i, (actual, expected) in enumerate(zip(beats, EXPECTED_BEATS, strict=True)):
        assert abs(float(actual) - expected) < 0.021, (
            f"Beat {i}: expected {expected:.3f}s, got {float(actual):.3f}s"
        )

    for i, (actual, expected) in enumerate(zip(downbeats, EXPECTED_DOWNBEATS, strict=True)):
        assert abs(float(actual) - expected) < 0.021, (
            f"Downbeat {i}: expected {expected:.3f}s, got {float(actual):.3f}s"
        )

    # Verify BPM is close to 120
    assert analysis.bpm is not None
    assert 115 < analysis.bpm < 125, f"Expected BPM ~120, got {analysis.bpm:.1f}"

    # the 120 BPM fixture is common time
    assert analysis.beats_per_bar == 4


async def test_extended_analysis_fields(
    deterministic_provider: SmartFadesProvider,
    mass_mock: Mock,
) -> None:
    """Test that extended analysis fields (energy, centroid, key) are populated."""
    audio_format = AudioFormat(
        content_type=ContentType.PCM_F32LE,
        bit_depth=32,
        sample_rate=44100,
        channels=2,
    )

    stream_details = Mock()
    stream_details.item_id = "test_120bpm"
    stream_details.provider = "test"
    stream_details.queue_id = "test"
    stream_details.uri = "test://120bpm"
    stream_details.media_type = MediaType.TRACK
    stream_details.duration = 120

    session_id = "test:test:test_120bpm_extended"
    await deterministic_provider.start_analysis(session_id, stream_details, audio_format)

    pcm_data = FIXTURE_PCM.read_bytes()
    chunk_size = 44100 * 2 * 4  # 1 second at 44100 Hz, stereo, float32
    offset = 0
    while offset < len(pcm_data):
        chunk = pcm_data[offset : offset + chunk_size]
        await deterministic_provider.process_pcm_chunk(session_id, chunk)
        offset += chunk_size

    await deterministic_provider.finalize(session_id)

    set_aa_mock = mass_mock.streams.audio_analysis.set_audio_analysis
    analysis = set_aa_mock.call_args.kwargs["analysis"]
    assert set_aa_mock.call_args.kwargs["analysis_version"] == 3

    # Energy curve should be 1800 bins, normalized to [0, 1]
    assert analysis.rms_energy is not None
    assert len(analysis.rms_energy) == 1800
    assert max(analysis.rms_energy) <= 1.0
    assert min(analysis.rms_energy) >= 0.0

    # Spectral centroid should be 1800 bins with positive Hz values
    assert analysis.spectral_centroid is not None
    assert len(analysis.spectral_centroid) == 1800
    assert all(v >= 0 for v in analysis.spectral_centroid)

    # Musical key should be detected
    assert analysis.key is not None
    assert analysis.key in [
        "C",
        "C#",
        "D",
        "D#",
        "E",
        "F",
        "F#",
        "G",
        "G#",
        "A",
        "A#",
        "B",
        "Bb",
    ]
    assert analysis.mode in ["major", "minor"]

    # BPM and beats should still be correct
    assert analysis.bpm is not None
    assert 115 < analysis.bpm < 125

    # v2: per-band RMS envelopes in extra_data, normalized by the full-band peak
    assert analysis.extra_data is not None
    band_rms = analysis.extra_data["band_rms"]
    assert set(band_rms) == {"low", "low_mid", "mid", "high"}
    for band in band_rms.values():
        assert len(band) == 1800
        assert all(v >= 0.0 for v in band)
    # music with drums has real low-band content; bands are lists (JSON-safe)
    assert isinstance(band_rms["low"], list)
    assert max(band_rms["low"]) > 0.05

    vocal_probabilities = analysis.extra_data["vocal_activity"]
    assert len(vocal_probabilities) == 1800
    assert all(math.isfinite(value) and 0.0 <= value <= 1.0 for value in vocal_probabilities)


async def test_finalize_returns_audio_analysis_data(
    deterministic_provider: SmartFadesProvider,
) -> None:
    """Test that _finalize returns an AudioAnalysisData on success."""
    audio_format = AudioFormat(
        content_type=ContentType.PCM_F32LE,
        bit_depth=32,
        sample_rate=44100,
        channels=2,
    )

    stream_details = Mock()
    stream_details.item_id = "test_finalize_return"
    stream_details.provider = "test"
    stream_details.queue_id = "test"
    stream_details.uri = "test://finalize_return"
    stream_details.media_type = MediaType.TRACK
    stream_details.duration = 120

    session_id = "test:test:test_finalize_return"
    await deterministic_provider.start_analysis(session_id, stream_details, audio_format)

    pcm_data = FIXTURE_PCM.read_bytes()
    chunk_size = 44100 * 2 * 4
    offset = 0
    while offset < len(pcm_data):
        chunk = pcm_data[offset : offset + chunk_size]
        await deterministic_provider.process_pcm_chunk(session_id, chunk)
        offset += chunk_size

    result = await deterministic_provider._finalize(session_id)

    assert isinstance(result, AudioAnalysisData)


async def test_finalize_raises_when_not_enough_beats(
    feature_provider: SmartFadesProvider,
) -> None:
    """Test that _finalize raises AudioAnalysisError when not enough beats are detected."""
    audio_format = AudioFormat(
        content_type=ContentType.PCM_F32LE,
        bit_depth=32,
        sample_rate=44100,
        channels=2,
    )

    stream_details = Mock()
    stream_details.item_id = "test_finalize_none"
    stream_details.provider = "test"
    stream_details.queue_id = "test"
    stream_details.uri = "test://finalize_none"
    stream_details.media_type = MediaType.TRACK
    stream_details.duration = 120

    session_id = "test:test:test_finalize_none"
    await feature_provider.start_analysis(session_id, stream_details, audio_format)

    pcm_data = FIXTURE_PCM.read_bytes()
    chunk_size = 44100 * 2 * 4
    offset = 0
    while offset < len(pcm_data):
        chunk = pcm_data[offset : offset + chunk_size]
        await feature_provider.process_pcm_chunk(session_id, chunk)
        offset += chunk_size

    # Patch _infer_beat_timings to return fewer than 2 beats → deterministic skip
    with (
        patch.object(
            feature_provider,
            "_infer_beat_timings",
            return_value=(np.array([0.5]), np.array([]), 4),
        ),
        pytest.raises(AudioAnalysisError, match="beat"),
    ):
        await feature_provider._finalize(session_id)


async def test_digital_silence_yields_finite_spectral_centroid(
    provider: SmartFadesProvider,
) -> None:
    """Digitally-silent audio yields 0 Hz centroid frames instead of non-finite values."""
    sample_rate = 22050
    tone = np.sin(2 * np.pi * 440 * np.arange(sample_rate, dtype=np.float32) / sample_rate)
    pcm = np.concatenate([tone.astype(np.float32), np.zeros(sample_rate, dtype=np.float32)])
    data = Mock()
    data.energy_chunks = []
    data.frequency_band_chunks = {}
    data.centroid_chunks = []

    provider._compute_energy_and_spectral_centroids(pcm, data)

    assert data.centroid_chunks
    assert np.isfinite(np.concatenate(data.centroid_chunks)).all()


async def test_setup_raises_when_requirements_not_met(
    mass_mock: Mock, manifest_mock: Mock, config_mock: Mock
) -> None:
    """setup() fails (before importing the heavy provider module) when requirements aren't met."""
    from music_assistant.providers import smart_fades  # noqa: PLC0415

    with (
        patch(
            "music_assistant.providers.smart_fades.verify_system_meets_requirements",
            side_effect=SetupFailedError("unsupported system"),
        ),
        pytest.raises(SetupFailedError),
    ):
        await smart_fades.setup(mass_mock, manifest_mock, config_mock)


def test_initialize_models_uses_expected_components(provider: SmartFadesProvider) -> None:
    """Model initialization wires the expected local and third-party components."""
    beat_model = Mock()
    original_beat_module = beat_model.model
    quantized_beat_module = Mock()
    beat_post_processor = Mock()
    skey_components = (Mock(), Mock(), Mock())
    spectral_centroid = Mock()
    firered_components = (
        Mock(),
        np.zeros(FIRERED_MEL_BINS, dtype=np.float64),
        np.ones(FIRERED_MEL_BINS, dtype=np.float64),
    )

    with (
        patch(
            "music_assistant.providers.smart_fades.provider.Spect2Frames",
            return_value=beat_model,
        ) as spect2frames,
        patch(
            "music_assistant.providers.smart_fades.provider.torch.ao.quantization.quantize_dynamic",
            return_value=quantized_beat_module,
        ) as quantize_dynamic,
        patch(
            "music_assistant.providers.smart_fades.provider.DBNDownBeatTracker",
            return_value=beat_post_processor,
        ),
        patch(
            "music_assistant.providers.smart_fades.provider.load_skey_components",
            return_value=skey_components,
        ),
        patch(
            "music_assistant.providers.smart_fades.provider.SpectralCentroid",
            return_value=spectral_centroid,
        ),
        patch(
            "music_assistant.providers.smart_fades.provider.load_firered_components",
            return_value=firered_components,
        ),
    ):
        components = provider._initialize_models()

    spect2frames.assert_called_once_with(checkpoint_path="small0", device="cpu")
    quantize_dynamic.assert_called_once_with(
        original_beat_module,
        {torch.nn.Linear},
        dtype=torch.qint8,
    )
    assert beat_model.model is quantized_beat_module
    assert components == (
        beat_model,
        beat_post_processor,
        *skey_components,
        spectral_centroid,
        *firered_components,
    )


async def test_analysis_version_is_3(provider: SmartFadesProvider) -> None:
    """v3 adds FireRed AED vocal activity."""
    assert provider.analysis_version == 3


async def test_cancel_clears_all_session_state(provider: SmartFadesProvider) -> None:
    """Cancellation releases every retained PCM and feature block."""
    audio_format = AudioFormat(
        content_type=ContentType.PCM_F32LE,
        bit_depth=32,
        sample_rate=44100,
        channels=2,
    )
    stream_details = Mock(
        item_id="cancel",
        provider="test",
        media_type=MediaType.TRACK,
        duration=120,
    )
    session_id = "test:test:cancel"
    await provider.start_analysis(session_id, stream_details, audio_format)
    data = provider._data[session_id]
    data.pcm_buffer.append(np.ones(10, dtype=np.float32))
    data.beats_feature_blocks.append(np.ones((1, 128), dtype=np.float32))
    data.energy_chunks.append(np.ones(1, dtype=np.float32))
    data.centroid_chunks.append(np.ones(1, dtype=np.float32))
    data.frequency_band_chunks["low"] = [np.ones(1, dtype=np.float32)]
    data.musical_key_feature_blocks.append(torch.ones((1, 1, 84, 1)))
    data.vocal_feature_blocks.append(np.ones((1, 80), dtype=np.float32))

    await provider.cancel(session_id)

    assert session_id not in provider._data
    assert not data.pcm_buffer
    assert not data.beats_feature_blocks
    assert not data.energy_chunks
    assert not data.centroid_chunks
    assert not data.frequency_band_chunks
    assert not data.musical_key_feature_blocks
    assert not data.vocal_feature_blocks
    assert data.resampler is None
    assert data.vocal_resampler is None
    assert data.vocal_fbank is None


async def test_finalize_error_clears_all_session_state(
    provider: SmartFadesProvider,
) -> None:
    """Finalization errors release the popped session state."""
    audio_format = AudioFormat(
        content_type=ContentType.PCM_F32LE,
        bit_depth=32,
        sample_rate=22050,
        channels=1,
    )
    stream_details = Mock(
        item_id="finalize_error",
        provider="test",
        media_type=MediaType.TRACK,
        duration=120,
    )
    session_id = "test:test:finalize_error"
    await provider.start_analysis(session_id, stream_details, audio_format)
    data = provider._data[session_id]
    data.beats_feature_blocks.append(np.ones((1, 128), dtype=np.float32))

    with (
        patch.object(provider, "_process_block", new=AsyncMock()),
        patch.object(data.features, "finalize", new=AsyncMock(return_value=np.empty((0, 128)))),
        patch.object(
            provider,
            "_run_final_inference",
            new=AsyncMock(side_effect=RuntimeError("inference failed")),
        ),
        pytest.raises(RuntimeError, match="inference failed"),
    ):
        await provider._finalize(session_id)

    assert session_id not in provider._data
    assert not data.beats_feature_blocks
    assert data.vocal_fbank is None


@pytest.mark.parametrize(("frame_count", "model_calls"), [(100, 1), (30_001, 2)])
async def test_vocal_inference_uses_bounded_model_calls(
    provider: SmartFadesProvider,
    frame_count: int,
    model_calls: int,
) -> None:
    """Normal inputs use one model call while long inputs use bounded chunks."""
    features = np.zeros((frame_count, 80), dtype=np.float32)

    with patch(
        "music_assistant.providers.smart_fades.provider.infer_firered_chunk",
        side_effect=lambda _model, chunk, _device: np.zeros((len(chunk), 3), dtype=np.float32),
    ) as infer:
        probabilities = await provider._infer_vocal_activity(features, frame_count / 100)

    assert infer.call_count == model_calls
    assert len(probabilities) == math.ceil((frame_count / 100) / 0.1)


async def test_vocal_inference_starts_before_beat_finishes(
    provider: SmartFadesProvider,
) -> None:
    """FireRed starts concurrently while key inference remains behind beat inference."""
    beat_started = asyncio.Event()
    vocal_started = asyncio.Event()
    beat_finished = False

    async def run_offloaded(func: Callable[..., object], *args: object) -> object:
        nonlocal beat_finished
        if func.__name__ == "_infer_beat_timings":
            beat_started.set()
            await asyncio.wait_for(vocal_started.wait(), timeout=1)
            beat_finished = True
            return np.array([0.0, 0.5]), np.array([0.0]), 4
        if func is infer_firered_chunk:
            assert beat_started.is_set()
            vocal_started.set()
            features = args[1]
            assert isinstance(features, np.ndarray)
            return np.zeros((len(features), 3), dtype=np.float32)
        if func.__name__ == "_infer_musical_key":
            assert beat_finished
            return "C", "major"
        raise AssertionError(f"unexpected offload: {func}")

    async def run_offloaded_timed(func: Callable[..., object], *args: object) -> object:
        return await run_offloaded(func, *args), 0.0

    with (
        patch.object(provider, "_run_offloaded", side_effect=run_offloaded),
        patch.object(provider, "_run_offloaded_timed", side_effect=run_offloaded_timed),
    ):
        beat_key, vocal = await provider._run_final_inference(
            np.zeros((10, 128), dtype=np.float32),
            None,
            np.zeros((10, 80), dtype=np.float32),
            0.1,
        )

    assert vocal_started.is_set()
    assert beat_key[3:] == ("C", "major")
    assert vocal.shape == (1,)


async def test_final_inference_cancellation_stops_long_vocal_loop(
    provider: SmartFadesProvider,
) -> None:
    """Cancelling finalization prevents dispatch of later FireRed chunks."""
    vocal_started = asyncio.Event()
    never_finish = asyncio.Event()
    vocal_calls = 0

    async def run_offloaded(func: Callable[..., object], *_args: object) -> object:
        nonlocal vocal_calls
        if func is infer_firered_chunk:
            vocal_calls += 1
            vocal_started.set()
        await never_finish.wait()
        raise AssertionError("unreachable")

    async def run_offloaded_timed(func: Callable[..., object], *args: object) -> object:
        return await run_offloaded(func, *args), 0.0

    with (
        patch.object(provider, "_run_offloaded", side_effect=run_offloaded),
        patch.object(provider, "_run_offloaded_timed", side_effect=run_offloaded_timed),
    ):
        task = asyncio.create_task(
            provider._run_final_inference(
                np.zeros((10, 128), dtype=np.float32),
                None,
                np.zeros((60_001, 80), dtype=np.float32),
                600.01,
            )
        )
        await asyncio.wait_for(vocal_started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert vocal_calls == 1


async def test_beat_failure_cancels_vocal_inference(
    provider: SmartFadesProvider,
) -> None:
    """An early beat failure cancels the FireRed branch instead of awaiting it."""
    vocal_started = asyncio.Event()
    vocal_cancelled = asyncio.Event()
    never_finish = asyncio.Event()

    async def run_offloaded(func: Callable[..., object], *_args: object) -> object:
        if func.__name__ == "_infer_beat_timings":
            # Fail only once the vocal branch is in flight, so cancellation is observable.
            await asyncio.wait_for(vocal_started.wait(), timeout=1)
            return np.array([0.0]), np.array([]), 0
        if func is infer_firered_chunk:
            vocal_started.set()
            try:
                await never_finish.wait()
            except asyncio.CancelledError:
                vocal_cancelled.set()
                raise
        raise AssertionError(f"unexpected offload: {func}")

    async def run_offloaded_timed(func: Callable[..., object], *args: object) -> object:
        return await run_offloaded(func, *args), 0.0

    with (
        patch.object(provider, "_run_offloaded", side_effect=run_offloaded),
        patch.object(provider, "_run_offloaded_timed", side_effect=run_offloaded_timed),
        pytest.raises(AudioAnalysisError, match="no rhythmic beat"),
    ):
        await asyncio.wait_for(
            provider._run_final_inference(
                np.zeros((10, 128), dtype=np.float32),
                None,
                np.zeros((10, 80), dtype=np.float32),
                0.1,
            ),
            timeout=1,
        )

    assert vocal_cancelled.is_set()


async def test_vocal_worker_keeps_local_state_during_cancel_cleanup(
    provider: SmartFadesProvider,
) -> None:
    """A worker keeps valid resampler and fbank references while session fields clear."""
    data = Mock()
    data.vocal_feature_blocks = []
    fbank = Mock()
    fbank.process.return_value = np.ones((1, 80), dtype=np.float32)
    fbank.finalize.return_value = np.ones((1, 80), dtype=np.float32)
    resampler = Mock()

    def resample_chunk(pcm: np.ndarray, _last: bool) -> np.ndarray:
        data.vocal_resampler = None
        data.vocal_fbank = None
        return pcm

    resampler.resample_chunk.side_effect = resample_chunk
    data.vocal_resampler = resampler
    data.vocal_fbank = fbank

    provider._compute_vocal_features(
        np.ones(1600, dtype=np.float32),
        data,
        True,
    )

    assert len(data.vocal_feature_blocks) == 2
    fbank.process.assert_called_once()
    fbank.finalize.assert_called_once()


async def test_vocal_inference_failure_is_retryable(provider: SmartFadesProvider) -> None:
    """A torch/hardware FireRed failure records a retryable error, never a permanent row."""
    features = np.zeros((100, 80), dtype=np.float32)

    with (
        patch.object(
            provider,
            "_run_offloaded",
            new=AsyncMock(side_effect=RuntimeError("torch kernel crashed")),
        ),
        pytest.raises(AudioAnalysisError, match="FireRed vocal inference failed") as excinfo,
    ):
        await provider._infer_vocal_activity(features, 1.0)

    assert excinfo.value.retry_at is not None
    assert excinfo.value.retry_at > datetime.now(UTC)


async def test_vocal_inference_with_unloaded_model_is_retryable(
    provider: SmartFadesProvider,
) -> None:
    """The idle-unload race (models freed mid-finalize) must not poison the track forever."""
    provider._firered_model = None

    with pytest.raises(AudioAnalysisError) as excinfo:
        await provider._infer_vocal_activity(np.zeros((10, 80), dtype=np.float32), 0.1)

    assert excinfo.value.retry_at is not None
