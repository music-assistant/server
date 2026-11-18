"""Tests for the precomputed CLAP prompt-embedding cache loader."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np

from music_assistant.providers.sonic_analysis import SonicAnalysisProvider


def _make_provider() -> tuple[SonicAnalysisProvider, MagicMock]:
    """Build a SonicAnalysisProvider with just enough scaffolding for the cache check."""
    p = SonicAnalysisProvider.__new__(SonicAnalysisProvider)
    fake_logger = MagicMock()
    p.logger = fake_logger
    return p, fake_logger


def test_returns_embeddings_when_hash_matches() -> None:
    """Hash matches → cached embeddings are returned and no warning is logged."""
    p, fake_logger = _make_provider()
    fake_emb = np.zeros((10, 1024), dtype=np.float32)
    fake_hash = "abc123"

    with (
        patch(
            "music_assistant.providers.sonic_analysis.load_precomputed_prompt_embeddings",
            return_value=(fake_emb, fake_hash),
        ),
        patch(
            "music_assistant.providers.sonic_analysis.hash_scalar_prompt_pairs",
            return_value=fake_hash,
        ),
    ):
        result = p._try_load_cached_prompt_embeddings()

    assert result is fake_emb
    fake_logger.warning.assert_not_called()


def test_returns_none_when_hash_drifts() -> None:
    """Hash mismatch → returns None and warns the operator to re-run the precompute script."""
    p, fake_logger = _make_provider()
    fake_emb = np.zeros((10, 1024), dtype=np.float32)

    with (
        patch(
            "music_assistant.providers.sonic_analysis.load_precomputed_prompt_embeddings",
            return_value=(fake_emb, "stale_hash"),
        ),
        patch(
            "music_assistant.providers.sonic_analysis.hash_scalar_prompt_pairs",
            return_value="fresh_hash",
        ),
    ):
        result = p._try_load_cached_prompt_embeddings()

    assert result is None
    fake_logger.warning.assert_called()


def test_returns_none_when_cache_file_missing() -> None:
    """Missing .npz → returns None and warns; _load_clap turns that into a setup failure."""
    p, fake_logger = _make_provider()

    with patch(
        "music_assistant.providers.sonic_analysis.load_precomputed_prompt_embeddings",
        side_effect=FileNotFoundError("no cache file"),
    ):
        result = p._try_load_cached_prompt_embeddings()

    assert result is None
    fake_logger.warning.assert_called()
