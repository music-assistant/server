"""Tests for collection type handling in API argument parsing."""

from __future__ import annotations

from typing import Any

from music_assistant.helpers.api import parse_value


def test_parse_value_none_for_optional_dict() -> None:
    """A None value is returned as-is when the annotation is dict[str, Any] | None."""
    result = parse_value("supported_types", None, dict[str, Any] | None)
    assert result is None


def test_parse_value_none_for_optional_list() -> None:
    """A None value is returned as-is when the annotation is list[str] | None."""
    result = parse_value("values", None, list[str] | None)
    assert result is None


def test_parse_value_dict_for_optional_dict() -> None:
    """A real dict is still parsed correctly when the annotation is dict[str, Any] | None."""
    result = parse_value("supported_types", {"a": 1}, dict[str, Any] | None)
    assert result == {"a": 1}
