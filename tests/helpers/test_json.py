"""Tests for the JSON helpers."""

from __future__ import annotations

import dataclasses
import datetime
import enum
import uuid
from typing import Any

import pytest
from mashumaro.mixins.orjson import DataClassORJSONMixin

from music_assistant.helpers.json import (
    get_serializable_value,
    json_dumps,
    make_utf8_safe,
    serialize_to_json,
    strip_code_fence,
)

# a filename byte that is not valid UTF-8 arrives as a lone surrogate, here the
# latin-1 encoded "ß" of a folder named "Straße"
UNDECODABLE_PATH = "Musik/Stra\udcdfe/cover.jpg"
ESCAPED_PATH = "Musik/Stra\\xdfe/cover.jpg"


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
    Return long runs of backticks unchanged instead of scanning them for a fence body.

    Matching a fence with a backtracking pattern took seconds on these, which would block
    the event loop of a caller parsing an AI response.
    """
    assert strip_code_fence("`" * 4096) == "`" * 4096
    assert strip_code_fence("`" * 4087 + "jsonjson") == "`" * 4087 + "jsonjson"


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


@dataclasses.dataclass
class _Message(DataClassORJSONMixin):
    """Stands in for the (mashumaro) api message models."""

    text: str


def test_make_utf8_safe_escapes_lone_surrogates() -> None:
    """Strings that are not valid UTF-8 are replaced by their escaped form."""
    assert make_utf8_safe(UNDECODABLE_PATH) == ESCAPED_PATH
    # nested containers are covered as well, keys included
    assert make_utf8_safe({UNDECODABLE_PATH: [(UNDECODABLE_PATH,)]}) == {
        ESCAPED_PATH: [(ESCAPED_PATH,)]
    }
    # objects only expose their strings once converted
    assert make_utf8_safe(_Message(UNDECODABLE_PATH)) == {"text": ESCAPED_PATH}
    # a lone surrogate that does not stand for a byte has to be escaped as itself
    assert make_utf8_safe("half a pair: \ud800").encode() == b"half a pair: \\ud800"


@pytest.mark.parametrize("value", ["plain", "Straße", "日本語", "", {"a": [1, None]}])
def test_make_utf8_safe_leaves_valid_values_untouched(value: Any) -> None:
    """Anything that is already valid UTF-8 comes back unchanged."""
    assert make_utf8_safe(value) == value
