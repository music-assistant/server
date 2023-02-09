"""Model for Music Assistant Event."""

from dataclasses import dataclass
from typing import Any

from music_assistant.common.models.enums import EventType

from mashumaro import DataClassDictMixin


@dataclass
class MassEvent(DataClassDictMixin):
    """Representation of an Event emitted in/by Music Assistant."""

    event: EventType
    object_id: str | None = None  # player_id, queue_id or uri
    data: Any = None  # optional data (such as the object)
