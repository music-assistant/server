"""Tests for SonicAnalysisProvider._load_clap text-encoder gating.

These tests verify the decision logic that picks between:
  (a) CLAP with text_enabled=False + cached prompt embeddings, when
      CONF_TEXT_SEARCH is off and the shipped .npz hash matches.
  (b) CLAP with text_enabled=True (current behavior) when text search
      is on, OR when the cached hash drifts from current prompts.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np
import torch

from music_assistant.providers.sonic_analysis import SonicAnalysisProvider
from music_assistant.providers.sonic_analysis.clap_prompts import (
    PRECOMPUTED_EMBEDDINGS_PATH,
    load_precomputed_prompt_embeddings,
)


def _make_stub_provider(*, text_search_enabled: bool) -> Any:
    """Build a minimal SonicAnalysisProvider instance that can call _load_clap."""
    provider = SonicAnalysisProvider.__new__(SonicAnalysisProvider)
    provider.config = MagicMock()
    provider.config.get_value = MagicMock(return_value=text_search_enabled)
    provider.logger = MagicMock()
    return provider


def _fake_clap_factory() -> tuple[MagicMock, MagicMock]:
    """Return (CLAP_class_mock, model_instance_mock).

    The model exposes get_text_embeddings returning a torch tensor so the
    live path can run without real weights.
    """
    mock_model = MagicMock()
    mock_model.get_text_embeddings = MagicMock(return_value=torch.zeros((10, 1024)))
    mock_clap_cls = MagicMock(return_value=mock_model)
    return mock_clap_cls, mock_model


# --------------------------------------------------------------------------- #
#  Cached path: text search disabled + valid cache hash                        #
# --------------------------------------------------------------------------- #


def test_load_clap_uses_text_disabled_when_text_search_off() -> None:
    """text_search OFF + valid cache → CLAP constructed with text_enabled=False."""
    mock_clap_cls, _mock_model = _fake_clap_factory()
    with patch("music_assistant.providers.sonic_analysis.vendored_clap.CLAP", mock_clap_cls):
        provider = _make_stub_provider(text_search_enabled=False)
        provider._load_clap()

    assert mock_clap_cls.called
    kwargs = mock_clap_cls.call_args.kwargs
    assert kwargs.get("text_enabled") is False


def test_load_clap_returns_cached_embeddings_as_tensor() -> None:
    """Cached path returns a torch.Tensor compatible with compute_similarity."""
    expected_np, _ = load_precomputed_prompt_embeddings(PRECOMPUTED_EMBEDDINGS_PATH)
    mock_clap_cls, _ = _fake_clap_factory()

    with patch("music_assistant.providers.sonic_analysis.vendored_clap.CLAP", mock_clap_cls):
        provider = _make_stub_provider(text_search_enabled=False)
        _model, embeddings, _order = provider._load_clap()

    assert isinstance(embeddings, torch.Tensor)
    np.testing.assert_array_equal(embeddings.detach().cpu().numpy(), expected_np)


# --------------------------------------------------------------------------- #
#  Live path: text search enabled                                              #
# --------------------------------------------------------------------------- #


def test_load_clap_uses_text_enabled_when_text_search_on() -> None:
    """text_search ON → CLAP constructed with text_enabled=True (live embed)."""
    mock_clap_cls, mock_model = _fake_clap_factory()
    with patch("music_assistant.providers.sonic_analysis.vendored_clap.CLAP", mock_clap_cls):
        provider = _make_stub_provider(text_search_enabled=True)
        provider._load_clap()

    kwargs = mock_clap_cls.call_args.kwargs
    assert kwargs.get("text_enabled") is True
    mock_model.get_text_embeddings.assert_called_once()


# --------------------------------------------------------------------------- #
#  Fallback path: cache hash mismatch                                          #
# --------------------------------------------------------------------------- #


def test_load_clap_falls_back_when_cache_hash_mismatches() -> None:
    """Hash mismatch → load full CLAP (text_enabled=True), warn, embed live."""
    mock_clap_cls, mock_model = _fake_clap_factory()
    bad_cache = (np.zeros((10, 1024), dtype=np.float32), "0" * 64)

    with (
        patch("music_assistant.providers.sonic_analysis.vendored_clap.CLAP", mock_clap_cls),
        patch(
            "music_assistant.providers.sonic_analysis.load_precomputed_prompt_embeddings",
            return_value=bad_cache,
        ),
    ):
        provider = _make_stub_provider(text_search_enabled=False)
        provider._load_clap()

    kwargs = mock_clap_cls.call_args.kwargs
    assert kwargs.get("text_enabled") is True
    mock_model.get_text_embeddings.assert_called_once()
    provider.logger.warning.assert_called()
