"""Plain English sentences for the steps of a Rotation playlist."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .fields import FIELDS, RANDOM, FieldType
from .models import (
    ExpandStep,
    FilterStep,
    FlattenStep,
    ItemKind,
    LimitStep,
    SortStep,
    SourceStep,
    item_kinds,
)

if TYPE_CHECKING:
    from .models import Condition, SortKey, Step

PLACEHOLDER = "..."
_SINGULAR = {ItemKind.ARTIST: "artist", ItemKind.ALBUM: "album", ItemKind.TRACK: "track"}
_PLURAL = {ItemKind.ARTIST: "artists", ItemKind.ALBUM: "albums", ItemKind.TRACK: "tracks"}
_OPERATORS = {
    "is": "is",
    "is_not": "is not",
    "contains": "contains",
    "not_contains": "does not contain",
    "starts_with": "starts with",
    "ends_with": "ends with",
    "less_than": "is less than",
    "more_than": "is more than",
    "between": "is between",
    "one_of": "is one of",
}
_DATE_OPERATORS = {"less_than": "is before", "more_than": "is after"}
_DESCENDING = {
    FieldType.DATE: "newest first",
    FieldType.NUMBER: "highest first",
    FieldType.BOOL: "yes first",
    FieldType.TEXT: "Z to A",
    FieldType.REF: "Z to A",
}


def explain_steps(steps: list[Step], names: dict[str, str] | None = None) -> list[str]:
    """
    Return one plain English sentence per step.

    :param steps: The steps of the Rotation.
    :param names: Display names for uri values, for example of artists, by uri.
    """
    names = names or {}
    kinds = item_kinds(steps)
    sentences: list[str] = []
    # the kind of the items at each group depth, outermost first
    levels: list[ItemKind] = []
    for index, step in enumerate(steps):
        kind, depth = kinds[index - 1] if index else (kinds[0][0], 0)
        sentence = _sentence(step, kind, names)
        if depth and isinstance(step, FilterStep | SortStep | LimitStep):
            # these work inside every group of the expand before them
            group = _SINGULAR[levels[depth - 1]]
            sentence = f"Inside each {group}: {sentence[:1].lower()}{sentence[1:]}"
        sentences.append(sentence)
        if isinstance(step, SourceStep):
            levels = [step.media_type]
        elif isinstance(step, ExpandStep):
            levels = [*levels[: depth + 1], step.to]
        elif isinstance(step, FlattenStep):
            levels = [kinds[index][0]]
    return sentences


def _sentence(step: Step, kind: ItemKind, names: dict[str, str]) -> str:
    """Return the sentence of one step that works on items of a kind."""
    if isinstance(step, SourceStep):
        return f"Take {_PLURAL[step.media_type]} from my library"
    if isinstance(step, FilterStep):
        if not step.conditions:
            return "Keep everything" if step.mode == "keep" else "Leave out nothing"
        joiner = " and " if step.match == "all" else " or "
        prefix = "Keep where " if step.mode == "keep" else "Leave out where "
        return prefix + joiner.join(
            _condition(condition, kind, names) for condition in step.conditions
        )
    if isinstance(step, SortStep):
        if [key.field for key in step.keys] == [RANDOM]:
            return "Order randomly"
        return "Order by " + ", then ".join(_sort_key(key, kind) for key in step.keys)
    if isinstance(step, LimitStep):
        return f"Only the first {_limit(step, kind)}"
    if isinstance(step, ExpandStep):
        sentence = f"Play each {_SINGULAR[kind]}'s {_PLURAL[step.to]}"
        if step.scope == "providers" and kind == ItemKind.ARTIST and step.to == ItemKind.ALBUM:
            sentence += ", also those on the providers"
        return sentence
    return "Mix all together"


def _condition(condition: Condition, kind: ItemKind, names: dict[str, str]) -> str:
    """Return a condition as words, for example 'title contains Intro'."""
    definition = FIELDS[kind].get(condition.field)
    label = _label(condition.field, kind)
    field_type = definition.type if definition else FieldType.TEXT
    if condition.op == "in_last_days":
        return f"{label} is in the last {_value(condition.value, names)} days"
    operator = _OPERATORS.get(condition.op, condition.op.replace("_", " "))
    if field_type == FieldType.DATE:
        operator = _DATE_OPERATORS.get(condition.op, operator)
    value = condition.value
    fixed = definition is not None and bool(definition.values)
    if fixed:
        value = _fixed_value(value)
    # a text or a looked up name reads in quotes, so the sentence shows where it ends; a fixed
    # word like an album type does not
    quoted = field_type in (FieldType.TEXT, FieldType.REF) and not fixed
    if field_type == FieldType.REF:
        quoted = quoted and all(
            str(part) in names or "://" in str(part)
            for part in (value if isinstance(value, list) else [value])
            if part is not None
        )
    if condition.op == "between" and isinstance(value, list) and len(value) == 2:
        text = f"{_value(value[0], names, quoted)} and {_value(value[1], names, quoted)}"
    elif isinstance(value, list):
        parts = [_value(part, names, quoted) for part in value]
        text = ", ".join(parts[:-1]) + " or " + parts[-1] if len(parts) > 1 else "".join(parts)
    else:
        text = _value(value, names, quoted)
    return f"{label} {operator} {text}"


def _sort_key(key: SortKey, kind: ItemKind) -> str:
    """Return a sort key as words, with its direction when descending."""
    if key.field == RANDOM:
        return "random order"
    label = _label(key.field, kind)
    if not key.descending:
        return label
    definition = FIELDS[kind].get(key.field)
    direction = _DESCENDING[definition.type] if definition else "descending"
    return f"{label}, {direction}"


def _limit(step: LimitStep, kind: ItemKind) -> str:
    """Return the amount of a limit, for example '20 albums' or '2 hours'."""
    if step.count is not None:
        if step.count == 1:
            return _SINGULAR[kind]
        return f"{step.count} {_PLURAL[kind]}"
    minutes = step.minutes or 0
    if minutes and minutes % 60 == 0:
        hours = minutes // 60
        return "hour" if hours == 1 else f"{hours} hours"
    return "minute" if minutes == 1 else f"{minutes} minutes"


def _label(field: str, kind: ItemKind) -> str:
    """Return the short lower case label of a field, without the remark in brackets."""
    definition = FIELDS[kind].get(field)
    if definition is None:
        return field.replace("_", " ")
    return definition.label.split(" (")[0].lower()


def _fixed_value(value: Any) -> Any:
    """Return a fixed value, or a list of them, as words: hi_res reads as hi-res."""
    if isinstance(value, list):
        return [_fixed_value(part) for part in value]
    return value.replace("_", "-") if isinstance(value, str) else value


def _value(value: Any, names: dict[str, str], quoted: bool = False) -> str:
    """Return a condition value as text, a text or a name in double quotes."""
    if value is None:
        return PLACEHOLDER
    if isinstance(value, bool):
        return "yes" if value else "no"
    text = str(value)
    text = names.get(text, text)
    return f'"{text}"' if quoted else text
