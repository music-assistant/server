"""Tests for value conversion and union fallthrough in API argument parsing."""

from __future__ import annotations

import inspect
import logging
from typing import get_type_hints

import pytest

from music_assistant.helpers.api import parse_arguments, parse_value


def test_numeric_string_converted_to_int() -> None:
    """A numeric string is accepted for an int annotation when conversion is allowed."""
    assert parse_value("volume", "5", int, allow_value_convert=True) == 5


def test_int_converted_to_float() -> None:
    """An int is accepted for a float annotation when conversion is allowed."""
    result = parse_value("gain", 1, float, allow_value_convert=True)
    assert isinstance(result, float)
    assert result == 1.0


def test_numeric_string_converted_to_float() -> None:
    """A numeric string is accepted for a float annotation when conversion is allowed."""
    assert parse_value("gain", "5", float, allow_value_convert=True) == 5.0


def test_float_converted_to_int() -> None:
    """A float is truncated for an int annotation when conversion is allowed."""
    assert parse_value("volume", 1.9, int, allow_value_convert=True) == 1


@pytest.mark.parametrize(
    ("value", "expected"),
    [("true", True), ("1", True), (1, True), ("false", False), ("nope", False), (0, False)],
)
def test_string_and_int_converted_to_bool(value: str | int, expected: bool) -> None:
    """A string or int is accepted for a bool annotation when conversion is allowed."""
    assert parse_value("enabled", value, bool, allow_value_convert=True) is expected


def test_no_conversion_without_allow_value_convert() -> None:
    """A value of the wrong type is rejected while conversion is not allowed."""
    with pytest.raises(TypeError, match="is invalid for volume"):
        parse_value("volume", "5", int)


def test_value_without_a_conversion_is_still_rejected() -> None:
    """A value that no conversion applies to is rejected even when conversion is allowed."""
    with pytest.raises(TypeError, match="is invalid for name"):
        parse_value("name", 5, str, allow_value_convert=True)


def test_parse_arguments_retries_with_conversion() -> None:
    """Arguments that only fit after conversion are accepted by the retry."""

    def _handler(volume: int, gain: float) -> None: ...

    result = parse_arguments(
        inspect.signature(_handler),
        get_type_hints(_handler),
        {"volume": "5", "gain": 1},
    )
    assert result == {"volume": 5, "gain": 1.0}


def test_union_that_fits_no_member_raises() -> None:
    """A value that fits no member of a union without None is rejected."""
    with pytest.raises(TypeError, match="is invalid for values"):
        parse_value("values", {}, int | str)


def test_optional_union_that_fits_no_member_warns_and_returns_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A value that fits no member of an optional union is logged and dropped."""
    with caplog.at_level(logging.WARNING):
        assert parse_value("values", {}, int | str | None) is None
    assert "is invalid for values" in caplog.text
