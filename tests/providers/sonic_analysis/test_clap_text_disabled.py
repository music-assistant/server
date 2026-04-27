"""Tests for the text-disabled load path in the vendored CLAP wrapper.

These tests verify Option A's core mechanism: when text_enabled=False,
the wrapper must NOT trigger any HuggingFace Hub download for the GPT2
text encoder or its tokenizer (~500MB savings).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from music_assistant.providers.sonic_analysis.vendored_clap.clap_wrapper import CLAPWrapper
from music_assistant.providers.sonic_analysis.vendored_clap.models import clap

# --------------------------------------------------------------------------- #
#  TextEncoder skip_text_model flag                                            #
# --------------------------------------------------------------------------- #


def test_text_encoder_skip_does_not_call_automodel() -> None:
    """skip_text_model=True must not invoke AutoModel.from_pretrained."""
    with patch.object(clap, "AutoModel") as mock_auto:
        clap.TextEncoder(
            d_out=1024,
            text_model="gpt2",
            transformer_embed_dim=768,
            skip_text_model=True,
        )
        mock_auto.from_pretrained.assert_not_called()


def test_text_encoder_skip_sets_base_to_none() -> None:
    """skip_text_model=True leaves the text-model base attribute as None."""
    with patch.object(clap, "AutoModel"):
        encoder = clap.TextEncoder(
            d_out=1024,
            text_model="gpt2",
            transformer_embed_dim=768,
            skip_text_model=True,
        )
        assert encoder.base is None


def test_text_encoder_default_calls_automodel() -> None:
    """Default behavior unchanged: AutoModel.from_pretrained is invoked."""
    with patch.object(clap, "AutoModel") as mock_auto:
        mock_auto.from_pretrained.return_value = MagicMock()
        clap.TextEncoder(d_out=1024, text_model="gpt2", transformer_embed_dim=768)
        mock_auto.from_pretrained.assert_called_once_with("gpt2")


# --------------------------------------------------------------------------- #
#  CLAPWrapper.get_text_embeddings clear-error path                            #
# --------------------------------------------------------------------------- #


def test_wrapper_get_text_embeddings_raises_when_text_disabled() -> None:
    """get_text_embeddings on a text-disabled wrapper raises a clear RuntimeError."""
    wrapper = CLAPWrapper.__new__(CLAPWrapper)
    wrapper.text_enabled = False
    wrapper.tokenizer = None
    with pytest.raises(RuntimeError, match="text encoder is disabled"):
        wrapper.get_text_embeddings(["any query"])
