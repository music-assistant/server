"""
Recursive JSON-safe converter for raw MA dataclass inspection.

Caller contract — :func:`dump` never raises for user-visible cases:

* unknown attribute access errors are caught per-field and rendered as
  ``"<raise: ExcClass>"`` placeholders so a half-constructed object can
  still be inspected;
* cycles render as ``"<cycle>"`` (not :class:`RecursionError`);
* tasks / locks / generators / file handles render as
  ``"<unserializable ClassName>"`` rather than failing;
* depth and string-length are capped (defaults: ``max_depth=6``,
  ``max_str=2048``);
* an optional ``max_total_bytes`` cap halts serialisation after the
  budget is exceeded; ``return_truncated=True`` returns a tuple
  ``(payload, truncated)`` instead of just the payload.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import enum
import json
from typing import Any

_UNSERIALISABLE = (
    asyncio.Lock,
    asyncio.Future,
    asyncio.Task,
    asyncio.Event,
    asyncio.Queue,
    asyncio.Semaphore,
)

# Names that are known runtime back-references on MA dataclasses like
# ``Player``, ``Queue``, ``Provider``:
#
# * ``mass`` / ``hass`` point at the MusicAssistant / Home Assistant root
#   — walking them drags every other provider, queue, player, cache and
#   metadata-cache into the dump.
# * ``logger`` is a ``logging.Logger`` whose ``.parent.manager.loggerDict``
#   chain transitively reaches every other logger registered in the
#   runtime; for our purposes its ``name`` / ``level`` would be the only
#   useful bits, and both are already implied by the entity's ``provider``
#   / ``domain`` field.
#
# Each one alone is enough to exhaust the 256 KB byte budget before the
# entity's own fields are serialised. Skip them explicitly and render a
# transparent ``<back-ref skipped: name>`` placeholder so callers see the
# field exists but the heavy subtree was not chased.
_BACK_REF_NAMES = frozenset({"mass", "hass", "logger"})


def dump(
    obj: Any,
    *,
    max_depth: int = 6,
    max_str: int = 2048,
    max_total_bytes: int | None = None,
    return_truncated: bool = False,
) -> Any:
    """
    Convert ``obj`` to a JSON-safe structure.

    :param obj: Any Python object.
    :param max_depth: Maximum recursion depth before fields collapse to
        ``"<max depth>"``.
    :param max_str: Per-string truncation length.
    :param max_total_bytes: When set, serialisation halts once the
        approximate JSON-encoded size exceeds this many bytes.
    :param return_truncated: When True, return ``(payload, truncated_flag)``
        instead of just ``payload``.
    """
    state = _State(max_depth=max_depth, max_str=max_str, max_total_bytes=max_total_bytes)
    payload = _convert(obj, depth=0, seen=set(), state=state)
    if return_truncated:
        return payload, state.truncated
    return payload


class _State:
    __slots__ = ("bytes_used", "max_depth", "max_str", "max_total_bytes", "truncated")

    def __init__(self, *, max_depth: int, max_str: int, max_total_bytes: int | None) -> None:
        self.max_depth = max_depth
        self.max_str = max_str
        self.max_total_bytes = max_total_bytes
        self.bytes_used = 0
        self.truncated = False

    def budget_exceeded(self) -> bool:
        return self.max_total_bytes is not None and self.bytes_used >= self.max_total_bytes

    def charge(self, value: Any) -> None:
        # Estimate the serialized byte cost of a leaf value directly rather
        # than round-tripping it through ``json.dumps`` (called once per leaf,
        # this was the hot path). The budget is a safety bound, so a close
        # estimate is enough; escaping may add a few bytes we don't count.
        if self.max_total_bytes is None:
            return
        if isinstance(value, str):
            self.bytes_used += len(value.encode("utf-8")) + 2  # + surrounding quotes
        elif isinstance(value, bool):
            self.bytes_used += 4 if value else 5  # "true" / "false"
        elif value is None:
            self.bytes_used += 4  # "null"
        elif isinstance(value, (int, float)):
            self.bytes_used += len(repr(value))
        else:
            self.bytes_used += 32


def _convert(obj: Any, *, depth: int, seen: set[int], state: _State) -> Any:
    if state.budget_exceeded():
        state.truncated = True
        return "<truncated>"
    if depth >= state.max_depth:
        return "<max depth>"

    if obj is None or isinstance(obj, bool | int | float):
        state.charge(obj)
        return obj

    if isinstance(obj, str):
        if len(obj) > state.max_str:
            remainder = len(obj) - state.max_str
            value = f"{obj[: state.max_str]}…({remainder} more chars)"
        else:
            value = obj
        state.charge(value)
        return value

    if isinstance(obj, bytes | bytearray | memoryview):
        value = f"<bytes len={len(obj)}>"
        state.charge(value)
        return value

    if isinstance(obj, enum.Enum):
        return _convert(obj.value, depth=depth, seen=seen, state=state)

    if isinstance(obj, dt.datetime | dt.date | dt.time):
        value = obj.isoformat()
        state.charge(value)
        return value

    if isinstance(obj, _UNSERIALISABLE):
        value = f"<unserializable {type(obj).__name__}>"
        state.charge(value)
        return value

    if id(obj) in seen:
        return "<cycle>"
    seen = seen | {id(obj)}

    if isinstance(obj, dict):
        return _convert_dict(obj, depth, seen, state)

    if isinstance(obj, list | tuple | set | frozenset):
        return _convert_seq(obj, depth, seen, state)

    if dataclasses.is_dataclass(obj):
        names = [f.name for f in dataclasses.fields(obj)]
        # Include public properties from the dataclass (mirrors _extract_public_names pattern).
        for attr_name in dir(type(obj)):
            if attr_name.startswith("_") or attr_name in names:
                continue
            try:
                attr_val = getattr(type(obj), attr_name, None)
                if isinstance(attr_val, property):
                    names.append(attr_name)
            except Exception:  # noqa: S110 — defensive; dir() may list unavailable attrs
                pass
        return _convert_attrs(obj, names, depth, seen, state)

    if hasattr(obj, "__dict__") or hasattr(obj, "__slots__"):
        names = _extract_public_names(obj)
        if names:
            return _convert_attrs(obj, names, depth, seen, state)

    # Last-ditch fallback: stringify.
    value = f"<unserializable {type(obj).__name__}>"
    state.charge(value)
    return value


def _convert_dict(obj: dict[Any, Any], depth: int, seen: set[int], state: _State) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in obj.items():
        if state.budget_exceeded():
            state.truncated = True
            break
        key = str(k)
        conv_v = _convert(v, depth=depth + 1, seen=seen, state=state)
        out[key] = conv_v
        # Charge per key+value instead of re-encoding the partial dict (avoid O(N²)).
        if state.max_total_bytes is not None:
            try:
                key_bytes = len(json.dumps(key).encode("utf-8"))
                val_bytes = len(json.dumps(conv_v, default=str).encode("utf-8"))
                state.bytes_used += key_bytes + val_bytes + 4  # ": ," overhead
            except TypeError, ValueError:
                state.bytes_used += 64
        if state.budget_exceeded():
            state.truncated = True
            break
    return out


def _convert_seq(
    obj: list[Any] | tuple[Any, ...] | set[Any] | frozenset[Any],
    depth: int,
    seen: set[int],
    state: _State,
) -> list[Any]:
    seq: list[Any] = []
    for item in obj:
        if state.budget_exceeded():
            state.truncated = True
            break
        seq.append(_convert(item, depth=depth + 1, seen=seen, state=state))
    return seq


def _extract_public_names(obj: Any) -> list[str]:
    """Extract public attribute names from __dict__, __slots__, and properties."""
    names = list(getattr(obj, "__dict__", {}).keys())
    for slot in getattr(obj, "__slots__", ()) or ():
        if slot not in names:
            names.append(slot)
    # Include public properties from the class.
    for attr_name in dir(obj):
        if not attr_name.startswith("_") and attr_name not in names:
            try:
                attr_val = getattr(type(obj), attr_name, None)
                if isinstance(attr_val, property):
                    names.append(attr_name)
            except Exception:  # noqa: S110 — defensive; dir() may list unavailable attrs
                pass
    return [n for n in names if not n.startswith("_")]


def _convert_attrs(
    obj: Any, names: list[str], depth: int, seen: set[int], state: _State
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in names:
        if state.budget_exceeded():
            state.truncated = True
            break
        if name in _BACK_REF_NAMES:
            placeholder = f"<back-ref skipped: {name}>"
            out[name] = placeholder
            state.charge(placeholder)
            continue
        try:
            value = getattr(obj, name)
        except Exception as exc:
            out[name] = f"<raise: {type(exc).__name__}>"
            continue
        out[name] = _convert(value, depth=depth + 1, seen=seen, state=state)
    return out
