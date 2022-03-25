"""Model and helpers for Config entries."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Tuple

from mashumaro import DataClassDictMixin
import ujson


class ConfigEntryType(Enum):
    """Enum for the type of a config entry."""

    BOOL = "boolean"
    STRING = "string"
    PASSWORD = "password"
    INT = "integer"
    FLOAT = "float"
    LABEL = "label"
    DICT = "dict"
    MULTI_INT = "multi_int"
    MULTI_STRING = "multi_string"


ValueType = str | int | float | bool | dict | None | List[str | int]


@dataclass
class ConfigValueOption(DataClassDictMixin):
    """Model for a value with seperated name/value."""

    text: str
    value: ValueType


@dataclass(frozen=True)
class ConfigEntry(DataClassDictMixin):
    """Model for a Config Entry."""

    entry_key: str
    entry_type: ConfigEntryType
    default_value: ValueType = None
    # options: select from list of possible values/options
    options: Optional[List[ConfigValueOption]] = None
    range: Optional[Tuple[int, int]] = None  # select values within range
    label: Optional[str] = None  # a friendly name for the setting


@dataclass
class ConfigItem(DataClassDictMixin):
    """Model for a Configuration item."""

    owner: str
    entry: ConfigEntry
    key: str | None = None
    value: ValueType = None

    def __post_init__(self) -> None:
        """Handle post init."""
        if self.key is None:
            self.key = self.entry.entry_key
        if self.value is None:
            self.value = self.entry.default_value

    @classmethod
    def from_string(cls, owner: str, value: str, entry: ConfigEntry) -> "ConfigItem":
        """Parse value from string."""
        val = value
        if entry.entry_type == ConfigEntryType.BOOL:
            val = bool(val)
        elif entry.entry_type == ConfigEntryType.DICT:
            val = ujson.loads(value)
        elif entry.entry_type == ConfigEntryType.MULTI_STRING:
            val = ujson.loads(value)
        elif entry.entry_type == ConfigEntryType.MULTI_INT:
            val = ujson.loads(value)
        elif entry.entry_type == ConfigEntryType.INT:
            val = int(val)
        elif entry.entry_type == ConfigEntryType.FLOAT:
            val = float(val)
        return ConfigItem(owner=owner, entry=entry, value=val)

    def to_string(self) -> str | None:
        """Return value as string for storage in db."""
        if self.value is None:
            return self.value
        if "multi" in self.entry.entry_type.value:
            return ujson.dumps(self.value)
        if self.entry.entry_type == ConfigEntryType.DICT:
            return ujson.dumps(self.value)
        return str(self.value)
