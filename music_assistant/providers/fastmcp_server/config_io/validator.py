"""
Validate a single config value against its ConfigEntry definition.

Wraps ``ConfigEntry.parse_value`` (type coercion + the entry's optional
``validate`` callback) and additionally enforces ``range`` and
``options`` — which MA itself treats as UI hints, not parse-level
constraints. For a config-write surface driven by autonomous agents we
want these as fail-early guardrails so an out-of-range / not-in-options
value is rejected before it reaches storage. Any failure becomes a
:class:`fastmcp.exceptions.ToolError`. MA's ``save_*_config`` still
re-validates type/None authoritatively on the real write.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastmcp.exceptions import ToolError

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry


def coerce(entry: ConfigEntry, value: Any) -> Any:
    """
    Return the parsed/validated value for ``entry`` or raise ``ToolError``.

    Enforces, in order: type coercion + the entry's ``validate`` callback
    (via ``parse_value``), then ``range`` (numeric bounds) and ``options``
    (allowed-value set). ``range``/``options`` are enforced here because MA
    treats them as UI hints, not parse-level constraints.

    :param entry: The ConfigEntry definition the value must satisfy.
    :param value: The raw incoming value.
    """
    try:
        parsed = entry.parse_value(value, allow_none=True, raise_on_error=True)
    except Exception as exc:
        raise ToolError(f"value for {entry.key!r} failed validation: {exc}") from exc

    if entry.range is not None and isinstance(parsed, int | float) and not isinstance(parsed, bool):
        low, high = entry.range
        if not (low <= parsed <= high):
            raise ToolError(
                f"value for {entry.key!r} failed validation: {parsed} out of range [{low}, {high}]"
            )

    if entry.options and parsed is not None:
        allowed = {opt.value for opt in entry.options}
        candidates = parsed if isinstance(parsed, list) else [parsed]
        bad = [c for c in candidates if c not in allowed]
        if bad:
            raise ToolError(
                f"value for {entry.key!r} failed validation: {bad!r} not in allowed options"
            )

    return parsed
