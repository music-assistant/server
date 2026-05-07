"""Tests for clap_prompts: stable hashing of SCALAR_PROMPT_PAIRS."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import torch

from music_assistant.providers.sonic_analysis import SonicAnalysisProvider
from music_assistant.providers.sonic_analysis.clap_prompts import (
    CALIBRATION_PROMPTS_HASH,
    SCALAR_PROMPT_PAIRS,
    compute_prompt_embeddings,
    hash_scalar_prompt_pairs,
    load_precomputed_prompt_embeddings,
    save_precomputed_prompt_embeddings,
    validate_calibration_freshness,
)


def test_hash_is_stable_across_calls() -> None:
    """Same input → same hash, every call."""
    h1 = hash_scalar_prompt_pairs(SCALAR_PROMPT_PAIRS)
    h2 = hash_scalar_prompt_pairs(SCALAR_PROMPT_PAIRS)
    assert h1 == h2


def test_hash_is_deterministic_hex_sha256() -> None:
    """Hash returns a 64-character lowercase hex string (SHA-256)."""
    h = hash_scalar_prompt_pairs(SCALAR_PROMPT_PAIRS)
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_hash_changes_when_a_single_word_changes() -> None:
    """Smallest possible prompt edit must invalidate the hash."""
    original = dict(SCALAR_PROMPT_PAIRS)
    pos, neg = original["danceability"]
    edited = dict(original)
    edited["danceability"] = (pos + ".", neg)  # one character difference
    assert hash_scalar_prompt_pairs(original) != hash_scalar_prompt_pairs(edited)


def test_hash_changes_when_a_key_is_renamed() -> None:
    """Renaming a scalar key must invalidate the hash."""
    original = dict(SCALAR_PROMPT_PAIRS)
    edited = {("dance" if k == "danceability" else k): v for k, v in original.items()}
    assert hash_scalar_prompt_pairs(original) != hash_scalar_prompt_pairs(edited)


def test_hash_changes_when_pair_order_swaps() -> None:
    """Swapping pos/neg within a pair must invalidate the hash."""
    original = dict(SCALAR_PROMPT_PAIRS)
    edited = dict(original)
    pos, neg = original["danceability"]
    edited["danceability"] = (neg, pos)
    assert hash_scalar_prompt_pairs(original) != hash_scalar_prompt_pairs(edited)


def test_save_load_round_trips_embeddings(tmp_path: Path) -> None:
    """Bit-exact round-trip of embeddings and hash through the .npz cache."""
    embeddings = np.random.RandomState(42).randn(10, 1024).astype(np.float32)
    prompts_hash = "a" * 64
    cache_path = tmp_path / "cache.npz"

    save_precomputed_prompt_embeddings(cache_path, embeddings, prompts_hash)
    loaded_embeddings, loaded_hash = load_precomputed_prompt_embeddings(cache_path)

    np.testing.assert_array_equal(embeddings, loaded_embeddings)
    assert loaded_hash == prompts_hash


def test_load_returns_native_str_hash(tmp_path: Path) -> None:
    """The loaded hash must be a Python str, not a numpy unicode scalar."""
    embeddings = np.zeros((10, 1024), dtype=np.float32)
    cache_path = tmp_path / "cache.npz"
    save_precomputed_prompt_embeddings(cache_path, embeddings, "deadbeef" * 8)

    _, loaded_hash = load_precomputed_prompt_embeddings(cache_path)
    assert type(loaded_hash) is str


def test_load_raises_when_file_missing(tmp_path: Path) -> None:
    """Missing cache file must surface as FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        load_precomputed_prompt_embeddings(tmp_path / "does-not-exist.npz")


def test_compute_prompt_embeddings_flattens_pos_neg_in_order() -> None:
    """The model receives prompts as [pos_0, neg_0, pos_1, neg_1, ...]."""
    captured: list[str] = []

    class FakeModel:
        def get_text_embeddings(self, prompts: list[str]) -> torch.Tensor:
            captured.extend(prompts)
            return torch.zeros((len(prompts), 1024))

    test_prompts = {
        "alpha": ("pos_a", "neg_a"),
        "beta": ("pos_b", "neg_b"),
    }
    compute_prompt_embeddings(FakeModel(), test_prompts)
    assert captured == ["pos_a", "neg_a", "pos_b", "neg_b"]


