"""Data models for the Library Automations plugin: rules made of trigger + conditions + action."""

from __future__ import annotations

import time
import uuid as _uuid
from dataclasses import dataclass, field
from typing import Any

from music_assistant_models.enums import MediaType
from music_assistant_models.errors import InvalidDataError

RULES_FILENAME = "library_automations_rules.json"

CONDITION_LOGIC_AND = "AND"
CONDITION_LOGIC_OR = "OR"
CONDITION_OPERATORS = ("eq", "contains", "in")

VALID_MEDIA_TYPES = (MediaType.TRACK.value, MediaType.ALBUM.value, MediaType.ARTIST.value)

# --- trigger type ids (metadata lives in triggers.py, kept separate to avoid an import cycle) ---
TRIGGER_MEDIA_ITEM_UNFAVORITED = "media_item_unfavorited"
TRIGGER_MEDIA_ITEM_FAVORITED = "media_item_favorited"
TRIGGER_MEDIA_ITEM_ADDED_TO_LIBRARY = "media_item_added_to_library"

# --- action type ids (metadata lives in actions.py) ---
ACTION_ADD_TO_PLAYLIST = "add_to_playlist"
ACTION_REMOVE_FROM_PLAYLIST = "remove_from_playlist"
ACTION_REMOVE_FROM_LIBRARY = "remove_from_library"


def _coerce_str(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidDataError(f"Invalid value for {field_name}: {value!r}")
    return value


def _coerce_str_list(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise InvalidDataError(f"Expected list for {field_name}, got {type(value).__name__}")
    return [str(item) for item in value]


def _coerce_dict(value: Any, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise InvalidDataError(f"Expected dict for {field_name}, got {type(value).__name__}")
    return dict(value)


@dataclass
class AutomationTrigger:
    """The event that arms an automation rule."""

    type: str
    media_types: list[str] = field(default_factory=lambda: [MediaType.TRACK.value])
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {"type": self.type, "media_types": self.media_types, "params": self.params}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AutomationTrigger:
        """Deserialize from dictionary."""
        media_types = data.get("media_types") or [MediaType.TRACK.value]
        return cls(
            type=_coerce_str(data.get("type"), "trigger.type"),
            media_types=_coerce_str_list(media_types, "trigger.media_types"),
            params=_coerce_dict(data.get("params"), "trigger.params"),
        )


@dataclass
class AutomationCondition:
    """A single predicate evaluated against the triggering media item."""

    field: str
    operator: str
    value: Any

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {"field": self.field, "operator": self.operator, "value": self.value}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AutomationCondition:
        """Deserialize from dictionary."""
        operator = _coerce_str(data.get("operator"), "condition.operator")
        if operator not in CONDITION_OPERATORS:
            msg = f"Invalid condition operator: {operator!r}. Must be one of {CONDITION_OPERATORS}"
            raise InvalidDataError(msg)
        return cls(
            field=_coerce_str(data.get("field"), "condition.field"),
            operator=operator,
            value=data.get("value"),
        )


@dataclass
class AutomationAction:
    """The effect a matched rule performs."""

    type: str
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {"type": self.type, "params": self.params}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AutomationAction:
        """Deserialize from dictionary."""
        return cls(
            type=_coerce_str(data.get("type"), "action.type"),
            params=_coerce_dict(data.get("params"), "action.params"),
        )


@dataclass
class AutomationRule:
    """A single library automation rule: trigger + conditions + action."""

    id: str
    name: str
    trigger: AutomationTrigger
    action: AutomationAction
    enabled: bool = True
    conditions: list[AutomationCondition] = field(default_factory=list)
    condition_logic: str = CONDITION_LOGIC_AND
    created_at: int = field(default_factory=lambda: int(time.time()))

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "id": self.id,
            "name": self.name,
            "enabled": self.enabled,
            "trigger": self.trigger.to_dict(),
            "conditions": [c.to_dict() for c in self.conditions],
            "condition_logic": self.condition_logic,
            "action": self.action.to_dict(),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AutomationRule:
        """Deserialize from dictionary."""
        condition_logic = data.get("condition_logic", CONDITION_LOGIC_AND)
        if condition_logic not in (CONDITION_LOGIC_AND, CONDITION_LOGIC_OR):
            msg = f"Invalid condition_logic: {condition_logic!r}. Must be AND or OR."
            raise InvalidDataError(msg)
        return cls(
            id=_coerce_str(data.get("id") or str(_uuid.uuid4()), "id"),
            name=_coerce_str(data.get("name"), "name"),
            enabled=bool(data.get("enabled", True)),
            trigger=AutomationTrigger.from_dict(_coerce_dict(data.get("trigger"), "trigger")),
            conditions=[AutomationCondition.from_dict(c) for c in (data.get("conditions") or [])],
            condition_logic=condition_logic,
            action=AutomationAction.from_dict(_coerce_dict(data.get("action"), "action")),
            created_at=int(data.get("created_at") or time.time()),
        )


def new_rule_id() -> str:
    """Return a new unique rule id."""
    return str(_uuid.uuid4())


def validate_rule_media_types(rule: AutomationRule) -> None:
    """
    Raise InvalidDataError if the rule's trigger media_types are missing/invalid.

    Split out from the trigger/action-type checks (see triggers.py/actions.py, which own
    those registries) so this module needs no import of either - avoiding a circular import,
    since both of them import from here.
    """
    if not rule.trigger.media_types:
        raise InvalidDataError("trigger.media_types must not be empty")
    for media_type in rule.trigger.media_types:
        if media_type not in VALID_MEDIA_TYPES:
            msg = f"Invalid media_type: {media_type!r}. Must be one of {VALID_MEDIA_TYPES}"
            raise InvalidDataError(msg)
