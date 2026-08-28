"""JSON-safe serialization for dynamic Music Assistant command results."""

from __future__ import annotations

import copy
import dataclasses
import heapq
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID

from fastmcp.exceptions import ToolError

type JSONValue = None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]

COMMAND_ENVELOPE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "command": {"type": "string"},
        "data": {},
        "truncated": {"type": "boolean"},
        "returned_count": {"type": "integer"},
        "bytes": {"type": "integer"},
        "applied": {"type": "object"},
        "total_count": {"type": "integer"},
    },
    "required": ["command", "data", "truncated", "returned_count", "bytes", "applied"],
}


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
    for index, (key, child) in enumerate(items):
        if index >= item_cap:
            truncated = True
            break
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


@dataclass(slots=True)
class _ListReductionCandidate:
    """Mutable heap state for one list in a response-reduction trial."""

    items: list[Any]
    depth: int
    order: int
    active: bool = True
    revision: int = 0


def fit_json_envelope(envelope: dict[str, Any], byte_cap: int) -> None:
    """
    Reduce one command envelope in place until it fits the byte budget.

    :param envelope: Mutable command response envelope.
    :param byte_cap: Maximum encoded UTF-8 size in bytes.
    """
    envelope["bytes"] = byte_cap
    if _encoded_size(envelope) <= byte_cap:
        _set_measured_bytes(envelope)
        return

    original_data = envelope["data"]
    max_removals = _count_list_items(original_data)
    if max_removals:
        envelope["truncated"] = True
        smallest_data = _simulate_list_removals(original_data, max_removals)
        envelope["data"] = smallest_data
        _set_returned_count(envelope)
        if _encoded_size(envelope) <= byte_cap:
            low = 1
            high = max_removals
            best_data = smallest_data
            while low < high:
                midpoint = (low + high) // 2
                candidate_data = _simulate_list_removals(original_data, midpoint)
                envelope["data"] = candidate_data
                _set_returned_count(envelope)
                if _encoded_size(envelope) <= byte_cap:
                    high = midpoint
                    best_data = candidate_data
                else:
                    low = midpoint + 1
            envelope["data"] = best_data
            _set_returned_count(envelope)
            _set_measured_bytes(envelope)
            return

    envelope["data"] = _minimal_json_shape(original_data)
    envelope["truncated"] = True
    _set_returned_count(envelope)
    envelope.pop("total_count", None)
    if _encoded_size(envelope) <= byte_cap:
        _set_measured_bytes(envelope)
        return
    envelope["applied"]["fields"] = []
    if _encoded_size(envelope) <= byte_cap:
        _set_measured_bytes(envelope)
        return
    mode = str(envelope["applied"]["mode"])
    raise ToolError(f"Response exceeds the {mode} byte budget")


def _simulate_list_removals(value: Any, removals: int) -> Any:
    """Return a copy after a bounded number of original-policy list removals."""
    reduced = copy.deepcopy(value)
    candidates: list[_ListReductionCandidate] = []
    candidates_by_id: dict[int, _ListReductionCandidate] = {}
    heap: list[tuple[int, int, int, int, int]] = []

    def collect(item: Any, depth: int) -> None:
        if isinstance(item, list):
            candidate_index = len(candidates)
            candidate = _ListReductionCandidate(item, depth, candidate_index)
            candidates.append(candidate)
            candidates_by_id[id(item)] = candidate
            if item:
                heap.append(
                    (-len(item), depth, candidate.order, candidate.revision, candidate_index)
                )
            for child in item:
                collect(child, depth + 1)
        elif isinstance(item, dict):
            for child in item.values():
                collect(child, depth + 1)

    def invalidate(item: Any) -> None:
        if isinstance(item, list):
            candidate = candidates_by_id.get(id(item))
            if candidate is not None:
                candidate.active = False
                candidate.revision += 1
            for child in item:
                invalidate(child)
        elif isinstance(item, dict):
            for child in item.values():
                invalidate(child)

    collect(reduced, 0)
    heapq.heapify(heap)
    removed = 0
    while removed < removals and heap:
        negative_length, _depth, _order, revision, candidate_index = heapq.heappop(heap)
        candidate = candidates[candidate_index]
        if (
            not candidate.active
            or candidate.revision != revision
            or len(candidate.items) != -negative_length
        ):
            continue
        removed_item = candidate.items.pop()
        removed += 1
        invalidate(removed_item)
        candidate.revision += 1
        if candidate.items:
            heapq.heappush(
                heap,
                (
                    -len(candidate.items),
                    candidate.depth,
                    candidate.order,
                    candidate.revision,
                    candidate_index,
                ),
            )
    return reduced


def _count_list_items(value: Any) -> int:
    """Return a safe upper bound on logical removals for a JSON tree."""
    if isinstance(value, list):
        return len(value) + sum(_count_list_items(item) for item in value)
    if isinstance(value, dict):
        return sum(_count_list_items(item) for item in value.values())
    return 0


def _minimal_json_shape(value: Any) -> Any:
    """Return the smallest JSON value retaining the result's top-level type."""
    if isinstance(value, dict):
        return {}
    if isinstance(value, list):
        return []
    if isinstance(value, str):
        return ""
    if isinstance(value, bool):
        return False
    if isinstance(value, int | float):
        return 0
    return None


def _set_returned_count(envelope: dict[str, Any]) -> None:
    """Refresh the envelope's top-level returned item count."""
    data = envelope["data"]
    envelope["returned_count"] = len(data) if isinstance(data, list) else (0 if data is None else 1)


def _encoded_size(value: Any) -> int:
    """Measure the compact UTF-8 JSON representation."""
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode()
    )


def _set_measured_bytes(envelope: dict[str, Any]) -> None:
    """Stabilize the self-referential encoded byte count."""
    for _attempt in range(3):
        measured = _encoded_size(envelope)
        if envelope["bytes"] == measured:
            return
        envelope["bytes"] = measured
