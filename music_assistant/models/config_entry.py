"""Model and helpers for Config entries."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, List, Optional, Tuple


class ConfigEntryType(Enum):
    """Enum for the type of a config entry."""
    BOOL = "boolean"
    STRING = "string"
    INT = "integer"
    FLOAT = "float"
    PLAYER_ID = "player_id"
    VOLUME_CONTROL = "volume_control"
    POWER_CONTROL = "power_control"



@dataclass
class ConfigEntry():
    """Model for a Config Entry."""
    entry_key: str
    entry_type: ConfigEntryType
    default_value: Optional[Any] = None
    values: List[Any] = field(default_factory=list) # select from list of values
    range: Tuple[Any] = () # select values within range
    description_key: Optional[str] = None  # key in the translations file
    help_key: Optional[str] = None # key in the translations file
    multi_value: bool = False # allow multiple values from the list
