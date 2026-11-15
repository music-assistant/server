"""Tests for value conversion and union fallthrough in API argument parsing."""

from __future__ import annotations

import inspect
import logging
from typing import get_type_hints

import pytest
from music_assistant_models.config_entries import ConfigValueType

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


def test_rejected_value_is_left_out_of_the_error() -> None:
    """The error for a value of the wrong type names the argument, not the value."""
    with pytest.raises(TypeError, match="is invalid for password") as err:
        parse_value("password", ["made-up-password"], str)
    assert "made-up-password" not in str(err.value)


def test_rejected_optional_value_is_left_out_of_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The warning for a value that fits no member of an optional union leaves out the value."""
    with caplog.at_level(logging.WARNING):
        assert parse_value("password", ["made-up-password"], str | None) is None
    assert "is invalid for password" in caplog.text
    assert "made-up-password" not in caplog.text


@pytest.mark.parametrize("value", [2346, 0])
def test_int_accepted_for_optional_float(value: int) -> None:
    """A JSON int is accepted for an optional float annotation."""
    result = parse_value("start_position", value, float | None)
    assert isinstance(result, float)
    assert result == float(value)


def test_float_accepted_for_optional_float() -> None:
    """A float is passed through for an optional float annotation."""
    assert parse_value("start_position", 12.5, float | None) == 12.5


def test_none_accepted_for_optional_float() -> None:
    """None is passed through for an optional float annotation."""
    assert parse_value("start_position", None, float | None) is None


def test_bool_not_accepted_for_float() -> None:
    """A bool is not turned into a float."""
    with pytest.raises(TypeError, match="is invalid for gain"):
        parse_value("gain", True, float)


def test_ints_accepted_for_optional_float_list() -> None:
    """JSON ints in a list are accepted for an optional list of floats."""
    result = parse_value("seed_weights", [1, 2], list[float] | None)
    assert result == [1.0, 2.0]
    assert all(isinstance(item, float) for item in result)


def test_int_stays_int_for_config_value() -> None:
    """An int config value is not turned into a float by the float member of the union."""
    result = parse_value("baudrate", 115200, ConfigValueType)
    assert type(result) is int
    assert result == 115200


def test_int_list_stays_int_list_for_config_value() -> None:
    """A list of int config values is not turned into a list of floats."""
    result = parse_value("ports", [1, 2], ConfigValueType)
    assert result == [1, 2]
    assert all(type(item) is int for item in result)
