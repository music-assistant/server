"""Model for Music Assistant Event."""

from dataclasses import dataclass
from typing import Any

from mashumaro import DataClassDictMixin

from music_assistant.common.models.enums import EventType


@dataclass
class MassEvent(DataClassDictMixin):
    """Representation of an Event emitted in/by Music Assistant."""

    event: EventType
    object_id: str | None = None  # player_id, queue_id or uri
    data: Any = None  # optional data (such as the object)
