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
class ConfigEntryBase(DataClassDictMixin):
    """Base Model for Config Entries."""

    # key: used as identifier for the entry, also for localization
    key: str
    type: ConfigEntryType
    # options [optional]: select from list of possible values/options
    options: Optional[List[ConfigValueOption]] = None
    # range [optional]: select values within range
    range: Optional[Tuple[int, int]] = None
    # description [optional]: extended description of the setting.
    description: Optional[str] = None
    # help_link [optional]: link to help article.
    help_link: Optional[str] = None
    # multi_value [optional]: allow multiple values from the list
    multi_value: bool = False
    # depends_on [optional]: needs to be set before this setting shows up in frontend
    depends_on: Optional[str] = None
    # hidden: hide from UI
    hidden: bool = False


@dataclass
class ConfigEntry(ConfigEntryBase):
    """Model for a Config Entry definition."""

    default_value: ValueTypes = None


@dataclass
class ConfigValue(ConfigEntryBase):
    """Model for a Config Entry Value."""

    # path: full path to get/set this value (e.g. music_providers/spotify/marcel/username)
    path: str = None
    value: ValueTypes = None
