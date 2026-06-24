"""Zero-shot CLAP prompt pairs and Platt-scale calibration for the soft scalars."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np

PRECOMPUTED_EMBEDDINGS_PATH: Path = (
    Path(__file__).parent / "vendored_clap" / "precomputed_prompt_embeddings.npz"
)

SCALAR_PROMPT_PAIRS: dict[str, tuple[str, str]] = {
    "danceability": (
        "dance beat, 4/4 groove, danceable, body movement, club, steady tempo, pulsing bassline",
        "slow tempo, ballad, free rhythm, ambient, meditative, sparse drums, no beat",
    ),
    "valence": (
        "The sound of a joyful upbeat song with bright major-key chords and happy vocals.",
        "The sound of a mournful sad song with minor-key chords and melancholy vocals.",
    ),
    "arousal": (
        "loud, intense, fast tempo, distorted guitars, aggressive drums, high energy, shouting",
        "soft, quiet, slow tempo, gentle, ambient, meditative, calm, peaceful, whispered",
    ),
    "instrumentalness": (
        "instrumental, no vocals, no singing, piano solo, strings, orchestra, film score",
        "lead vocals, singer, lyrics, verses, chorus, vocal melody, singing",
    ),
    "acousticness": (
        "acoustic guitar, piano, and hand percussion recorded with natural room sound.",
        "synthesizers, drum machines, and auto-tuned vocals with heavy studio production.",
    ),
}


def compute_prompt_embeddings(model: object, prompts: dict[str, tuple[str, str]]) -> np.ndarray:
    """
    Run a CLAP model's text encoder over a SCALAR_PROMPT_PAIRS-shaped mapping.

    :param model: An object exposing ``get_text_embeddings(list[str]) -> torch.Tensor``.
    :param prompts: Mapping of scalar name -> (positive, negative) prompt pair.
    """
    flat: list[str] = []
    for pos, neg in prompts.values():
        flat.extend([pos, neg])
    embeddings_tensor = model.get_text_embeddings(flat)  # type: ignore[attr-defined]
    arr: np.ndarray = embeddings_tensor.detach().cpu().numpy().astype(np.float32, copy=False)
    return arr


def save_precomputed_prompt_embeddings(
    path: Path, embeddings: np.ndarray, prompts_hash: str
) -> None:
    """
    Persist the (N, D) prompt-embedding matrix and its prompts-hash to .npz.

    :param path: Destination .npz path; parent must exist.
    :param embeddings: float32 array of shape (N_prompts, embedding_dim).
    :param prompts_hash: SHA-256 hex digest of the SCALAR_PROMPT_PAIRS the
        embeddings were computed from (drift detector at load time).
    """
    np.savez_compressed(
        path,
        embeddings=embeddings.astype(np.float32, copy=False),
        prompts_hash=np.array(prompts_hash),
    )


def load_precomputed_prompt_embeddings(path: Path) -> tuple[np.ndarray, str]:
    """
    Load (embeddings, prompts_hash) from an .npz written by save_precomputed_prompt_embeddings.

    :param path: Source .npz path.
    """
    if not path.exists():
        raise FileNotFoundError(path)
    with np.load(path) as data:
        embeddings = np.asarray(data["embeddings"], dtype=np.float32)
        prompts_hash = str(data["prompts_hash"].item())
    return embeddings, prompts_hash


def hash_scalar_prompt_pairs(prompts: dict[str, tuple[str, str]]) -> str:
    """
    Stable SHA-256 hex digest of a SCALAR_PROMPT_PAIRS-shaped mapping.

    :param prompts: Mapping of scalar name -> (positive, negative) prompt pair.
    """
    canonical = json.dumps(
        [[k, [p, n]] for k, (p, n) in prompts.items()],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# Calibration provenance
# ----------------------
# (a, b) Platt-scaling coefficients: score = sigmoid(a * (pos_logit - neg_logit) + b).
# Fit via sklearn.LogisticRegression (5-fold CV) on a 50-track diverse ground-truth
# set during initial development (commit c93183684, 2026-04-23).
# 5-fold CV accuracy on the validation set:
#   acousticness: 0.843, danceability: 0.910, instrumentalness: 0.896,
#   arousal: 0.727, valence: 0.713
# The b term corrects for CLAP's per-attribute bias (e.g., b<<0 on instrumentalness
# corrects CLAP's tendency to interpret most music as instrumental).
#
# CALIBRATION_PROMPTS_HASH records the SCALAR_PROMPT_PAIRS hash at calibration time.
# If you edit SCALAR_PROMPT_PAIRS, the hash will drift and
# ``test_clap_calibration_hash_matches_prompts`` will fail in CI. When that happens
# you must re-fit CALIBRATION and update CALIBRATION_PROMPTS_HASH, then bump
# analysis_version in the provider so existing rows get re-analyzed.
CALIBRATION_PROMPTS_HASH: str = "67495ab1df2dae36c6886975fdc56265151656f68bc7b3c9dbc941abd518b969"

CALIBRATION: dict[str, tuple[float, float]] = {
    "danceability": (0.940, -0.134),
    "valence": (0.441, -1.870),
    "arousal": (0.359, +0.358),
    "instrumentalness": (0.761, -3.538),
    "acousticness": (0.549, +0.453),
}


def score_scalars(
    mean_similarities: np.ndarray,
    calibration: dict[str, tuple[float, float]] = CALIBRATION,
) -> dict[str, float]:
    """
    Map mean per-window CLAP similarity logits to calibrated 0-1 scalars.

    :param mean_similarities: Per-prompt similarity logits averaged across
        windows, ordered as SCALAR_PROMPT_PAIRS flattens its (pos, neg) pairs.
    :param calibration: scalar name -> (a, b) Platt coefficients.
    """
    expected = 2 * len(SCALAR_PROMPT_PAIRS)
    if mean_similarities.shape != (expected,):
        raise ValueError(
            f"mean_similarities must have shape ({expected},), got {mean_similarities.shape}"
        )
    scores: dict[str, float] = {}
    for idx, scalar_name in enumerate(SCALAR_PROMPT_PAIRS):
        pos_logit = float(mean_similarities[idx * 2])
        neg_logit = float(mean_similarities[idx * 2 + 1])
        a, b = calibration[scalar_name]
        margin = pos_logit - neg_logit
        scores[scalar_name] = 1.0 / (1.0 + math.exp(-(a * margin + b)))
    return scores
