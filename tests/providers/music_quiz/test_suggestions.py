"""Tests for Music Quiz suggestion helpers."""

from __future__ import annotations

import json
import random
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from music_assistant.models.plugin import PluginProvider
from music_assistant.providers.music_quiz.ai_distractors import (
    MAX_AI_LABEL_LENGTH,
    MAX_AI_PROMPT_BYTES,
    MAX_AI_RESPONSE_BYTES,
    MAX_AI_RESPONSE_LINES,
    parse_ai_distractor_response,
    request_ai_distractors,
)
from music_assistant.providers.music_quiz.suggestions import (
    SuggestionCandidate,
    answer_labels_are_too_close,
    build_answer_label,
    build_suggestions,
    filter_suggestion_candidates,
    normalize_answer_label,
    suggestion_candidates_are_too_close,
)


def test_build_answer_label_with_artist_and_title() -> None:
    """Build the v1 Artist - Track title answer label."""
    assert build_answer_label("Massive Attack", "Teardrop") == "Massive Attack - Teardrop"


def test_build_answer_label_without_artist() -> None:
    """Fall back to title when the artist is unknown."""
    assert build_answer_label(None, "Untitled") == "Untitled"


def test_normalize_answer_label_ignores_case_and_punctuation() -> None:
    """Normalize answer labels for duplicate detection."""
    assert normalize_answer_label("Daft Punk - One More Time!") == "daft punk one more time"


def test_normalize_answer_label_handles_canonical_unicode_equivalence() -> None:
    """Normalize composed and decomposed Unicode answer labels identically."""
    assert normalize_answer_label("Beyoncé") == normalize_answer_label("Beyonce\u0301")


def test_answer_labels_are_too_close_detects_title_versions() -> None:
    """Treat radio edits and remasters as too close for answer choices."""
    assert answer_labels_are_too_close(
        "Massive Attack - Teardrop",
        "Massive Attack - Teardrop [Radio Edit]",
    )
    assert answer_labels_are_too_close(
        "Massive Attack - Teardrop",
        "Massive Attack - Teardrop (Remastered 2019)",
    )
    assert not answer_labels_are_too_close(
        "Massive Attack - Teardrop",
        "Portishead - Glory Box",
    )


def test_suggestion_candidates_compare_track_titles_first() -> None:
    """Use raw track titles as the primary distance signal when available."""
    assert suggestion_candidates_are_too_close(
        SuggestionCandidate(
            "Artist One - Midnight City",
            "library://track/1",
            title="Midnight City",
        ),
        SuggestionCandidate(
            "Artist Two - Midnight City [Radio Edit]",
            "library://track/2",
            title="Midnight City [Radio Edit]",
        ),
    )
    assert not suggestion_candidates_are_too_close(
        SuggestionCandidate(
            "Artist One - Midnight City",
            "library://track/1",
            title="Midnight City",
        ),
        SuggestionCandidate(
            "Artist One - Reunion",
            "library://track/2",
            title="Reunion",
        ),
    )


def test_build_suggestions_includes_one_correct_answer() -> None:
    """Build suggestions with exactly one correct answer."""
    suggestions = build_suggestions(
        SuggestionCandidate("Daft Punk - One More Time", "library://track/1"),
        [
            SuggestionCandidate("Justice - D.A.N.C.E.", "library://track/2"),
            SuggestionCandidate("Phoenix - Lisztomania", "library://track/3"),
            SuggestionCandidate("Air - Sexy Boy", "library://track/4"),
        ],
        4,
        rng=random.Random(1),
    )

    assert len(suggestions) == 4
    assert sum(item.is_correct for item in suggestions) == 1
    assert {item.label for item in suggestions} == {
        "Daft Punk - One More Time",
        "Justice - D.A.N.C.E.",
        "Phoenix - Lisztomania",
        "Air - Sexy Boy",
    }


def test_build_suggestions_filters_duplicate_uri_and_label() -> None:
    """Skip distractors that duplicate the correct answer or each other."""
    suggestions = build_suggestions(
        SuggestionCandidate("Daft Punk - One More Time", "library://track/1"),
        [
            SuggestionCandidate("Daft Punk - One More Time", "library://track/other"),
            SuggestionCandidate("Different label", "library://track/1"),
            SuggestionCandidate("Justice - D.A.N.C.E.", "library://track/2"),
            SuggestionCandidate("Justice D A N C E", "library://track/3"),
            SuggestionCandidate("Phoenix - Lisztomania", "library://track/4"),
        ],
        3,
        rng=random.Random(1),
    )

    assert {item.label for item in suggestions} == {
        "Daft Punk - One More Time",
        "Justice - D.A.N.C.E.",
        "Phoenix - Lisztomania",
    }


