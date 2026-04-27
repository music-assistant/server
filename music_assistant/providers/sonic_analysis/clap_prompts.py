"""Zero-shot text prompts and Platt-scale calibration for CLAP scoring.

Each scalar is derived by computing cosine similarity between the audio
embedding and a POSITIVE / NEGATIVE text pair, then applying Platt-scaled
sigmoid calibration to convert the raw margin into a well-calibrated 0-1
probability for the positive concept.

Prompts and calibration (a, b) were selected empirically via A/B testing
on a 50-track diverse ground-truth set. Prompt wording and calibration
are considered a tuned parameter of this provider — meaningful changes
should bump the provider's analysis_version so existing rows get
re-analyzed.

Calibration fit:
    score = sigmoid(a * (pos_logit - neg_logit) + b)

The b term corrects for CLAP's systematic per-attribute bias (e.g., its
tendency to interpret most music as "happy" gets corrected by b<0 on
valence, its tendency to see music as instrumental gets corrected by
b<<0 on instrumentalness).

5-fold CV accuracy on the 50-track validation set:
    acousticness:     0.843
    danceability:     0.910
    instrumentalness: 0.896
    arousal:          0.727
    valence:          0.713  ← signal-limited; probe needed for further gain
"""

from __future__ import annotations

# (positive_prompt, negative_prompt) — the scalar represents CLAP's
# confidence that the audio matches the positive side.
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

# Platt scaling per attribute: (a, b) such that
# score = sigmoid(a * (pos_logit - neg_logit) + b).
# Fit via sklearn.LogisticRegression on the 50-track validation set.
CALIBRATION: dict[str, tuple[float, float]] = {
    "danceability": (0.940, -0.134),
    "valence": (0.441, -1.870),
    "arousal": (0.359, +0.358),
    "instrumentalness": (0.761, -3.538),
    "acousticness": (0.549, +0.453),
}
