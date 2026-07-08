"""Tests for the per-window CLAP inference + session accumulator logic."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import torch

from music_assistant.providers.sonic_analysis import (
    CLAP_WINDOW_SECONDS,
    SonicAnalysisProvider,
    SonicSessionData,
)

SR = 22050
WINDOW_SAMPLES = CLAP_WINDOW_SECONDS * SR


def _make_provider(
    embedding_value: float = 0.5,
    similarity_row: list[float] | None = None,
) -> tuple[SonicAnalysisProvider, MagicMock, MagicMock]:
    """
    Stub provider with a mocked CLAP model returning predictable tensors.

    :returns: (provider, fake_model_mock, fake_logger_mock).
    """
    p = SonicAnalysisProvider.__new__(SonicAnalysisProvider)
    fake_logger = MagicMock()
    p.logger = fake_logger
    # _run_offloaded reads mass.streams.audio_analysis.analysis_semaphore; a plain Mock
    # attribute is not an asyncio.Semaphore, so offloads fall back to a plain worker thread.
    p.mass = MagicMock()

    fake_model = MagicMock()
    sim = similarity_row if similarity_row is not None else [1.0, 2.0, 3.0, 4.0]
    fake_model.get_audio_embeddings_from_tensor = MagicMock(
        return_value=torch.full((1, 1024), embedding_value, dtype=torch.float32)
    )
    fake_model.compute_similarity = MagicMock(return_value=torch.tensor([sim], dtype=torch.float32))

    p._clap_model = fake_model
    p._clap_text_embeddings = MagicMock()
    return p, fake_model, fake_logger


def _make_session() -> SonicSessionData:
    return SonicSessionData(streamdetails=MagicMock(), audio_format=MagicMock())


@pytest.mark.asyncio
async def test_first_completion_initializes_sums() -> None:
    """First successful inference allocates zero-arrays for the running sums."""
    p, _, _ = _make_provider(embedding_value=0.5)
    session = _make_session()
    window = np.zeros(WINDOW_SAMPLES, dtype=np.float32)

    await p._run_single_clap_window(session, window, SR)

    assert session.clap_sum_embedding is not None
    assert session.clap_sum_embedding.shape == (1024,)
    assert session.clap_sum_similarities is not None
    assert session.clap_sum_similarities.shape == (4,)
    assert session.clap_completed_count == 1


@pytest.mark.asyncio
async def test_subsequent_completions_accumulate() -> None:
    """Three calls produce running sums equal to 3x the per-call output."""
    p, _, _ = _make_provider(embedding_value=0.5, similarity_row=[1.0, 2.0, 3.0, 4.0])
    session = _make_session()
    window = np.zeros(WINDOW_SAMPLES, dtype=np.float32)

    for _ in range(3):
        await p._run_single_clap_window(session, window, SR)

    assert session.clap_completed_count == 3
    assert session.clap_sum_embedding is not None
    assert session.clap_sum_similarities is not None
    np.testing.assert_array_almost_equal(
        session.clap_sum_embedding, np.full(1024, 1.5, dtype=np.float32)
    )
    np.testing.assert_array_almost_equal(
        session.clap_sum_similarities, np.array([3.0, 6.0, 9.0, 12.0], dtype=np.float32)
    )


@pytest.mark.asyncio
async def test_clap_failure_logs_and_skips_accumulation() -> None:
    """A CLAP exception logs at debug, leaves the session sums untouched."""
    p, fake_model, fake_logger = _make_provider()
    fake_model.get_audio_embeddings_from_tensor = MagicMock(side_effect=RuntimeError("boom"))
    session = _make_session()
    window = np.zeros(WINDOW_SAMPLES, dtype=np.float32)

    await p._run_single_clap_window(session, window, SR)

    assert session.clap_completed_count == 0
    assert session.clap_sum_embedding is None
    assert session.clap_sum_similarities is None
    fake_logger.debug.assert_called()


@pytest.mark.asyncio
async def test_no_clap_model_is_no_op() -> None:
    """If the CLAP model never loaded, the helper returns silently."""
    p, _, _ = _make_provider()
    p._clap_model = None
    session = _make_session()
    window = np.zeros(WINDOW_SAMPLES, dtype=np.float32)

    await p._run_single_clap_window(session, window, SR)

    assert session.clap_completed_count == 0
    assert session.clap_sum_embedding is None


@pytest.mark.asyncio
async def test_calls_compute_similarity_with_text_embeddings() -> None:
    """The session's CLAP text embeddings are forwarded to compute_similarity."""
    p, fake_model, _ = _make_provider()
    session = _make_session()
    window = np.zeros(WINDOW_SAMPLES, dtype=np.float32)

    await p._run_single_clap_window(session, window, SR)

    fake_model.compute_similarity.assert_called_once()
    _audio_embs, text_embs_arg = fake_model.compute_similarity.call_args.args
    assert text_embs_arg is p._clap_text_embeddings


def test_single_window_inference_sync_returns_none_when_model_is_none() -> None:
    """
    _single_window_inference_sync returns None cleanly when the CLAP model is unloaded.

    Simulates the unload() race: model is nulled before the sync thread reads it.
    """
    p, _, _ = _make_provider()
    p._clap_model = None
    window = np.zeros(WINDOW_SAMPLES, dtype=np.float32)

    result = p._single_window_inference_sync(window, SR)

    assert result is None


def test_single_window_inference_sync_returns_none_when_text_embeddings_are_none() -> None:
    """_single_window_inference_sync returns None cleanly when text embeddings are unloaded."""
    p, _, _ = _make_provider()
    p._clap_text_embeddings = None
    window = np.zeros(WINDOW_SAMPLES, dtype=np.float32)

    result = p._single_window_inference_sync(window, SR)

    assert result is None


@pytest.mark.asyncio
async def test_run_single_clap_window_handles_none_return_from_sync() -> None:
    """
    _run_single_clap_window does not log a warning when inference returns None (unload race).

    The unload race: model is non-None at the early-return check in _run_single_clap_window,
    but is nulled before the sync thread reads it, causing _single_window_inference_sync to
    return None. The caller must handle this without a warning.
    """
    p, _, fake_logger = _make_provider()
    session = _make_session()
    window = np.zeros(WINDOW_SAMPLES, dtype=np.float32)

    with patch.object(p, "_single_window_inference_sync", return_value=None):
        await p._run_single_clap_window(session, window, SR)

    assert session.clap_completed_count == 0
    fake_logger.warning.assert_not_called()
    # Discriminate the new graceful path (logs "unloaded mid-flight") from the
    # old bug path (would log a TypeError about unpacking NoneType via the
    # broad except handler). Without this assertion the test is always-green.
    fake_logger.debug.assert_called()
    debug_messages = [str(call.args[0]) for call in fake_logger.debug.call_args_list]
    assert any("unloaded" in msg for msg in debug_messages), (
        f"Expected 'unloaded' debug message; got: {debug_messages}"
    )
