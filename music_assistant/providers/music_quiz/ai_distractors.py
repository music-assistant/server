"""Strict AI request and response helpers for Music Quiz distractors."""

from __future__ import annotations

import asyncio
import logging
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from music_assistant.helpers.json import JSON_DECODE_EXCEPTIONS, json_loads, strip_code_fence
from music_assistant.helpers.plugin_engines import resolve_ai_engine
from music_assistant.providers.music_quiz.ai_guards import (
    ai_prompt_exceeds_limit,
    validate_ai_response,
)
from music_assistant.providers.music_quiz.constants import AI_QUERY_TIMEOUT_SECONDS
from music_assistant.providers.music_quiz.suggestions import answer_labels_are_too_close

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

LOGGER = logging.getLogger(__name__)

MAX_AI_CONTEXT_VALUE_LENGTH = 500
MAX_AI_LABEL_LENGTH = 200
MAX_AI_SYNTHETIC_COUNT = 12


@dataclass(frozen=True, slots=True)
class AISyntheticDistractor:
    """A validated synthetic wrong-answer label returned by an AI provider."""

    kind: str
    label: str


@dataclass(frozen=True, slots=True)
class AIDistractorResponse:
    """A validated AI ranking and synthetic distractor response."""

    ranked_ids: tuple[str, ...]
    synthetic: tuple[AISyntheticDistractor, ...]


async def request_ai_distractors(
    mass: MusicAssistant,
    prompt: str,
    *,
    engine_uid: str | None,
    timeout: float = AI_QUERY_TIMEOUT_SECONDS,
) -> object | None:
    """
    Request distractors from the configured AI engine.

    :param mass: Music Assistant instance used to discover AI engines.
    :param prompt: Bounded prompt to submit.
    :param engine_uid: The configured engine uid.
    :param timeout: Maximum request duration in seconds.
    :return: The untrusted engine response, or ``None`` when unavailable.
    """
    if ai_prompt_exceeds_limit(prompt):
        return None
    engine = await resolve_ai_engine(mass, engine_uid)
    if engine is None:
        return None
    try:
        async with asyncio.timeout(timeout):
            return await engine.provider.ai_query(prompt, engine_id=engine.id)
    except Exception as err:
        LOGGER.debug(
            "Music Quiz AI distractor request failed via %s (%s)",
            engine.uid,
            type(err).__name__,
        )
        return None


def parse_ai_distractor_response(
    response: object,
    candidate_ids: Sequence[str],
    expected_kinds: Sequence[str],
) -> AIDistractorResponse:
    """
    Parse one exact AI distractor response.

    :param response: Untrusted response returned by an AI provider.
    :param candidate_ids: Server-owned candidate IDs the response must rank.
    :param expected_kinds: Exact ordered synthetic distractor kinds requested.
    :return: Strictly validated ranking and synthetic labels.
    """
    response_text = validate_ai_response(response)
    if len(expected_kinds) > MAX_AI_SYNTHETIC_COUNT:
        raise ValueError("too many synthetic distractors were requested")
    if len(set(candidate_ids)) != len(candidate_ids):
        raise ValueError("candidate IDs must be unique")
    try:
        payload = json_loads(strip_code_fence(response_text))
    except JSON_DECODE_EXCEPTIONS as err:
        raise ValueError("response is not valid JSON") from err
    if not isinstance(payload, dict) or payload.keys() != {"ranked_ids", "synthetic"}:
        raise ValueError("response must contain exactly ranked_ids and synthetic")

    ranked_ids = payload["ranked_ids"]
    expected_candidate_ids = set(candidate_ids)
    if (
        not isinstance(ranked_ids, list)
        or len(ranked_ids) != len(candidate_ids)
        or any(not isinstance(candidate_id, str) for candidate_id in ranked_ids)
        or len(set(ranked_ids)) != len(ranked_ids)
        or set(ranked_ids) != expected_candidate_ids
    ):
        raise ValueError("ranked_ids must be a complete candidate permutation")

    synthetic = payload["synthetic"]
    if not isinstance(synthetic, list) or len(synthetic) != len(expected_kinds):
        raise ValueError("synthetic has an invalid shape")
    parsed_synthetic: list[AISyntheticDistractor] = []
    for raw_distractor, expected_kind in zip(synthetic, expected_kinds, strict=True):
        if not isinstance(raw_distractor, dict) or raw_distractor.keys() != {"kind", "label"}:
            raise ValueError("synthetic entries must contain exactly kind and label")
        kind = raw_distractor["kind"]
        if not isinstance(kind, str) or kind != expected_kind:
            raise ValueError("synthetic distractor kind does not match the request")
        label = _validate_ai_label(raw_distractor["label"])
        if any(answer_labels_are_too_close(label, existing.label) for existing in parsed_synthetic):
            raise ValueError("synthetic distractor labels must be distinct")
        parsed_synthetic.append(AISyntheticDistractor(kind=kind, label=label))
    return AIDistractorResponse(
        ranked_ids=tuple(ranked_ids),
        synthetic=tuple(parsed_synthetic),
    )


def bounded_ai_context(value: str | None) -> str | None:
    """
    Bound an untrusted metadata value before including it in an AI prompt.

    :param value: Metadata value to bound.
    :return: The bounded value, if present.
    """
    if value is None:
        return None
    return value[:MAX_AI_CONTEXT_VALUE_LENGTH]


def _validate_ai_label(raw_label: object) -> str:
    """Return a strict single-line synthetic label."""
    if not isinstance(raw_label, str):
        raise TypeError("synthetic distractor labels must be strings")
    if not raw_label or raw_label != raw_label.strip():
        raise ValueError("synthetic distractor labels must be non-empty and trimmed")
    if len(raw_label) > MAX_AI_LABEL_LENGTH:
        raise ValueError("synthetic distractor label exceeds the length limit")
    if any(
        unicodedata.category(character).startswith("C")
        or unicodedata.category(character) in {"Zl", "Zp"}
        for character in raw_label
    ):
        raise ValueError("synthetic distractor labels must not contain control characters")
    return raw_label
