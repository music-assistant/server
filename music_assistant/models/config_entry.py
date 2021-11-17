"""Model and helpers for Config entries."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Tuple, Union

from mashumaro import DataClassDictMixin


class ConfigEntryType(Enum):
    """Enum for the type of a config entry."""

    BOOL = "boolean"
    STRING = "string"
    PASSWORD = "password"
    INT = "integer"
    FLOAT = "float"
    LABEL = "label"
    DICT = "dict"


ValueTypes = Union[str, int, float, bool, dict, None]


@dataclass
class ConfigValueOption(DataClassDictMixin):
    """Model for a value with seperated name/value."""

    text: str
    value: ValueTypes


@dataclass
class ConfigEntry(DataClassDictMixin):
    """Model for a Config Entry."""

    entry_key: str
    entry_type: ConfigEntryType
    default_value: ValueTypes = None
    # options: select from list of possible values/options
    options: Optional[List[ConfigValueOption]] = None
    range: Optional[Tuple[int, int]] = None  # select values within range
    label: Optional[str] = None  # a friendly name for the setting
    description: Optional[str] = None  # extended description of the setting.
    help_key: Optional[str] = None  # key in the translations file
    multi_value: bool = False  # allow multiple values from the list
    depends_on: Optional[
        str
    ] = None  # needs to be set before this setting shows up in frontend
    hidden: bool = False  # hide from UI
    value: ValueTypes = None  # set by the configuration manager
    store_hashed: bool = False  # value will be hashed, non reversible
