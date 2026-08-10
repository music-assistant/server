"""Tests for collection type handling in API argument parsing."""

from __future__ import annotations

from typing import Any

import pytest
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


def test_parse_value_none_members_dropped_from_list() -> None:
    """None members are dropped when the element type of a list does not admit None."""
    result = parse_value("values", ["a", None, "b"], list[str])
    assert result == ["a", "b"]


def test_parse_value_none_members_kept_for_optional_element_type() -> None:
    """None members are kept when the element type of a list admits None."""
    result = parse_value("values", ["a", None, "b"], list[str | None])
    assert result == ["a", None, "b"]


def test_parse_value_none_member_kept_in_set_of_optional() -> None:
    """A None member is kept for a set with an element type that admits None."""
    result = parse_value("values", ["a", None], set[str | None])
    assert result == {"a", None}


def test_parse_value_none_member_kept_in_variadic_tuple_of_optional() -> None:
    """A None member is kept for a variadic tuple with an element type that admits None."""
    result = parse_value("values", ["a", None, "b"], tuple[str | None, ...])
    assert result == ("a", None, "b")


def test_parse_value_fixed_length_tuple_per_position_types() -> None:
    """Each member of a fixed-length tuple is parsed against the type of its own position."""
    result = parse_value("values", ["a", 1, "party"], tuple[str, int, DashboardType])
    assert result == ("a", 1, DashboardType.PARTY)


def test_parse_value_fixed_length_tuple_keeps_none_members() -> None:
    """A None member of a fixed-length tuple is kept, so the tuple keeps its length."""
    result = parse_value("values", ["a", "name", None], tuple[str, str, str | None])
    assert result == ("a", "name", None)


def test_parse_value_list_of_fixed_length_tuples() -> None:
    """A json list of lists is parsed into tuples that each keep their None members."""
    result = parse_value(
        "stations",
        [["t1", "Name A", "http://img"], ["t2", "Name B", None]],
        list[tuple[str, str, str | None]],
    )
    assert result == [("t1", "Name A", "http://img"), ("t2", "Name B", None)]


def test_parse_value_variadic_tuple() -> None:
    """A json list is parsed into a tuple of arbitrary length for a variadic annotation."""
    result = parse_value("values", ["a", "b", "c"], tuple[str, ...])
    assert result == ("a", "b", "c")


def test_parse_value_fixed_length_tuple_wrong_length() -> None:
    """A value with the wrong number of members is rejected for a fixed-length tuple."""
    with pytest.raises(TypeError, match="is invalid for values"):
        parse_value("values", ["a"], tuple[str, str])


def test_parse_value_fixed_length_tuple_names_the_failing_member() -> None:
    """A member that does not fit its type is reported with its position in the tuple."""
    with pytest.raises(TypeError, match=r"invalid for values\[1\]"):
        parse_value("values", ["a", {}], tuple[str, str])


def test_parse_value_fixed_length_tuple_in_union() -> None:
    """A union of fixed-length tuples falls through to the member that fits the value."""
    result = parse_value("values", ["a", "b", "c"], tuple[str, str] | tuple[str, str, str])
    assert result == ("a", "b", "c")


def test_parse_value_dict_names_the_failing_value() -> None:
    """A dict value that does not fit its type is reported with its argument and key."""
    with pytest.raises(TypeError, match=r"invalid for supported_types\[a\]"):
        parse_value("supported_types", {"a": {}}, dict[str, int])


def test_parse_value_dict_names_the_failing_key() -> None:
    """A dict key that does not fit its type is reported with the argument it belongs to."""
    with pytest.raises(TypeError, match="invalid for supported_types key"):
        parse_value("supported_types", {1: "a"}, dict[str, str])
