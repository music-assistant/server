"""Tests for clap_prompts: stable hashing of SCALAR_PROMPT_PAIRS."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest
import torch

from music_assistant.providers.sonic_analysis.clap_prompts import (
    CALIBRATION,
    CALIBRATION_PROMPTS_HASH,
    SCALAR_PROMPT_PAIRS,
    compute_prompt_embeddings,
    hash_scalar_prompt_pairs,
    load_precomputed_prompt_embeddings,
    save_precomputed_prompt_embeddings,
    score_scalars,
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
# CALIBRATION_PROMPTS_HASH tripwire — fails CI if prompts drift from calibration
# ---------------------------------------------------------------------------


def test_calibration_prompts_hash_is_sha256_hex() -> None:
    """CALIBRATION_PROMPTS_HASH must be a 64-character lowercase hex string."""
    assert isinstance(CALIBRATION_PROMPTS_HASH, str)
    assert len(CALIBRATION_PROMPTS_HASH) == 64
    assert all(c in "0123456789abcdef" for c in CALIBRATION_PROMPTS_HASH)


def test_clap_calibration_hash_matches_prompts() -> None:
    """Tripwire: if CLAP prompts change, calibration coefficients must be re-tuned."""
    assert hash_scalar_prompt_pairs(SCALAR_PROMPT_PAIRS) == CALIBRATION_PROMPTS_HASH, (
        "CLAP prompts changed but CALIBRATION_PROMPTS_HASH was not updated. "
        "Re-run calibration on the 50-track sample set and update the hash + scalars."
    )


def test_score_scalars_zero_margin_returns_sigmoid_of_bias() -> None:
    """All similarities equal -> margin 0 for every scalar -> score == sigmoid(b)."""
    mean = np.zeros(2 * len(SCALAR_PROMPT_PAIRS), dtype=np.float32)
    scores = score_scalars(mean)
    assert set(scores) == set(SCALAR_PROMPT_PAIRS)
    for name, (_a, b) in CALIBRATION.items():
        assert math.isclose(scores[name], 1.0 / (1.0 + math.exp(-b)), rel_tol=1e-9)


def test_score_scalars_uses_pos_minus_neg_margin() -> None:
    """Danceability is index 0: set pos=1.0, neg=0.0 -> margin 1.0."""
    mean = np.zeros(2 * len(SCALAR_PROMPT_PAIRS), dtype=np.float32)
    mean[0] = 1.0  # danceability pos
    mean[1] = 0.0  # danceability neg
    a, b = CALIBRATION["danceability"]
    expected = 1.0 / (1.0 + math.exp(-(a * 1.0 + b)))
    assert math.isclose(score_scalars(mean)["danceability"], expected, rel_tol=1e-9)


def test_score_scalars_rejects_wrong_shape() -> None:
    """A too-long array would otherwise be silently truncated -> fail fast instead."""
    with pytest.raises(ValueError, match="must have shape"):
        score_scalars(np.zeros(2 * len(SCALAR_PROMPT_PAIRS) + 2, dtype=np.float32))
