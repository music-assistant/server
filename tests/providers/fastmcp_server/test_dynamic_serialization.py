"""Tests for dynamic Music Assistant response serialization."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import Enum
from pathlib import Path
from random import Random
from typing import Any
from uuid import UUID

from music_assistant.providers.fastmcp_server.dynamic_serialization import (
    bounded_json_value,
    json_value,
)


@dataclass(frozen=True)
class Artist:
    """A minimal MA artist-shaped model."""

    item_id: str
    name: str


class UniqueList(list[Artist]):
    """A collection that only accepts artist values."""

    def __init__(self, values: list[Artist]) -> None:
        """Initialise the collection with artist values."""
        if any(not isinstance(value, Artist) for value in values):
            raise TypeError("UniqueList accepts Artist values")
        super().__init__(values)


@dataclass
class Track:
    """A minimal MA track-shaped model."""

    uri: str
    artists: UniqueList


def test_dataclass_unique_list_is_converted_field_by_field() -> None:
    """Serialize dataclass fields without rebuilding custom collections."""
    value = Track("library://track/1", UniqueList([Artist("7", "Artist")]))

    assert json_value(value) == {
        "uri": "library://track/1",
        "artists": [{"item_id": "7", "name": "Artist"}],
    }


def test_to_dict_precedes_dataclass_conversion() -> None:
    """Prefer MA model serializers over dataclass fields."""

    @dataclass
    class Item:
        secret: str

        def to_dict(self) -> dict[str, str]:
            """Return the model's public representation."""
            return {"masked": "***"}

    assert json_value(Item("do-not-return")) == {"masked": "***"}


def test_set_output_is_deterministic() -> None:
    """Sort unordered values by their JSON representation."""
    assert json_value({"beta", "alpha"}) == ["alpha", "beta"]


def test_model_dump_precedes_dataclass_conversion() -> None:
    """Prefer Pydantic-style JSON model dumping over dataclass fields."""

    @dataclass
    class Item:
        secret: str

        def model_dump(self, *, mode: str) -> dict[str, str]:
            """Return the JSON-mode public representation."""
            assert mode == "json"
            return {"masked": "***"}

    assert json_value(Item("do-not-return")) == {"masked": "***"}


def test_common_scalar_and_container_values_are_json_safe() -> None:
    """Serialize scalar MA values inside nested mappings."""

    class State(Enum):
        PLAYING = "playing"

    value = {
        "nested": {
            "state": State.PLAYING,
            "day": date(2026, 7, 30),
            "moment": datetime(2026, 7, 30, 12, 45, 0, tzinfo=UTC),
            "identifier": UUID("12345678-1234-5678-1234-567812345678"),
            "path": Path("music/track.flac"),
            "values": frozenset({"z", "a"}),
        }
    }

    assert json_value(value) == {
        "nested": {
            "state": "playing",
            "day": "2026-07-30",
            "moment": "2026-07-30T12:45:00+00:00",
            "identifier": "12345678-1234-5678-1234-567812345678",
            "path": "music/track.flac",
            "values": ["a", "z"],
        }
    }


def test_repeated_non_cyclic_dataclass_is_serialized_each_time() -> None:
    """Only active recursive references are treated as cycles."""
    artist = Artist("7", "Artist")

    assert json_value([artist, artist]) == [
        {"item_id": "7", "name": "Artist"},
        {"item_id": "7", "name": "Artist"},
    ]


def test_cyclic_dataclass_uses_a_stable_cycle_marker() -> None:
    """Replace recursive object references with an explanatory marker."""

    @dataclass
    class Node:
        child: Node | None = None

    node = Node()
    node.child = node

    assert json_value(node) == {"child": f"<{Node.__module__}.{Node.__qualname__}:cycle>"}


def test_bounded_json_value_matches_seeded_reference_policy() -> None:
    """Bounded normalization preserves deterministic list, depth, and string policy."""
    random = Random(20260805)

    def payload(depth: int) -> Any:
        if depth == 0:
            return random.choice([None, True, random.randint(-20, 20), "x" * random.randint(0, 12)])
        kind = random.choice(["leaf", "list", "dict"])
        if kind == "leaf":
            return payload(0)
        if kind == "list":
            return [payload(depth - 1) for _index in range(random.randint(0, 6))]
        return {f"key-{index}": payload(depth - 1) for index in range(random.randint(0, 4))}

    def reference(value: Any, depth: int) -> tuple[Any, bool]:
        if isinstance(value, str):
            return (value[:8] + "…", True) if len(value) > 8 else (value, False)
        if isinstance(value, list | dict) and depth <= 0:
            return "[truncated]", True
        if isinstance(value, list):
            list_children = [reference(child, depth - 1) for child in value[:3]]
            return (
                [child for child, _changed in list_children],
                len(value) > 3 or any(changed for _child, changed in list_children),
            )
        if isinstance(value, dict):
            dict_children = {key: reference(child, depth - 1) for key, child in value.items()}
            return (
                {key: child for key, (child, _changed) in dict_children.items()},
                any(changed for _child, changed in dict_children.values()),
            )
        return value, False

    for _case in range(200):
        value = payload(4)
        expected_value, expected_truncated = reference(value, 4)
        normalized = bounded_json_value(value, item_cap=3, string_cap=8, max_depth=4)
        assert normalized.value == expected_value
        assert normalized.truncated is expected_truncated


def test_bounded_json_value_truncates_long_strings_at_the_prefix() -> None:
    """Long strings retain only their bounded prefix and an omission marker."""
    normalized = bounded_json_value("abcdefgh", item_cap=3, string_cap=4, max_depth=1)

    assert normalized.value == "abcd…"
    assert normalized.truncated is True
