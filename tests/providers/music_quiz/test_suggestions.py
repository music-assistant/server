"""Tests for Music Quiz suggestion helpers."""

from __future__ import annotations

import random
from unittest.mock import patch

import pytest

from music_assistant.providers.music_quiz.suggestions import (
    SuggestionCandidate,
    answer_labels_are_too_close,
    build_answer_label,
    build_suggestions,
    normalize_answer_label,
    parse_ai_distractors,
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


def test_parse_ai_distractors_parses_artist_title_lines() -> None:
    """Parse plain 'Artist - Title' lines into candidates."""
    result = parse_ai_distractors("Daft Punk - Aerodynamic\nJustice - Genesis")
    assert [(item.label, item.title) for item in result] == [
        ("Daft Punk - Aerodynamic", "Aerodynamic"),
        ("Justice - Genesis", "Genesis"),
    ]


def test_parse_ai_distractors_strips_numbering_bullets_and_quotes() -> None:
    """Tolerate list markers, numbering and surrounding quotes."""
    text = '1. Daft Punk - Aerodynamic\n- Justice - Genesis\n* Air - La Femme\n"Phoenix - 1901"'
    assert [item.label for item in parse_ai_distractors(text)] == [
        "Daft Punk - Aerodynamic",
        "Justice - Genesis",
        "Air - La Femme",
        "Phoenix - 1901",
    ]


def test_parse_ai_distractors_supports_dash_variants() -> None:
    """Accept en dash and em dash separators between artist and title."""
    result = parse_ai_distractors("Daft Punk \u2013 Aerodynamic\nJustice \u2014 Genesis")
    assert [item.title for item in result] == ["Aerodynamic", "Genesis"]


def test_parse_ai_distractors_drops_lines_without_separator() -> None:
    """Discard preamble/commentary lines that are not 'Artist - Title'."""
    text = "Here are some options:\nDaft Punk - Aerodynamic\n\nHope that helps!"
    assert [item.label for item in parse_ai_distractors(text)] == ["Daft Punk - Aerodynamic"]


def test_parse_ai_distractors_empty_or_unusable_returns_empty() -> None:
    """Return an empty list for empty or unusable output."""
    assert parse_ai_distractors("") == []
    assert parse_ai_distractors("Sorry, I cannot help with that.") == []
