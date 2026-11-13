"""Semantic validation of a Rotation steps document."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .fields import FIELDS, NAME_OPERATORS, RANDOM, FieldDef, FieldType, operators_for, parse_date
from .models import (
    ExpandStep,
    FilterStep,
    ItemKind,
    SortStep,
    Step,
    StepError,
    item_kinds,
    step_text,
)

if TYPE_CHECKING:
    from music_assistant_models.translations import TranslatableText

_PLURAL = {ItemKind.ARTIST: "artists", ItemKind.ALBUM: "albums", ItemKind.TRACK: "tracks"}
_EXPAND_HINT = {
    (ItemKind.ALBUM, ItemKind.TRACK): "add a 'Play each album's tracks' step",
    (ItemKind.ARTIST, ItemKind.ALBUM): "add a 'Play each artist's albums' step",
    (ItemKind.ARTIST, ItemKind.TRACK): "add a 'Play each artist's tracks' step",
}


def validate_steps(steps: list[Step]) -> list[StepError]:
    """
    Check that every condition and sort key fits the items its step works on.

    :param steps: The steps of a document that already passed the shape check.
    """
    errors: list[StepError] = []
    kinds = item_kinds(steps)
    for index, step in enumerate(steps):
        if index == 0:
            continue
        number = index + 1
        kind = kinds[index - 1][0]
        if isinstance(step, FilterStep):
            for position, condition in enumerate(step.conditions):
                where = f"conditions[{position}]"
                definition = _find_field(
                    number, kind, condition.field, f"{where}.field", errors, filtering=True
                )
                if definition is None:
                    continue
                if condition.field == RANDOM:
                    errors.append(
                        StepError(
                            number,
                            f"{where}.field",
                            "'random' can only be used to order items; pick another field",
                            "random_not_filter",
                        )
                    )
                    continue
                _check_condition(number, where, definition, condition.op, condition.value, errors)
        elif isinstance(step, ExpandStep):
            if kind == ItemKind.ARTIST and step.to == ItemKind.TRACK and step.scope == "providers":
                errors.append(
                    StepError(
                        number,
                        "scope",
                        "the tracks of an artist come from the library only; to reach the "
                        "providers, expand the artists to albums with the providers scope, "
                        "then the albums to tracks",
                        "expand_artist_tracks_from_providers",
                    )
                )
        elif isinstance(step, SortStep):
            for position, key in enumerate(step.keys):
                definition = _find_field(
                    number, kind, key.field, f"keys[{position}].field", errors, filtering=False
                )
                if definition is not None and not definition.sortable:
                    choices = ", ".join(
                        name for name, item in FIELDS[kind].items() if item.sortable
                    )
                    errors.append(
                        StepError(
                            number,
                            f"keys[{position}].field",
                            f"'{key.field}' has no order; choose one of: {choices}",
                            "field_not_sortable",
                            [key.field, choices],
                        )
                    )
    return errors


def _find_field(
    number: int, kind: ItemKind, name: str, where: str, errors: list[StepError], filtering: bool
) -> FieldDef | None:
    """Return the field of the kind or report how to get it, with the fields a step can use."""
    definition = FIELDS[kind].get(name)
    if definition is not None:
        return definition
    message = f"'{name}' is not available for {_PLURAL[kind]}"
    for other in ItemKind:
        if other != kind and name in FIELDS[other] and (kind, other) in _EXPAND_HINT:
            message += f"; {_EXPAND_HINT[(kind, other)]} before this step"
            key = f"field_needs_{other.value}s_of_{kind.value}s"
            args = [name]
            break
    else:
        choices = ", ".join(
            field.name
            for field in FIELDS[kind].values()
            if (field.name != RANDOM if filtering else field.sortable)
        )
        message += f"; choose one of: {choices}"
        key = f"field_not_for_{kind.value}s"
        args = [name, choices]
    errors.append(StepError(number, where, message, key, args))
    return None


def _check_condition(
    number: int,
    where: str,
    definition: FieldDef,
    op: str,
    value: Any,
    errors: list[StepError],
) -> None:
    """Report an operator or value that does not fit the field."""
    allowed = operators_for(definition)
    if op not in allowed:
        errors.append(
            StepError(
                number,
                f"{where}.op",
                f"'{op}' does not fit '{definition.name}' ({definition.type.value}); "
                f"use one of: {', '.join(allowed)}",
                "operator_not_for_field",
                [op, definition.name, definition.type.value, ", ".join(allowed)],
            )
        )
        return
    if value is None and definition.type == FieldType.REF and op not in NAME_OPERATORS:
        label = definition.label.lower()
        article = "an" if label[0] in "aeiou" else "a"
        problem: TranslatableText | None = step_text(
            f"pick_{definition.name}", f"pick {article} {label}"
        )
    else:
        problem = _value_problem(definition.type, op, value) or _unknown_value(definition, value)
    if problem:
        errors.append(
            StepError(
                number,
                f"{where}.value",
                f"'{definition.name}' {op}: {problem.fallback}",
                "condition_value",
                [definition.name, op, problem],
            )
        )


def _value_problem(field_type: FieldType, op: str, value: Any) -> TranslatableText | None:
    """Return what is wrong with a condition value, or None when it fits."""
    if value is None:
        return step_text("value_missing", "add a value")
    if op == "in_last_days":
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return None
        return step_text("value_not_days", "the value must be a whole number of days above 0")
    if op == "between":
        if isinstance(value, list) and len(value) == 2:
            return next(
                (problem for part in value if (problem := _scalar_problem(field_type, part))),
                None,
            )
        return step_text("value_not_pair", "the value must be a list of two values")
    if op == "one_of":
        if isinstance(value, list) and value and all(isinstance(part, str) for part in value):
            return None
        return step_text("value_not_texts", "the value must be a list of texts")
    return _scalar_problem(field_type, value)


def _unknown_value(definition: FieldDef, value: Any) -> TranslatableText | None:
    """Return what is wrong with a value of a field that only takes fixed values."""
    if not definition.values:
        return None
    allowed = {known.casefold() for known in definition.values}
    choices = ", ".join(definition.values)
    for part in value if isinstance(value, list) else [value]:
        if str(part).casefold() not in allowed:
            return step_text(
                "value_unknown",
                f"'{part}' is not a known value; use one of: {choices}",
                part,
                choices,
            )
    return None


def _scalar_problem(field_type: FieldType, value: Any) -> TranslatableText | None:
    """Return what is wrong with a single value, or None when it fits."""
    if field_type == FieldType.NUMBER:
        ok = isinstance(value, int | float) and not isinstance(value, bool)
        return None if ok else step_text("value_not_number", "the value must be a number")
    if field_type == FieldType.BOOL:
        if isinstance(value, bool):
            return None
        return step_text("value_not_bool", "the value must be true or false")
    if field_type == FieldType.DATE:
        try:
            parse_date(value)
        except TypeError, ValueError:
            return step_text("value_not_date", "the value must be a date such as 2024-05-31")
        return None
    if isinstance(value, str) and value:
        return None
    return step_text("value_not_text", "the value must be a non-empty text")
