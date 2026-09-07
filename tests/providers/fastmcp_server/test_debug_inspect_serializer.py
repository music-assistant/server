"""Unit tests for the recursive JSON-safe inspect serializer."""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import enum
import json
from typing import Any

from music_assistant.providers.fastmcp_server.debug.inspect_serializer import _State, dump


def test_charge_estimates_bytes_without_json_encoding() -> None:
    """
    ``_State.charge`` sizes values directly, not via ``json.dumps``.

    For an unescaped string the estimate matches the JSON length (content
    bytes + 2 quotes). For a string containing a quote, the direct estimate is
    the UTF-8 length + 2 — strictly less than the JSON-escaped length — which
    pins that ``json.dumps`` is no longer used.
    """
    state = _State(max_depth=10, max_str=1000, max_total_bytes=1_000_000)
    state.charge("yyyy")
    assert state.bytes_used == len(b"yyyy") + 2

    quote_state = _State(max_depth=10, max_str=1000, max_total_bytes=1_000_000)
    quote_state.charge('a"b')
    assert quote_state.bytes_used == len(b'a"b') + 2  # 5, not json's 6

    none_state = _State(max_depth=10, max_str=1000, max_total_bytes=1_000_000)
    none_state.charge(None)
    assert none_state.bytes_used == 4  # "null"


def test_charge_is_noop_without_budget() -> None:
    """With no byte budget configured, ``charge`` does nothing."""
    state = _State(max_depth=10, max_str=1000, max_total_bytes=None)
    state.charge("anything")
    assert state.bytes_used == 0


@dataclasses.dataclass
class _Inner:
    x: int
    label: str


@dataclasses.dataclass
class _Outer:
    name: str
    inner: _Inner
    items: list[int]


def test_dump_simple_dataclass() -> None:  # noqa: D103
    obj = _Outer(name="root", inner=_Inner(x=1, label="hi"), items=[1, 2, 3])
    out = dump(obj)
    assert out == {"name": "root", "inner": {"x": 1, "label": "hi"}, "items": [1, 2, 3]}


def test_dump_handles_self_reference_without_recursion_error() -> None:  # noqa: D103
    d: dict[str, Any] = {}
    d["self"] = d
    out = dump(d)
    assert out["self"] == "<cycle>"


def test_dump_max_depth_truncates() -> None:  # noqa: D103
    deep: dict[str, Any] = {}
    current = deep
    for _ in range(10):
        current["next"] = {}
        current = current["next"]
    current["leaf"] = "value"
    out = dump(deep, max_depth=3)

    def _depth(o: Any, n: int = 0) -> tuple[int, Any]:
        if isinstance(o, dict) and "next" in o:
            return _depth(o["next"], n + 1)
        return n, o

    depth, leaf = _depth(out)
    assert depth == 3
    assert leaf == "<max depth>"


def test_dump_max_str_truncates_with_suffix() -> None:  # noqa: D103
    big = "x" * 5000
    out = dump(big, max_str=100)
    assert isinstance(out, str)
    assert out.startswith("x" * 100)
    assert "…(" in out
    assert "more chars)" in out


class _Color(enum.Enum):
    RED = "red"
    BLUE = "blue"


def test_dump_enum_to_value() -> None:  # noqa: D103
    assert dump(_Color.RED) == "red"


def test_dump_datetime_to_iso8601() -> None:  # noqa: D103
    t = dt.datetime(2026, 5, 28, 12, 34, 56, tzinfo=dt.UTC)
    assert dump(t) == "2026-05-28T12:34:56+00:00"


def test_dump_bytes_to_length_placeholder() -> None:  # noqa: D103
    assert dump(b"\x00" * 4096) == "<bytes len=4096>"


def test_dump_unserialisable_objects_render_placeholder() -> None:  # noqa: D103
    lock = asyncio.Lock()
    assert dump(lock) == "<unserializable Lock>"


def test_dump_property_raising_renders_placeholder() -> None:  # noqa: D103
    class _Bad:
        @property
        def value(self) -> str:
            raise RuntimeError("not ready")

    out = dump(_Bad())
    assert out["value"] == "<raise: RuntimeError>"


def test_dump_total_payload_cap_marks_truncated() -> None:  # noqa: D103
    # 300 KB of content (300 strings of 1 KB each); cap at 256 KB.
    big = {f"k{i}": "y" * 1024 for i in range(300)}
    out, truncated = dump(big, max_total_bytes=256 * 1024, return_truncated=True)
    assert truncated is True
    assert len(json.dumps(out)) <= 256 * 1024 + 1024  # +slack for the marker


def test_dump_dataclass_with_raising_property_renders_placeholder() -> None:
    """
    Real-world shape: MA Player is a dataclass with diagnostic @property.

    The plain-class variant of this test (test_dump_property_raising_renders_placeholder)
    used a non-dataclass and silently passed even when the dataclass branch did not
    discover properties — this regression guard pins the actual production case.
    """

    @dataclasses.dataclass
    class _Broken:
        player_id: str

        @property
        def diagnostic(self) -> str:
            raise RuntimeError("not ready")

    out = dump(_Broken(player_id="kitchen"))
    assert out["player_id"] == "kitchen"
    assert out["diagnostic"] == "<raise: RuntimeError>"


def test_dump_skips_mass_back_reference_on_dataclasses() -> None:
    """
    ``mass`` field on Player/Queue/Provider is a back-ref to MA's runtime root.

    Live verification surfaced the regression: walking ``player.mass`` drags the
    entire MA graph (every other provider, queue, player, cache) into the dump
    and exhausts the byte budget before the player's own ``player_id``/``name``/
    ``current_media`` get serialised — leaving ``raw.keys() == ["mass"]`` with
    every user-facing field reported as ``null``. The serializer must elide
    ``mass`` (and the equivalent ``hass`` for Home Assistant) with a transparent
    placeholder so the caller sees the field exists but the heavy subtree is
    not chased.
    """

    @dataclasses.dataclass
    class _Huge:
        # Stand-in for the MusicAssistant runtime root — a large payload that
        # would dominate the dump if walked.
        big: list[int] = dataclasses.field(default_factory=lambda: list(range(10_000)))

    @dataclasses.dataclass
    class _Player:
        player_id: str
        name: str
        mass: _Huge
        hass: _Huge
        logger: _Huge

    p = _Player(
        player_id="kitchen",
        name="Kitchen",
        mass=_Huge(),
        hass=_Huge(),
        logger=_Huge(),
    )
    out = dump(p)

    # The user-facing fields ARE present even though they come after the
    # back-ref in dataclass field order.
    assert out["player_id"] == "kitchen"
    assert out["name"] == "Kitchen"
    # All three back-refs (MA runtime, HA client, scoped Logger) are elided
    # with a transparent placeholder — the caller sees the field exists but
    # the heavy subtree was not chased.
    assert out["mass"] == "<back-ref skipped: mass>"
    assert out["hass"] == "<back-ref skipped: hass>"
    assert out["logger"] == "<back-ref skipped: logger>"
    # And — critically — the 10_000-element ``big`` list inside any of them
    # did not get walked, so it does not appear anywhere in the payload.
    assert "big" not in json.dumps(out)


def test_dump_dataclass_includes_property_values() -> None:
    """Dataclass properties whose getters succeed should appear in the output too."""

    @dataclasses.dataclass
    class _Good:
        x: int

        @property
        def doubled(self) -> int:
            return self.x * 2

    out = dump(_Good(x=3))
    assert out == {"x": 3, "doubled": 6}
