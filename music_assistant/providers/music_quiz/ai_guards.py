"""AI request and response limit guards for the Music Quiz provider."""

from __future__ import annotations

from music_assistant.providers.music_quiz.constants import (
    MAX_AI_PROMPT_BYTES,
    MAX_AI_RESPONSE_BYTES,
    MAX_AI_RESPONSE_LINES,
)


def ai_prompt_exceeds_limit(prompt: str) -> bool:
    """
    Return whether a prompt is too large to submit to an AI engine.

    :param prompt: Prompt the caller intends to submit.
    """
    return len(prompt.encode("utf-8")) > MAX_AI_PROMPT_BYTES


def validated_ai_response(response: object) -> str:
    """
    Return the AI response text, if it is within the shared response limits.

    :param response: Untrusted response returned by an AI provider.
    :raises TypeError: If the response is not a string.
    :raises ValueError: If the response exceeds the size or line limit.
    """
    if not isinstance(response, str):
        raise TypeError("response must be a string")
    if len(response.encode("utf-8")) > MAX_AI_RESPONSE_BYTES:
        raise ValueError("response exceeds the size limit")
    if len(response.splitlines()) > MAX_AI_RESPONSE_LINES:
        raise ValueError("response exceeds the line limit")
    return response
