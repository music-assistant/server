"""Tests for collection type handling in API argument parsing."""

from __future__ import annotations

from typing import Any

from music_assistant_models.enums import DashboardType

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


def test_parse_value_set_of_enum() -> None:
    """A json list is parsed into a set when the annotation is set[Enum]."""
    result = parse_value("supported_types", ["party"], set[DashboardType])
    assert result == {DashboardType.PARTY}


def test_parse_value_optional_set_of_enum() -> None:
    """A json list is parsed into a set for an optional set[Enum] annotation."""
    result = parse_value("supported_types", ["party", "now_playing"], set[DashboardType] | None)
    assert result == {DashboardType.PARTY, DashboardType.NOW_PLAYING}


def test_parse_value_frozenset_of_str() -> None:
    """A json list is parsed into a frozenset when the annotation is frozenset[str]."""
    result = parse_value("values", ["a", "b"], frozenset[str])
    assert result == frozenset({"a", "b"})
