"""Tests for the live-CLAP finalize + cancel paths."""

from __future__ import annotations

import asyncio
import math
from unittest.mock import MagicMock

import numpy as np
import pytest

from music_assistant.helpers.datetime import utc
from music_assistant.models.audio_analysis import AudioAnalysisData, AudioAnalysisError
from music_assistant.providers.sonic_analysis import (
    MODEL_FAILURE_RETRY_DELAY,
    SonicAnalysisProvider,
    SonicSessionData,
)
from music_assistant.providers.sonic_analysis.clap_prompts import (
    CALIBRATION,
    SCALAR_PROMPT_PAIRS,
)


def _make_provider() -> tuple[SonicAnalysisProvider, MagicMock]:
    """Stub provider with the prompt order needed by _run_live_clap_if_eligible."""
    p = SonicAnalysisProvider.__new__(SonicAnalysisProvider)
    fake_logger = MagicMock()
    p.logger = fake_logger
    p._clap_prompt_order = list(SCALAR_PROMPT_PAIRS.items())
    return p, fake_logger


def _make_session(target_starts: list[int] | None = None) -> SonicSessionData:
    starts = list(target_starts) if target_starts is not None else []
    return SonicSessionData(
        streamdetails=MagicMock(),
        audio_format=MagicMock(),
        clap_target_starts=starts,
        clap_target_buffers=[[] for _ in starts],
        clap_target_complete=[False] * len(starts),
    )


@pytest.mark.asyncio
async def test_no_targets_short_circuits_silently() -> None:
    """A session with no planned targets returns immediately, no log noise."""
    p, fake_logger = _make_provider()
    session = _make_session(target_starts=[])
    analysis = AudioAnalysisData()

    await p._run_live_clap_if_eligible(session, analysis)

    assert analysis.danceability is None
    assert analysis.valence is None
    assert analysis.arousal is None
    assert analysis.instrumentalness is None
    assert analysis.acousticness is None
    assert analysis.speechiness is None
    assert analysis.extra_data is None or "clap_embedding" not in (analysis.extra_data or {})
    fake_logger.warning.assert_not_called()


@pytest.mark.asyncio
async def test_no_completions_raises_retryable() -> None:
    """Targets planned but zero windows completed → retryable failure, no scalar updates."""
    p, fake_logger = _make_provider()
    session = _make_session(target_starts=[0, 100, 200])
    # No tasks added, no completed_count incremented
    analysis = AudioAnalysisData()

    before = utc()
    with pytest.raises(AudioAnalysisError) as excinfo:
        await p._run_live_clap_if_eligible(session, analysis)

    assert excinfo.value.retry_at is not None
    assert excinfo.value.retry_at >= before + MODEL_FAILURE_RETRY_DELAY

    assert analysis.danceability is None
    assert analysis.valence is None
    assert analysis.arousal is None
    assert analysis.instrumentalness is None
    assert analysis.acousticness is None
    assert analysis.speechiness is None
    assert analysis.extra_data is None or "clap_embedding" not in (analysis.extra_data or {})
    fake_logger.warning.assert_called_once()


@pytest.mark.asyncio
async def test_partial_completions_raises_retryable() -> None:
    """Some but not all planned windows completed → retryable failure, nothing written."""
    p, fake_logger = _make_provider()
    session = _make_session(target_starts=[0, 100, 200])

    n_pairs = len(SCALAR_PROMPT_PAIRS)
    session.clap_completed_count = 2
    session.clap_sum_embedding = np.ones(1024, dtype=np.float32)
    session.clap_sum_similarities = np.zeros(2 * n_pairs, dtype=np.float32)

    analysis = AudioAnalysisData()

    with pytest.raises(AudioAnalysisError, match="2 of 3"):
        await p._run_live_clap_if_eligible(session, analysis)

    assert analysis.danceability is None
    assert analysis.extra_data is None or "clap_embedding" not in (analysis.extra_data or {})
    fake_logger.warning.assert_called_once()


@pytest.mark.asyncio
async def test_mean_pools_and_calibrates_scalars() -> None:
    """Three completed windows with known sums produce calibrated scalars and L2-normalized embedding."""
    p, _ = _make_provider()
    session = _make_session(target_starts=[0, 100, 200])

    n_pairs = len(SCALAR_PROMPT_PAIRS)
    # Sums equivalent to mean_emb = 0.5 (pre-norm), mean_sim = [1.0, 0.0, 1.0, 0.0, ...]
    session.clap_completed_count = 3
    session.clap_sum_embedding = np.full(1024, 1.5, dtype=np.float32)  # mean = 0.5
    raw_sims = np.zeros(2 * n_pairs, dtype=np.float32)
    for i in range(n_pairs):
        raw_sims[i * 2] = 3.0  # pos_logit mean = 1.0
        raw_sims[i * 2 + 1] = 0.0  # neg_logit mean = 0.0
    session.clap_sum_similarities = raw_sims

    analysis = AudioAnalysisData()
    await p._run_live_clap_if_eligible(session, analysis)

    # Embedding: pre-norm = 0.5 across 1024 dims; ||v|| = sqrt(1024 * 0.25) = 16.0; normalized = 1/32
    assert analysis.extra_data is not None
    emb = np.asarray(analysis.extra_data["clap_embedding"], dtype=np.float32)
    expected_norm = math.sqrt(1024 * 0.25)
    expected_value = 0.5 / expected_norm
    np.testing.assert_array_almost_equal(emb, np.full(1024, expected_value), decimal=5)

    for scalar_name in SCALAR_PROMPT_PAIRS:
        a, b = CALIBRATION[scalar_name]
        expected = 1.0 / (1.0 + math.exp(-(a * 1.0 + b)))
        assert getattr(analysis, scalar_name) == pytest.approx(expected)


@pytest.mark.asyncio
async def test_awaits_pending_tasks() -> None:
    """Tasks still in flight are awaited before the mean-pool runs."""
    p, _ = _make_provider()
    session = _make_session(target_starts=[0])

    completion_event = asyncio.Event()

    async def slow_inference() -> None:
        await completion_event.wait()
        n_pairs = len(SCALAR_PROMPT_PAIRS)
        session.clap_sum_embedding = np.ones(1024, dtype=np.float32)
        session.clap_sum_similarities = np.zeros(2 * n_pairs, dtype=np.float32)
        session.clap_completed_count = 1

    task = asyncio.create_task(slow_inference())
    session.clap_inference_tasks.append(task)

    finalize_task = asyncio.create_task(p._run_live_clap_if_eligible(session, AudioAnalysisData()))
    # Yield once: finalize should be blocked on the gather
    await asyncio.sleep(0)
    assert not finalize_task.done()

    # Release the inference task
    completion_event.set()
    await finalize_task
    assert task.done()


@pytest.mark.asyncio
async def test_cancel_aborts_pending_tasks_and_clears_buffers() -> None:
    """Cancel cancels in-flight inferences and resets per-window buffers."""
    p = SonicAnalysisProvider.__new__(SonicAnalysisProvider)
    p.logger = MagicMock()
    p._sessions = {}

    session = _make_session(target_starts=[0, 100])
    session.clap_target_buffers = [
        [np.zeros(1024, dtype=np.float32)],
        [np.zeros(2048, dtype=np.float32)],
    ]

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def long_running() -> None:
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    task = asyncio.create_task(long_running())
    session.clap_inference_tasks.append(task)
    await started.wait()

    p._sessions["sess"] = session
    await p.cancel("sess")

    # Give the cancellation a tick to propagate
    await asyncio.sleep(0)
    assert task.cancelled() or cancelled.is_set()
    assert session.clap_target_buffers == []
