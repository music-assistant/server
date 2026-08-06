"""Tests for the JSON helpers."""

from __future__ import annotations

import pytest

from music_assistant.helpers.json import strip_code_fence


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('```json\n{"a": 1}\n```', '{"a": 1}'),
        ('```JSON\n{"a": 1}\n```', '{"a": 1}'),
        ('```\n{"a": 1}\n```', '{"a": 1}'),
        ('````json\n{"a": 1}\n````', '{"a": 1}'),
        ('  ```json\r\n{"a": 1}\r\n```  ', '{"a": 1}'),
        ('```json\n\n{"a": 1}\n\n```', '{"a": 1}'),
    ],
)
def test_strip_code_fence_unwraps_a_single_fenced_block(text: str, expected: str) -> None:
    """Return the content of one code fence wrapping the complete text."""
    assert strip_code_fence(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        '{"a": 1}',
        "not fenced at all",
        # A fence around only part of the text must not be unwrapped.
        'Here you go:\n```json\n{"a": 1}\n```',
        '```json\n{"a": 1}\n```\nHope that helps!',
        '```json\nnot json\n```\n```json\n{"a": 1}\n```',
        # An unterminated or mismatched fence is not a fenced block.
        '```json\n{"a": 1}',
        '````json\n{"a": 1}\n```',
        '```json\n{"a": 1}\n````',
        "```",
        "``````",
        # The opening line must carry nothing but a language tag.
        '```json {"a": 1}\n```',
        '```{"a": 1}\n```',
    ],
)
def test_strip_code_fence_only_trims_whitespace_of_other_text(text: str) -> None:
    """Leave text that is not exactly one fenced block untouched apart from whitespace."""
    assert strip_code_fence(f"  {text}  ") == text


def test_strip_code_fence_handles_long_backtick_runs() -> None:
    """
    Return a long run of backticks unchanged, without scanning for a fence body.

    Guards the linear scan: matching a fence with a backtracking pattern made such input
    take seconds, which would block the event loop for a caller parsing an AI response.
    """
    assert strip_code_fence("`" * 4096) == "`" * 4096
    assert strip_code_fence("`" * 4087 + "jsonjson") == "`" * 4087 + "jsonjson"