def test_compute_prompt_embeddings_returns_float32_numpy() -> None:
    """Result is a numpy float32 array shaped (2*N_pairs, embedding_dim)."""

    class FakeModel:
        def get_text_embeddings(self, prompts: list[str]) -> torch.Tensor:
            return torch.ones((len(prompts), 1024)) * 0.5

    test_prompts = {
        "alpha": ("p", "n"),
        "beta": ("p", "n"),
        "gamma": ("p", "n"),
    }
    out = compute_prompt_embeddings(FakeModel(), test_prompts)
    assert out.dtype == np.float32
    assert out.shape == (6, 1024)
    assert np.allclose(out, 0.5)


# ---------------------------------------------------------------------------
# T2.5: CALIBRATION_PROMPTS_HASH and validate_calibration_freshness tests
# ---------------------------------------------------------------------------


def test_calibration_prompts_hash_is_sha256_hex() -> None:
    """CALIBRATION_PROMPTS_HASH must be a 64-character lowercase hex string."""
    assert isinstance(CALIBRATION_PROMPTS_HASH, str)
    assert len(CALIBRATION_PROMPTS_HASH) == 64
    assert all(c in "0123456789abcdef" for c in CALIBRATION_PROMPTS_HASH)


def test_calibration_prompts_hash_matches_current_prompts() -> None:
    """CALIBRATION_PROMPTS_HASH must equal hash_scalar_prompt_pairs(SCALAR_PROMPT_PAIRS)."""
    assert hash_scalar_prompt_pairs(SCALAR_PROMPT_PAIRS) == CALIBRATION_PROMPTS_HASH


def test_validate_calibration_freshness_silent_when_fresh() -> None:
    """No warning is logged when CALIBRATION_PROMPTS_HASH matches the current prompts."""
    with patch("music_assistant.providers.sonic_analysis.clap_prompts._LOGGER") as mock_logger:
        validate_calibration_freshness(SCALAR_PROMPT_PAIRS)
        mock_logger.warning.assert_not_called()


def test_validate_calibration_freshness_warns_on_drift() -> None:
    """A warning is logged when the prompts hash drifts from CALIBRATION_PROMPTS_HASH."""
    stale_prompts = dict(SCALAR_PROMPT_PAIRS)
    pos, neg = stale_prompts["danceability"]
    stale_prompts["danceability"] = (pos + " MODIFIED", neg)

    with patch("music_assistant.providers.sonic_analysis.clap_prompts._LOGGER") as mock_logger:
        validate_calibration_freshness(stale_prompts)
        mock_logger.warning.assert_called_once()
        # Verify both hashes are mentioned in the warning
        call_args = mock_logger.warning.call_args
        assert CALIBRATION_PROMPTS_HASH in str(call_args)


def test_validate_calibration_freshness_drift_hash_differs() -> None:
    """Modified prompts must produce a hash different from CALIBRATION_PROMPTS_HASH."""
    modified = dict(SCALAR_PROMPT_PAIRS)
    pos, neg = modified["valence"]
    modified["valence"] = (pos + " extra", neg)
    assert hash_scalar_prompt_pairs(modified) != CALIBRATION_PROMPTS_HASH


@pytest.mark.asyncio
async def test_handle_async_init_calls_validate_calibration_freshness() -> None:
    """SonicAnalysisProvider.handle_async_init must call validate_calibration_freshness."""
    p = SonicAnalysisProvider.__new__(SonicAnalysisProvider)
    p.logger = MagicMock()
    p._clap_model = None
    p._clap_text_embeddings = None
    p._clap_prompt_order = []
    p._clap_load_task = None
    p.mass = SimpleNamespace(  # type: ignore[assignment]
        create_task=MagicMock(side_effect=lambda coro: coro.close() or MagicMock())
    )

    with patch(
        "music_assistant.providers.sonic_analysis.validate_calibration_freshness"
    ) as mock_validate:
        await p.handle_async_init()
        mock_validate.assert_called_once_with()
