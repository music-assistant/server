"""Tests for type[X] parameter handling in API argument parsing.

The type[X] parameters (e.g. return_type: type[ConfigValueType]) exist only for
static type checking via @overload. They must be skipped by parse_arguments so
that arbitrary strings from API input are never resolved to types.
"""

from __future__ import annotations

import inspect
from typing import get_type_hints

import pytest

from music_assistant.helpers.api import parse_arguments, parse_value


def _example_with_type_param(
    instance_id: str,
    key: str,
    return_type: type[int] | None = None,
) -> None:
    """Simulate a config API method with a type[X] parameter."""


class TestParseArgumentsSkipsTypeParams:
    """Test that parse_arguments skips type[X] parameters."""

    def test_type_param_not_in_parsed_args(self) -> None:
        """type[X] parameters should not appear in parsed output."""
        sig = inspect.signature(_example_with_type_param)
        hints = get_type_hints(_example_with_type_param)
        result = parse_arguments(sig, hints, {"instance_id": "test", "key": "foo"})
        assert "return_type" not in result
        assert result["instance_id"] == "test"
        assert result["key"] == "foo"

    def test_type_param_ignored_even_if_provided(self) -> None:
        """Even if API input includes a type[X] value, it should be ignored."""
        sig = inspect.signature(_example_with_type_param)
        hints = get_type_hints(_example_with_type_param)
        result = parse_arguments(
            sig,
            hints,
            {"instance_id": "test", "key": "foo", "return_type": "str"},
        )
        assert "return_type" not in result


class TestParseValueTypeRejected:
    """Test that parse_value rejects type[X] as a safeguard."""

    @pytest.mark.parametrize("value", ["str", "int", "list[str]"])
    def test_type_resolution_rejected(self, value: str) -> None:
        """Direct calls to parse_value with type[X] should raise ValueError."""
        with pytest.raises(ValueError, match="Cannot resolve type from string"):
            parse_value("return_type", value, type[object])
