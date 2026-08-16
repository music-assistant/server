"""Trigger registry and matching logic for the Library Automations plugin."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from music_assistant_models.enums import EventType

from music_assistant.providers.library_automations.models import (
    TRIGGER_MEDIA_ITEM_ADDED_TO_LIBRARY,
    TRIGGER_MEDIA_ITEM_FAVORITED,
    TRIGGER_MEDIA_ITEM_UNFAVORITED,
)

if TYPE_CHECKING:
    from music_assistant_models.media_items import MediaItemType

    from music_assistant.providers.library_automations.models import AutomationTrigger


@dataclass(frozen=True)
class TriggerDefinition:
    """Static metadata describing a trigger type, surfaced via list_trigger_types()."""

    id: str
    label: str
    description: str
    source_event: EventType


TRIGGER_TYPES: dict[str, TriggerDefinition] = {
    TRIGGER_MEDIA_ITEM_UNFAVORITED: TriggerDefinition(
        id=TRIGGER_MEDIA_ITEM_UNFAVORITED,
        label="Item unfavorited",
        description="Fires the moment a track, album or artist is removed from favorites.",
        source_event=EventType.MEDIA_ITEM_UPDATED,
    ),
    TRIGGER_MEDIA_ITEM_FAVORITED: TriggerDefinition(
        id=TRIGGER_MEDIA_ITEM_FAVORITED,
        label="Item favorited",
        description="Fires the moment a track, album or artist is marked as favorite.",
        source_event=EventType.MEDIA_ITEM_UPDATED,
    ),
    TRIGGER_MEDIA_ITEM_ADDED_TO_LIBRARY: TriggerDefinition(
        id=TRIGGER_MEDIA_ITEM_ADDED_TO_LIBRARY,
        label="Item added to library",
        description="Fires when a track, album or artist is newly added to the library.",
        source_event=EventType.MEDIA_ITEM_ADDED,
    ),
}


def trigger_matches(
    trigger: AutomationTrigger, fired_trigger_type: str, item: MediaItemType
) -> bool:
    """Return True if a fired trigger_type/item matches a rule's configured trigger."""
    if trigger.type != fired_trigger_type:
        return False
    item_media_type = getattr(item, "media_type", None)
    if item_media_type is None:
        return False
    return item_media_type.value in trigger.media_types
