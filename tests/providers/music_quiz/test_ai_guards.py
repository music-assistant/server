"""Tests for the shared Music Quiz AI limit guards."""

from __future__ import annotations

import pytest

from music_assistant.providers.music_quiz.ai_guards import (
    ai_prompt_exceeds_limit,
    validate_ai_response,
)
from music_assistant.providers.music_quiz.constants import (
    MAX_AI_PROMPT_BYTES,
    MAX_AI_RESPONSE_BYTES,
    MAX_AI_RESPONSE_LINES,
)


def test_ai_prompt_exceeds_limit_allows_a_prompt_at_the_limit() -> None:
    """Accept a prompt that is exactly at the byte limit."""
    assert ai_prompt_exceeds_limit("x" * MAX_AI_PROMPT_BYTES) is False
    assert ai_prompt_exceeds_limit("x" * (MAX_AI_PROMPT_BYTES + 1)) is True


def test_ai_prompt_exceeds_limit_counts_bytes_not_characters() -> None:
    """Measure a prompt in UTF-8 bytes, so multi-byte metadata cannot slip past."""
    at_limit = "é" * (MAX_AI_PROMPT_BYTES // 2)

    assert len(at_limit) < MAX_AI_PROMPT_BYTES
    assert ai_prompt_exceeds_limit(at_limit) is False
    assert ai_prompt_exceeds_limit(f"{at_limit}é") is True


def test_validate_ai_response_returns_a_response_at_the_limits() -> None:
    """Return an unmodified response that is exactly at the size and line limits."""
    at_size_limit = "x" * MAX_AI_RESPONSE_BYTES
    at_line_limit = "\n".join("x" for _ in range(MAX_AI_RESPONSE_LINES))

    assert validate_ai_response(at_size_limit) == at_size_limit
    assert validate_ai_response(at_line_limit) == at_line_limit


def test_validate_ai_response_rejects_a_non_string() -> None:
    """Reject a response an AI provider did not return as text."""
    with pytest.raises(TypeError, match="must be a string"):
        validate_ai_response(42)


def test_validate_ai_response_rejects_an_oversized_response() -> None:
    """Reject a response one byte over the size limit."""
    with pytest.raises(ValueError, match="size"):
        validate_ai_response("x" * (MAX_AI_RESPONSE_BYTES + 1))


def test_validate_ai_response_rejects_too_many_lines() -> None:
    """Reject a response one line over the line limit."""
    with pytest.raises(ValueError, match="line"):
        validate_ai_response("\n".join("x" for _ in range(MAX_AI_RESPONSE_LINES + 1)))
