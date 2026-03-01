"""Tests for parse_value handling of type[X] annotations.

The ``origin is type`` branch in parse_value resolves a type name string
(e.g. "str", "list[int]") to the actual Python type.
"""

from __future__ import annotations

from types import NoneType

import pytest
from music_assistant_models.media_items.media_item import MediaItem

from music_assistant.helpers.api import parse_value

# Mirrors the real annotation from config.py: type[_ConfigValueT | ConfigValueType] | None
_CONFIG_RETURN_TYPE = type[
    bool | float | int | str | list[float] | list[int] | list[str] | list[bool] | None
]


class TestParseValueTypeBuiltins:
    """Test resolution of builtin type names."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("str", str),
            ("int", int),
            ("float", float),
            ("bool", bool),
            ("list", list),
            ("dict", dict),
            ("tuple", tuple),
            ("set", set),
            ("NoneType", NoneType),
        ],
    )
    def test_builtin_types_resolve(self, value: str, expected: type) -> None:
        """Builtin type names should resolve to their type objects."""
        result = parse_value("return_type", value, _CONFIG_RETURN_TYPE)
        assert result is expected


class TestParseValueTypeParameterized:
    """Test resolution of parameterized generic types like list[str]."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("list[str]", list[str]),
            ("list[int]", list[int]),
            ("dict[str, int]", dict[str, int]),
            ("tuple[int, ...]", tuple[int, ...]),
            ("dict[str, list[int]]", dict[str, list[int]]),
            ("set[str]", set[str]),
        ],
    )
    def test_parameterized_types_resolve(self, value: str, expected: type) -> None:
        """Parameterized generics should resolve correctly."""
        result = parse_value("return_type", value, _CONFIG_RETURN_TYPE)
        assert result == expected


class TestParseValueTypeModuleScope:
    """Test that types imported in the api module's scope resolve correctly."""

    def test_media_item_resolves(self) -> None:
        """MediaItem (imported in api.py) should resolve."""
        result = parse_value("return_type", "MediaItem", _CONFIG_RETURN_TYPE)
        assert result is MediaItem


class TestParseValueTypeRejectsInvalid:
    """Test that non-type strings are rejected."""

    @pytest.mark.parametrize(
        "value",
        [
            "not_a_type",
            "hello world",
            "",
            "???",
            "foo.bar",
        ],
    )
    def test_invalid_strings_rejected(self, value: str) -> None:
        """Non-type strings must raise ValueError."""
        with pytest.raises((ValueError, TypeError)):
            parse_value("return_type", value, _CONFIG_RETURN_TYPE)
