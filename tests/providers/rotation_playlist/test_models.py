"""Tests for the Rotation steps document model."""

from __future__ import annotations

from typing import Any

import pytest

from music_assistant.providers.rotation_playlist.models import (
    ItemKind,
    SourceStep,
    StepsError,
    item_kinds,
    parse_steps,
    steps_json_schema,
    steps_to_dict,
)

DIE_DREI: list[dict[str, Any]] = [
    {"type": "source", "media_type": "album", "from": "library"},
    {
        "type": "filter",
        "mode": "keep",
        "match": "all",
        "conditions": [{"field": "artist", "op": "is", "value": "Die drei ???"}],
    },
    {"type": "sort", "keys": [{"field": "random", "descending": False}]},
    {"type": "expand", "to": "track", "scope": "library"},
    {
        "type": "filter",
        "mode": "remove",
        "match": "any",
        "conditions": [
            {"field": "title", "op": "contains", "value": "Titelmusik"},
            {"field": "title", "op": "contains", "value": "Inhaltsangabe"},
        ],
    },
    {"type": "sort", "keys": [{"field": "track_number", "descending": False}]},
]
SOURCE = {"type": "source", "media_type": "album"}


def test_parse_round_trip_die_drei() -> None:
    """The example document parses and converts back unchanged."""
    assert steps_to_dict(parse_steps(DIE_DREI)) == DIE_DREI


def test_first_step_must_be_source() -> None:
    """A document starting with another step is rejected at step 1."""
    with pytest.raises(StepsError) as exc:
        parse_steps([{"type": "filter", "mode": "keep", "match": "all", "conditions": []}])
    assert exc.value.errors[0].step == 1
    assert "source" in str(exc.value)


def test_only_one_source() -> None:
    """A second source step is rejected at its position."""
    with pytest.raises(StepsError) as exc:
        parse_steps([SOURCE, SOURCE])
    assert [err.step for err in exc.value.errors] == [2]


@pytest.mark.parametrize(
    "limit",
    [{}, {"count": 3, "minutes": 5}, {"count": 0}, {"minutes": -1}],
)
def test_limit_needs_exactly_one_of_count_minutes(limit: dict[str, int]) -> None:
    """A limit must carry exactly one positive value."""
    with pytest.raises(StepsError) as exc:
        parse_steps([SOURCE, {"type": "limit", **limit}])
    assert exc.value.errors[0].step == 2
    assert len(parse_steps([SOURCE, {"type": "limit", "count": 3}])) == 2
    assert len(parse_steps([SOURCE, {"type": "limit", "minutes": 120}])) == 2


def test_too_many_steps_conditions_sort_keys() -> None:
    """31 steps, 21 conditions and 6 sort keys are rejected."""
    with pytest.raises(StepsError):
        parse_steps([SOURCE] + [{"type": "flatten"}] * 30)
    assert len(parse_steps([SOURCE] + [{"type": "flatten"}] * 29)) == 30
    condition = {"field": "name", "op": "contains", "value": "a"}
    for count, ok in ((20, True), (21, False)):
        filt = {"type": "filter", "mode": "keep", "match": "all", "conditions": [condition] * count}
        if ok:
            parse_steps([SOURCE, filt])
        else:
            with pytest.raises(StepsError):
                parse_steps([SOURCE, filt])
    keys = [{"field": "name"}] * 6
    with pytest.raises(StepsError):
        parse_steps([SOURCE, {"type": "sort", "keys": keys}])
    parse_steps([SOURCE, {"type": "sort", "keys": keys[:5]}])


def test_expand_must_go_down() -> None:
    """Expanding upwards or without a source is rejected with a fix hint."""
    with pytest.raises(StepsError) as exc:
        parse_steps([SOURCE, {"type": "expand", "to": "artist"}])
    assert "albums can only become tracks" in str(exc.value)
    with pytest.raises(StepsError) as exc:
        parse_steps([{"type": "expand", "to": "track"}])
    assert "an expand step needs albums or artists before it" in str(exc.value)
    parse_steps(
        [
            {"type": "source", "media_type": "artist"},
            {"type": "expand", "to": "album"},
            {"type": "expand", "to": "track"},
        ]
    )


def test_bad_shapes_are_reported() -> None:
    """Unknown types, unknown fields and wrong values give step errors."""
    for bad in (
        "nope",
        [5],
        [{"type": "magic"}],
        [{"type": "source", "media_type": "podcast"}],
        [{"type": "source", "media_type": "album", "extra": 1}],
        [{"type": "source"}],
        [SOURCE, {"type": "filter", "match": "all", "conditions": []}],
        [SOURCE, {"type": "expand"}],
    ):
        with pytest.raises(StepsError) as exc:
            parse_steps(bad)
        assert exc.value.errors


def test_item_kinds_tracks_depth() -> None:
    """Kind and group depth follow source, expand and flatten steps."""
    steps = parse_steps(DIE_DREI)
    album, track = ItemKind.ALBUM, ItemKind.TRACK
    assert item_kinds(steps) == [(album, 0)] * 3 + [(track, 1)] * 3
    flat = parse_steps([*DIE_DREI, {"type": "flatten"}])
    assert item_kinds(flat)[-1] == (track, 0)


def test_schema_is_one_of_by_type() -> None:
    """The generated schema lists six alternatives, each pinned to its type."""
    schema = steps_json_schema()
    alternatives = schema["items"].get("oneOf") or schema["items"]["anyOf"]
    assert len(alternatives) == 6
    types = set()
    for alt in alternatives:
        prop = alt["properties"]["type"]
        values = [prop["const"]] if "const" in prop else prop["enum"]
        assert len(values) == 1
        assert "default" not in prop
        assert "type" in alt["required"]
        types.add(values[0])
    assert types == {"source", "filter", "sort", "limit", "expand", "flatten"}


def test_source_only_supports_library() -> None:
    """Another source origin is rejected with a clear message."""
    with pytest.raises(StepsError) as exc:
        parse_steps([{"type": "source", "media_type": "album", "from": "providers"}])
    assert "only the library is supported as a source for now" in str(exc.value)
    first = parse_steps([{"type": "source", "media_type": "album"}])[0]
    assert isinstance(first, SourceStep)
    assert first.origin == "library"


def test_missing_field_is_named() -> None:
    """A missing required field is reported with step and field name."""
    with pytest.raises(StepsError) as exc:
        parse_steps([SOURCE, {"type": "expand"}])
    assert (exc.value.errors[0].step, exc.value.errors[0].field) == (2, "to")