def test_build_suggestions_filters_close_title_versions() -> None:
    """Skip distractors that are only version variants of the answer."""
    suggestions = build_suggestions(
        SuggestionCandidate(
            "Massive Attack - Teardrop",
            "library://track/1",
            title="Teardrop",
        ),
        [
            SuggestionCandidate(
                "Massive Attack - Teardrop [Radio Edit]",
                "library://track/2",
                title="Teardrop [Radio Edit]",
            ),
            SuggestionCandidate(
                "Massive Attack - Teardrop (Remastered 2019)",
                "library://track/3",
                title="Teardrop (Remastered 2019)",
            ),
            SuggestionCandidate(
                "Portishead - Glory Box",
                "library://track/4",
                title="Glory Box",
            ),
            SuggestionCandidate(
                "Tricky - Hell Is Round The Corner",
                "library://track/5",
                title="Hell Is Round The Corner",
            ),
        ],
        3,
        rng=random.Random(1),
    )

    assert {item.label for item in suggestions} == {
        "Massive Attack - Teardrop",
        "Portishead - Glory Box",
        "Tricky - Hell Is Round The Corner",
    }


def test_build_suggestions_filters_close_distractors() -> None:
    """Skip candidates that are too close to already selected distractors."""
    suggestions = build_suggestions(
        SuggestionCandidate("Daft Punk - One More Time", "library://track/1"),
        [
            SuggestionCandidate("Justice - D.A.N.C.E.", "library://track/2"),
            SuggestionCandidate("Justice - D.A.N.C.E. Radio Edit", "library://track/3"),
            SuggestionCandidate("Phoenix - Lisztomania", "library://track/4"),
        ],
        3,
        rng=random.Random(1),
    )

    assert {item.label for item in suggestions} == {
        "Daft Punk - One More Time",
        "Justice - D.A.N.C.E.",
        "Phoenix - Lisztomania",
    }


def test_build_suggestions_requires_enough_distractors() -> None:
    """Fail clearly when there are not enough unique distractors."""
    with pytest.raises(ValueError, match="Not enough distractors"):
        build_suggestions(
            SuggestionCandidate("Daft Punk - One More Time", "library://track/1"),
            [SuggestionCandidate("Daft Punk - One More Time", "library://track/2")],
            3,
        )


def test_build_suggestions_requires_at_least_two_choices() -> None:
    """Reject a suggestion count below two."""
    with pytest.raises(ValueError, match="at least 2"):
        build_suggestions(
            SuggestionCandidate("Daft Punk - One More Time", "library://track/1"),
            [],
            1,
        )


def test_build_suggestions_use_opaque_ids() -> None:
    """Suggestion IDs are sent to guests pre-reveal: they must not name the answer."""
    opaque_ids = ["id-a", "id-b", "id-c", "id-d"]
    with patch(
        "music_assistant.providers.music_quiz.suggestions.secrets.token_hex",
        side_effect=opaque_ids,
    ):
        suggestions = build_suggestions(
            SuggestionCandidate("Daft Punk - One More Time", "library://track/1"),
            [
                SuggestionCandidate("Justice - D.A.N.C.E.", "library://track/2"),
                SuggestionCandidate("Phoenix - Lisztomania", "library://track/3"),
                SuggestionCandidate("Air - Sexy Boy", "library://track/4"),
            ],
            4,
            rng=random.Random(1),
        )

    # every id comes from the opaque token source, never derived from the answer
    assert {suggestion.suggestion_id for suggestion in suggestions} == set(opaque_ids)


def test_build_suggestions_uses_local_secure_shuffle_by_default() -> None:
    """Shuffle final options locally with the system random source."""
    with patch("music_assistant.providers.music_quiz.suggestions.SYSTEM_RANDOM.shuffle") as shuffle:
        build_suggestions(
            SuggestionCandidate("Daft Punk - One More Time", "library://track/1"),
            [
                SuggestionCandidate("Justice - D.A.N.C.E.", "library://track/2"),
                SuggestionCandidate("Phoenix - Lisztomania", "library://track/3"),
                SuggestionCandidate("Air - Sexy Boy", "library://track/4"),
            ],
            4,
        )

    shuffle.assert_called_once()


def test_filter_suggestion_candidates_preserves_valid_order() -> None:
    """Filter duplicate and close candidates without reordering valid entries."""
    correct = SuggestionCandidate("Daft Punk - One More Time", title="One More Time")
    candidates = [
        SuggestionCandidate("Daft Punk - One More Time (Remix)", title="One More Time (Remix)"),
        SuggestionCandidate("Justice - Genesis", title="Genesis"),
        SuggestionCandidate("Justice - Genesis!", title="Genesis!"),
        SuggestionCandidate("Air - Sexy Boy", title="Sexy Boy"),
    ]

    assert filter_suggestion_candidates(correct, candidates) == [
        candidates[1],
        candidates[3],
    ]


