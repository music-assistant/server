"""Generic condition evaluator for the Library Automations plugin."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from music_assistant_models.errors import MusicAssistantError

if TYPE_CHECKING:
    from music_assistant_models.media_items import MediaItemType

    from music_assistant.providers.library_automations import LibraryAutomationsProvider
    from music_assistant.providers.library_automations.models import AutomationCondition

# supported condition fields in v1: extend _field_value() (or, for fields that need a DB/provider
# lookup rather than a plain attribute read, _evaluate_async_field()) below to add more
SUPPORTED_CONDITION_FIELDS = ("name", "genre", "provider", "explicit", "in_playlist")
# Fields evaluated via _evaluate_async_field() (need an async lookup) instead of _field_value().
ASYNC_CONDITION_FIELDS = ("in_playlist",)


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


async def _item_in_any_playlist(
    provider: LibraryAutomationsProvider, item: MediaItemType, playlist_ids: list[Any]
) -> bool:
    """Return True if the item is a track of any of the given (library) playlist ids."""
    if not item.uri:
        return False
    for playlist_id in playlist_ids:
        try:
            async for track in provider.mass.music.playlists.tracks(str(playlist_id), "library"):
                if track.uri == item.uri:
                    return True
        except MusicAssistantError:
            # a deleted/invalid playlist_id shouldn't break the whole rule evaluation
            continue
    return False


async def _evaluate_async_field(
    condition: AutomationCondition, item: MediaItemType, provider: LibraryAutomationsProvider
) -> bool:
    """Evaluate a condition whose field needs an async DB/provider lookup."""
    if condition.field == "in_playlist":
        playlist_ids = condition.value if isinstance(condition.value, list) else [condition.value]
        return await _item_in_any_playlist(provider, item, playlist_ids)
    return False


def _evaluate_single(condition: AutomationCondition, item: MediaItemType) -> bool:
    """Evaluate a single (synchronous-field) condition predicate against an item."""
    actual = _field_value(item, condition.field)
    if condition.operator == "eq":
        return bool(actual == condition.value)
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


async def evaluate_conditions(
    conditions: list[AutomationCondition],
    logic: str,
    item: MediaItemType,
    provider: LibraryAutomationsProvider,
) -> bool:
    """Evaluate a list of conditions against an item using AND/OR logic; no conditions => match."""
    if not conditions:
        return True
    results = [
        await _evaluate_async_field(condition, item, provider)
        if condition.field in ASYNC_CONDITION_FIELDS
        else _evaluate_single(condition, item)
        for condition in conditions
    ]
    return all(results) if logic == "AND" else any(results)
