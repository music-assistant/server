"""JSON-safe serialization for dynamic Music Assistant command results."""

from __future__ import annotations

import dataclasses
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID

type JSONValue = None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]


@dataclass(frozen=True, slots=True)
class BoundedJSON:
    """One normalized JSON value plus its response-bound metadata."""

    value: JSONValue
    truncated: bool
    total_count: int | None = None


def json_value(value: Any) -> JSONValue:
    """Convert one MA result without reconstructing custom collection types."""
    return _json_value(value, active_ids=set())


def bounded_json_value(
    value: Any,
    *,
    item_cap: int,
    string_cap: int,
    max_depth: int,
) -> BoundedJSON:
    """Normalize one result while bounding lists, strings, and recursive depth."""
    normalized, truncated, total_count = _bounded_json_value(
        value,
        active_ids=set(),
        item_cap=item_cap,
        string_cap=string_cap,
        depth=max_depth,
    )
    return BoundedJSON(normalized, truncated, total_count)


def _json_value(value: Any, active_ids: set[int]) -> JSONValue:
    """Convert one value while tracking active recursive references."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Enum):
        return _json_value(value.value, active_ids)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, UUID | Path):
        return str(value)

    object_id = id(value)
    if object_id in active_ids:
        return f"<{type(value).__module__}.{type(value).__qualname__}:cycle>"
    active_ids.add(object_id)
    try:
        if callable(to_dict := getattr(value, "to_dict", None)):
            return _json_value(to_dict(), active_ids)
        if callable(model_dump := getattr(value, "model_dump", None)):
            return _json_value(model_dump(mode="json"), active_ids)
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return {
                field.name: _json_value(getattr(value, field.name), active_ids)
                for field in dataclasses.fields(value)
            }
        if isinstance(value, Mapping):
            return {str(key): _json_value(child, active_ids) for key, child in value.items()}
        if isinstance(value, set | frozenset):
            children = [_json_value(child, active_ids) for child in value]
            return sorted(children, key=lambda child: json.dumps(child, sort_keys=True))
        if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
            return [_json_value(child, active_ids) for child in value]
        return f"<{type(value).__module__}.{type(value).__qualname__}>"
    finally:
        active_ids.remove(object_id)


def _bounded_json_value(
    value: Any,
    *,
    active_ids: set[int],
    item_cap: int,
    string_cap: int,
    depth: int,
) -> tuple[JSONValue, bool, int | None]:
    """Normalize one value without traversing beyond configured response bounds."""
    if value is None or isinstance(value, bool | int):
        return value, False, None
    if isinstance(value, float):
        return (value, False, None) if math.isfinite(value) else (None, True, None)
    if isinstance(value, str):
        normalized, truncated = _bounded_string(value, string_cap)
        return normalized, truncated, None
    if isinstance(value, Enum):
        return _bounded_json_value(
            value.value,
            active_ids=active_ids,
            item_cap=item_cap,
            string_cap=string_cap,
            depth=depth,
        )
    if isinstance(value, datetime | date):
        normalized, truncated = _bounded_string(value.isoformat(), string_cap)
        return normalized, truncated, None
    if isinstance(value, UUID | Path):
        normalized, truncated = _bounded_string(str(value), string_cap)
        return normalized, truncated, None
    if depth <= 0:
        return "[truncated]", True, None

    object_id = id(value)
    if object_id in active_ids:
        return f"<{type(value).__module__}.{type(value).__qualname__}:cycle>", True, None
    active_ids.add(object_id)
    try:
        if callable(to_dict := getattr(value, "to_dict", None)):
            return _bounded_json_value(
                to_dict(),
                active_ids=active_ids,
                item_cap=item_cap,
                string_cap=string_cap,
                depth=depth,
            )
        if callable(model_dump := getattr(value, "model_dump", None)):
            return _bounded_json_value(
                model_dump(mode="json"),
                active_ids=active_ids,
                item_cap=item_cap,
                string_cap=string_cap,
                depth=depth,
            )
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return _bounded_mapping(
                ((field.name, getattr(value, field.name)) for field in dataclasses.fields(value)),
                active_ids=active_ids,
                item_cap=item_cap,
                string_cap=string_cap,
                depth=depth,
            )
        if isinstance(value, Mapping):
            return _bounded_mapping(
                value.items(),
                active_ids=active_ids,
                item_cap=item_cap,
                string_cap=string_cap,
                depth=depth,
            )
        if isinstance(value, set | frozenset):
            set_children = [
                _bounded_json_value(
                    child,
                    active_ids=active_ids,
                    item_cap=item_cap,
                    string_cap=string_cap,
                    depth=depth - 1,
                )
                for child in value
            ]
            set_children.sort(key=lambda child: json.dumps(child[0], sort_keys=True))
            kept = set_children[:item_cap]
            return (
                [child for child, _changed, _count in kept],
                len(set_children) > item_cap or any(changed for _child, changed, _count in kept),
                len(set_children),
            )
        if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
            sequence_children: list[JSONValue] = []
            truncated = len(value) > item_cap
            for index, child in enumerate(value):
                if index >= item_cap:
                    break
                normalized_child, changed, _count = _bounded_json_value(
                    child,
                    active_ids=active_ids,
                    item_cap=item_cap,
                    string_cap=string_cap,
                    depth=depth - 1,
                )
                sequence_children.append(normalized_child)
                truncated |= changed
            return sequence_children, truncated, len(value)
        return f"<{type(value).__module__}.{type(value).__qualname__}>", False, None
    finally:
        active_ids.remove(object_id)


def _bounded_mapping(
    items: Any,
    *,
    active_ids: set[int],
    item_cap: int,
    string_cap: int,
    depth: int,
) -> tuple[JSONValue, bool, None]:
    """Normalize mapping-like items while retaining their insertion order."""
    result: dict[str, JSONValue] = {}
    truncated = False
    for key, child in items:
        normalized_key, key_changed = _bounded_string(str(key), None)
        normalized, changed, _count = _bounded_json_value(
            child,
            active_ids=active_ids,
            item_cap=item_cap,
            string_cap=string_cap,
            depth=depth - 1,
        )
        result[normalized_key] = normalized
        truncated |= key_changed or changed
    return result, truncated, None


def _bounded_string(value: str, limit: int | None) -> tuple[str, bool]:
    """Replace invalid Unicode scalars and optionally enforce one string limit."""
    over_limit = limit is not None and len(value) > limit
    source = value[:limit] if over_limit and limit is not None else value
    normalized = "".join(
        "\ufffd" if 0xD800 <= ord(character) <= 0xDFFF else character for character in source
    )
    if over_limit:
        return normalized + "…", True
    return normalized, normalized != value
