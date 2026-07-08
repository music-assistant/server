"""
Precompute CLAP text embeddings for SCALAR_PROMPT_PAIRS and ship as .npz.

Run once whenever the prompts in clap_prompts.py change (and bump the
provider's analysis_version alongside). The resulting artifact is loaded
by the sonic_analysis provider so the audio-only path can score scalars
without downloading the GPT2 text encoder.

Usage:
    python scripts/precompute_clap_prompt_embeddings.py
"""

from __future__ import annotations

import sys

# ruff: noqa: T201
from music_assistant.providers.sonic_analysis.clap_prompts import (
    PRECOMPUTED_EMBEDDINGS_PATH,
    SCALAR_PROMPT_PAIRS,
    compute_prompt_embeddings,
    hash_scalar_prompt_pairs,
    save_precomputed_prompt_embeddings,
)
from music_assistant.providers.sonic_analysis.vendored_clap import CLAP


def main() -> int:
    """Load CLAP, embed SCALAR_PROMPT_PAIRS, and write the .npz artifact."""
    print("Loading CLAP model with text encoder (this triggers GPT2 download on first run)...")
    model = CLAP(version="2023", use_cuda=False)

    print(
        f"Embedding {len(SCALAR_PROMPT_PAIRS)} prompt pairs ({2 * len(SCALAR_PROMPT_PAIRS)} strings)..."
    )
    embeddings = compute_prompt_embeddings(model, SCALAR_PROMPT_PAIRS)
    prompts_hash = hash_scalar_prompt_pairs(SCALAR_PROMPT_PAIRS)

    print(f"Embeddings shape: {embeddings.shape}, dtype: {embeddings.dtype}")
    print(f"Prompts hash: {prompts_hash}")

    PRECOMPUTED_EMBEDDINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    save_precomputed_prompt_embeddings(PRECOMPUTED_EMBEDDINGS_PATH, embeddings, prompts_hash)

    size_kb = PRECOMPUTED_EMBEDDINGS_PATH.stat().st_size / 1024
    print(f"Wrote {PRECOMPUTED_EMBEDDINGS_PATH} ({size_kb:.1f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
