"""Suggestion helpers for the Music Quiz provider."""

from __future__ import annotations

import random
import re
import secrets
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher

from music_assistant.providers.music_quiz.models import MultipleChoiceSuggestion

# collapse runs of non-word characters and underscores so filename-style titles
# ("Foo_Bar") normalize like their spaced form ("Foo Bar"); \W keeps this
# Unicode-aware so non-Latin titles (e.g. CJK, Cyrillic) survive instead of becoming ""
NORMALIZE_PATTERN = re.compile(r"[\W_]+")
MAX_LABEL_SIMILARITY = 0.78
MAX_TOKEN_CONTAINMENT = 0.85
SYSTEM_RANDOM = secrets.SystemRandom()


@dataclass(frozen=True)
class SuggestionCandidate:
    """A candidate track answer for Music Quiz suggestions."""

    label: str
    uri: str | None = None
    title: str | None = None
    artist_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class OpaqueOption:
    """An answer option with an opaque client-visible identity."""

    option_id: str
    label: str
    uri: str | None
    is_correct: bool


def normalize_answer_label(label: str) -> str:
    """
    Normalize an answer label for duplicate detection.

    :param label: Answer label to normalize.
    """
    normalized_label = unicodedata.normalize("NFKC", label).casefold()
    return NORMALIZE_PATTERN.sub(" ", normalized_label).strip()


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
) -> list[MultipleChoiceSuggestion]:
    """
    Build shuffled suggestions containing exactly one correct answer.

    :param correct: Correct answer candidate.
    :param distractors: Wrong answer candidates.
    :param suggestion_count: Total number of suggestions to return.
    :param rng: Optional random generator.
    """
    return [
        MultipleChoiceSuggestion(
            suggestion_id=option.option_id,
            label=option.label,
            uri=option.uri,
            is_correct=option.is_correct,
        )
        for option in build_opaque_options(
            correct,
            distractors,
            suggestion_count,
            rng=rng,
        )
    ]


def build_opaque_options(
    correct: SuggestionCandidate,
    distractors: Iterable[SuggestionCandidate],
    option_count: int,
    *,
    rng: random.Random | None = None,
) -> list[OpaqueOption]:
    """
    Build shuffled answer options with opaque IDs and one correct answer.

    :param correct: Correct answer candidate.
    :param distractors: Wrong answer candidates.
    :param option_count: Total number of options to return.
    :param rng: Optional random generator.
    """
    if option_count < 2:
        msg = "Suggestion count must be at least 2"
        raise ValueError(msg)

    selected = _select_distractors(correct, distractors, option_count - 1)
    # option IDs are sent to guests while the answer is still secret: they
    # must be opaque, never semantic ("correct"/"wrong_x" would leak the answer)
    options = [
        OpaqueOption(
            option_id=secrets.token_hex(8),
            label=correct.label,
            uri=correct.uri,
            is_correct=True,
        ),
        *[
            OpaqueOption(
                option_id=secrets.token_hex(8),
                label=candidate.label,
                uri=candidate.uri,
                is_correct=False,
            )
            for candidate in selected
        ],
    ]
    (rng or SYSTEM_RANDOM).shuffle(options)
    return options


def has_enough_distractors(
    correct: SuggestionCandidate,
    distractors: Iterable[SuggestionCandidate],
    option_count: int,
) -> bool:
    """
    Return whether candidates can fill every wrong option.

    :param correct: Correct answer candidate.
    :param distractors: Wrong answer candidates.
    :param option_count: Total number of options that must be built.
    """
    if option_count < 2:
        return False
    try:
        _select_distractors(correct, distractors, option_count - 1)
    except ValueError:
        return False
    return True


def filter_suggestion_candidates(
    correct: SuggestionCandidate,
    distractors: Iterable[SuggestionCandidate],
    *,
    limit: int | None = None,
) -> list[SuggestionCandidate]:
    """
    Return unique distractors that stay distinct from the correct answer.

    :param correct: Correct answer candidate.
    :param distractors: Ordered wrong-answer candidates to filter.
    :param limit: Maximum number of candidates to return.
    """
    if limit is not None and limit < 1:
        return []
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
        if limit is not None and len(selected) >= limit:
            break
    return selected


def _select_distractors(
    correct: SuggestionCandidate,
    distractors: Iterable[SuggestionCandidate],
    needed_count: int,
) -> Sequence[SuggestionCandidate]:
    """Return unique distractors that do not match the correct answer."""
    selected = filter_suggestion_candidates(correct, distractors, limit=needed_count)
    if len(selected) < needed_count:
        msg = "Not enough distractors to build suggestions"
        raise ValueError(msg)
    return selected
