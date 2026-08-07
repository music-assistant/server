"""Tests for the json helpers."""

from __future__ import annotations

import datetime
import enum
import uuid
from typing import Any

import pytest

from music_assistant.helpers.json import get_serializable_value, json_dumps, serialize_to_json


class _WithToDict:
    def to_dict(self) -> dict[str, Any]:
        return {"x": 1}


class _Unhandled:
    """Stands in for a raw third party SDK object without a to_dict method."""


class _Colour(enum.StrEnum):
    RED = "red"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("hello", '"hello"'),
        ({}, "{}"),
        (["a", "b"], '["a","b"]'),
        ({"a": [1, 2], "b": {"c": None}}, '{"a":[1,2],"b":{"c":null}}'),
        ([1, True, 1.5], "[1,true,1.5]"),
        ((1, 2), "[1,2]"),
        (b"\x00\x01", '"AAE="'),
        (datetime.date(2026, 1, 1), '"2026-01-01"'),
        (uuid.UUID(int=0), '"00000000-0000-0000-0000-000000000000"'),
        (_Colour.RED, '"red"'),
        ([datetime.date(2026, 1, 1)], '["2026-01-01"]'),
    ],
)
def test_serialize_to_json_passes_through_supported_values(value: Any, expected: str) -> None:
    """Values that orjson handles itself are not rejected by the unhandled type check."""
    assert serialize_to_json(value) == expected


def test_serialize_to_json_uses_to_dict() -> None:
    """Objects exposing to_dict are converted through it."""
    assert serialize_to_json(_WithToDict()) == '{"x":1}'
    assert serialize_to_json([_WithToDict()]) == '[{"x":1}]'


def test_dict_value_iterator_is_serialized_as_list() -> None:
    """A dict value iterator is consumed into a list rather than left unhandled."""
    assert serialize_to_json(iter({"a": 1}.values())) == "[1]"


def test_unhandled_type_reports_the_offending_type() -> None:
    """An object nothing can convert raises an error naming the type, not a recursion error."""
    with pytest.raises(TypeError) as excinfo:
        json_dumps({"tracks": [_Unhandled()]})

    assert "recursion" not in str(excinfo.value).lower()
    cause = excinfo.value.__cause__
    assert cause is not None
    # the module path matters: several providers ship classes named like our own models
    assert f"{__name__}._Unhandled" in str(cause)


def test_get_serializable_value_still_returns_unhandled_values() -> None:
    """The direct helper stays lenient, only the orjson hook raises."""
    obj = _Unhandled()
    assert get_serializable_value(obj) is obj
