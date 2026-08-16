"""Generic condition evaluator for the Library Automations plugin."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from music_assistant_models.media_items import MediaItemType

    from music_assistant.providers.library_automations.models import AutomationCondition

# supported condition fields in v1: extend _field_value() below to add more
SUPPORTED_CONDITION_FIELDS = ("name", "genre", "provider", "explicit")


def _field_value(item: MediaItemType, field_name: str) -> Any:
    """Extract the value to test for a given condition field from a media item."""
    if field_name == "name":
        return item.name
    if field_name == "genre":
        return list(item.metadata.genres) if item.metadata and item.metadata.genres else []
    if field_name == "provider":
        return [m.provider_domain for m in item.provider_mappings] + [
            m.provider_instance for m in item.provider_mappings
        ]
    if field_name == "explicit":
        return item.metadata.explicit if item.metadata else None
    return None


def _evaluate_single(condition: AutomationCondition, item: MediaItemType) -> bool:
    """Evaluate a single condition predicate against an item."""
    actual = _field_value(item, condition.field)
    if condition.operator == "eq":
        return actual == condition.value
    if condition.operator == "contains":
        if isinstance(actual, list):
            return any(
                isinstance(v, str)
                and isinstance(condition.value, str)
                and condition.value.lower() in v.lower()
                for v in actual
            )
        return (
            isinstance(actual, str)
            and isinstance(condition.value, str)
            and condition.value.lower() in actual.lower()
        )
    if condition.operator == "in":
        return isinstance(actual, list) and condition.value in actual
    return False


def evaluate_conditions(
    conditions: list[AutomationCondition], logic: str, item: MediaItemType
) -> bool:
    """Evaluate a list of conditions against an item using AND/OR logic; no conditions => match."""
    if not conditions:
        return True
    results = [_evaluate_single(c, item) for c in conditions]
    return all(results) if logic == "AND" else any(results)
