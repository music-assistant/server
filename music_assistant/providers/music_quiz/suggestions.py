"""Suggestion helpers for the Music Quiz provider."""

from __future__ import annotations

import random
import re
import secrets
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher

from music_assistant.providers.music_quiz.models import MusicQuizSuggestion

# collapse runs of non-word characters and underscores so filename-style titles
# ("Foo_Bar") normalize like their spaced form ("Foo Bar"); \W keeps this
# Unicode-aware so non-Latin titles (e.g. CJK, Cyrillic) survive instead of becoming ""
NORMALIZE_PATTERN = re.compile(r"[\W_]+")
MAX_LABEL_SIMILARITY = 0.78
MAX_TOKEN_CONTAINMENT = 0.85


@dataclass(frozen=True)
class SuggestionCandidate:
    """A candidate track answer for Music Quiz suggestions."""

    label: str
    uri: str | None = None
    title: str | None = None


def normalize_answer_label(label: str) -> str:
    """
    Normalize an answer label for duplicate detection.

    :param label: Answer label to normalize.
    """
    return NORMALIZE_PATTERN.sub(" ", label.casefold()).strip()


def answer_labels_are_too_close(first_label: str, second_label: str) -> bool:
    """
    Return if two answer labels are too similar to use together.

    :param first_label: First answer label to compare.
    :param second_label: Second answer label to compare.
    """
    first = normalize_answer_label(first_label)
    second = normalize_answer_label(second_label)
    if not first or not second:
        return False
    if first == second:
        return True

    similarity = SequenceMatcher(None, first, second).ratio()
    if similarity >= MAX_LABEL_SIMILARITY:
        return True

    first_tokens = set(first.split())
    second_tokens = set(second.split())
    shared_tokens = first_tokens & second_tokens
    token_containment = len(shared_tokens) / min(len(first_tokens), len(second_tokens))
    return token_containment >= MAX_TOKEN_CONTAINMENT


def suggestion_candidates_are_too_close(
    first: SuggestionCandidate,
    second: SuggestionCandidate,
) -> bool:
    """
    Return if two candidates are too similar to use together.

    Prefer comparing raw track titles when available so artists with similar
    names do not dominate the distance check.
    """
    if first.title and second.title:
        return answer_labels_are_too_close(first.title, second.title)
    return answer_labels_are_too_close(first.label, second.label)


def build_answer_label(artist: str | None, title: str) -> str:
    """
    Build the displayed answer label.

    :param artist: Artist name.
    :param title: Track title.
    """
    if artist:
        return f"{artist} - {title}"
    return title


def build_suggestions(
    correct: SuggestionCandidate,
    distractors: Iterable[SuggestionCandidate],
    suggestion_count: int,
    *,
    rng: random.Random | None = None,
) -> list[MusicQuizSuggestion]:
    """
    Build shuffled suggestions containing exactly one correct answer.

    :param correct: Correct answer candidate.
    :param distractors: Wrong answer candidates.
    :param suggestion_count: Total number of suggestions to return.
    :param rng: Optional random generator.
    """
    if suggestion_count < 2:
        msg = "Suggestion count must be at least 2"
        raise ValueError(msg)

    selected = _select_distractors(correct, distractors, suggestion_count - 1)
    # suggestion IDs are sent to guests while the answer is still secret: they
    # must be opaque, never semantic ("correct"/"wrong_x" would leak the answer)
    suggestions = [
        MusicQuizSuggestion(
            suggestion_id=secrets.token_hex(8),
            label=correct.label,
            uri=correct.uri,
            is_correct=True,
        ),
        *[
            MusicQuizSuggestion(
                suggestion_id=secrets.token_hex(8),
                label=candidate.label,
                uri=candidate.uri,
            )
            for candidate in selected
        ],
    ]
    (rng or random).shuffle(suggestions)
    return suggestions


def _select_distractors(
    correct: SuggestionCandidate,
    distractors: Iterable[SuggestionCandidate],
    needed_count: int,
) -> Sequence[SuggestionCandidate]:
    """Return unique distractors that do not match the correct answer."""
    correct_label = normalize_answer_label(correct.label)
    seen_labels = {correct_label}
    seen_uris = {correct.uri} if correct.uri else set()
    selected: list[SuggestionCandidate] = []
    for candidate in distractors:
        candidate_label = normalize_answer_label(candidate.label)
        if not candidate_label or candidate_label in seen_labels:
            continue
        if any(
            suggestion_candidates_are_too_close(candidate, selected_candidate)
            for selected_candidate in (correct, *selected)
        ):
            continue
        if candidate.uri and candidate.uri in seen_uris:
            continue
        seen_labels.add(candidate_label)
        if candidate.uri:
            seen_uris.add(candidate.uri)
        selected.append(candidate)
        if len(selected) == needed_count:
            break
    if len(selected) < needed_count:
        msg = "Not enough distractors to build suggestions"
        raise ValueError(msg)
    return selected