def test_parse_ai_distractor_response_accepts_exact_schema() -> None:
    """Parse a complete candidate permutation and exact synthetic kind sequence."""
    response = json.dumps(
        {
            "ranked_ids": ["candidate_1", "candidate_0"],
            "synthetic": [
                {"kind": "same_artist_title", "label": "Daft Punk - Neon Horizon"},
                {"kind": "context_track", "label": "Lunar Circuit - Chrome Reverie"},
            ],
        }
    )

    parsed = parse_ai_distractor_response(
        response,
        ["candidate_0", "candidate_1"],
        ["same_artist_title", "context_track"],
    )

    assert parsed.ranked_ids == ("candidate_1", "candidate_0")
    assert [(item.kind, item.label) for item in parsed.synthetic] == [
        ("same_artist_title", "Daft Punk - Neon Horizon"),
        ("context_track", "Lunar Circuit - Chrome Reverie"),
    ]


@pytest.mark.parametrize(
    "response",
    [
        "not json",
        json.dumps(
            {
                "ranked_ids": ["candidate_0"],
                "synthetic": [{"kind": "artist", "label": "Fake Artist"}],
                "extra": True,
            }
        ),
        json.dumps(
            {
                "ranked_ids": [],
                "synthetic": [{"kind": "artist", "label": "Fake Artist"}],
            }
        ),
        json.dumps(
            {
                "ranked_ids": ["candidate_0"],
                "synthetic": [{"kind": "wrong", "label": "Fake Artist"}],
            }
        ),
        json.dumps(
            {
                "ranked_ids": ["candidate_0"],
                "synthetic": [{"kind": "artist", "label": "Fake Artist", "extra": True}],
            }
        ),
        json.dumps(
            {
                "ranked_ids": ["candidate_0"],
                "synthetic": [{"kind": "artist", "label": 123}],
            }
        ),
        json.dumps(
            {
                "ranked_ids": ["candidate_0"],
                "synthetic": [{"kind": "artist", "label": " Fake Artist"}],
            }
        ),
        json.dumps(
            {
                "ranked_ids": ["candidate_0"],
                "synthetic": [{"kind": "artist", "label": "Fake\nArtist"}],
            }
        ),
        json.dumps(
            {
                "ranked_ids": ["candidate_0"],
                "synthetic": [{"kind": "artist", "label": "Fake\u0000Artist"}],
            }
        ),
        json.dumps(
            {
                "ranked_ids": ["candidate_0"],
                "synthetic": [{"kind": "artist", "label": "Fake\u2028Artist"}],
            }
        ),
    ],
)
def test_parse_ai_distractor_response_rejects_malformed_output(response: str) -> None:
    """Reject commentary, extra fields, wrong shapes, and unsafe labels."""
    with pytest.raises((TypeError, ValueError)):
        parse_ai_distractor_response(response, ["candidate_0"], ["artist"])


def test_parse_ai_distractor_response_rejects_wrong_count_and_close_labels() -> None:
    """Require the exact synthetic count with pairwise-distinct labels."""
    missing = json.dumps({"ranked_ids": [], "synthetic": []})
    close = json.dumps(
        {
            "ranked_ids": [],
            "synthetic": [
                {"kind": "artist", "label": "Fake Artist"},
                {"kind": "artist", "label": "Fake Artist!"},
            ],
        }
    )

    with pytest.raises(ValueError, match="shape"):
        parse_ai_distractor_response(missing, [], ["artist"])
    with pytest.raises(ValueError, match="distinct"):
        parse_ai_distractor_response(close, [], ["artist", "artist"])


def test_parse_ai_distractor_response_enforces_size_line_and_label_limits() -> None:
    """Reject responses and labels outside their explicit resource limits."""
    oversized_response = "x" * (MAX_AI_RESPONSE_BYTES + 1)
    too_many_lines = "\n".join("{}" for _ in range(MAX_AI_RESPONSE_LINES + 1))
    oversized_label = json.dumps(
        {
            "ranked_ids": [],
            "synthetic": [
                {"kind": "artist", "label": "x" * (MAX_AI_LABEL_LENGTH + 1)},
            ],
        }
    )

    with pytest.raises(ValueError, match="size"):
        parse_ai_distractor_response(oversized_response, [], [])
    with pytest.raises(ValueError, match="line"):
        parse_ai_distractor_response(too_many_lines, [], [])
    with pytest.raises(ValueError, match="length"):
        parse_ai_distractor_response(oversized_label, [], ["artist"])


@pytest.mark.asyncio
async def test_ai_distractor_request_rejects_oversized_prompt_before_provider_lookup() -> None:
    """Do not discover or call an AI provider for an oversized prompt."""
    mass = MagicMock()
    provider = MagicMock(spec=PluginProvider)
    provider.ai_query = AsyncMock(return_value="")
    mass.get_providers_supporting_feature.return_value = [provider]

    response = await request_ai_distractors(mass, "x" * (MAX_AI_PROMPT_BYTES + 1))

    assert response is None
    mass.get_providers_supporting_feature.assert_not_called()
    provider.ai_query.assert_not_awaited()
