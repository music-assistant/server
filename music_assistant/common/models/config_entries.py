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


ConfigValueTypes = Union[str, int, float, bool, dict, None]

ConfigEntryTypeMap = {
    ConfigEntryType.BOOL: bool,
    ConfigEntryType.STRING: str,
    ConfigEntryType.PASSWORD: str,
    ConfigEntryType.INT: int,
    ConfigEntryType.FLOAT: float,
    ConfigEntryType.LABEL: str,
}


@dataclass
class ConfigValueOption(DataClassDictMixin):
    """Model for a value with seperated name/value."""

    text: str
    value: ConfigValueTypes


@dataclass
class ConfigEntry(DataClassDictMixin):
    """
    Model for a Config Entry.

    The definition of something that can be configured for an opbject (e.g. provider or player)
    within Music Assistant. Its value will be loaded at runtime.
    """

    # key: used as identifier for the entry, also for localization
    key: str
    value_type: ConfigEntryType
    # label: default label when no translation for the key is present
    label: str
    default_value: ConfigValueTypes = None
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
    # require_reload: if the object depending on this entry must be reloaded on change
    require_reload: bool = False
    # value: the actual value of the configentry, only available at runtime for active instances.
    value: ConfigValueTypes = None


class ConfigValues(dict):

    @classmethod
    def parse(cls: "ConfigValues", entries: list[ConfigEntry], values: dict[str, ConfigValueTypes]):
        result = {}
        for entry in entries:
            entry.value = values.get(entry.key, entry.default_value)
            expected_type = ConfigEntryTypeMap.get(entry.value_type)
            if not isinstance(entry.value, expected_type):
                raise ValueError(f"{entry.key} has unexpected type: {type(entry.value)}")
            result[entry.key] = result

# ConfigValues = dict[str, ConfigValueTypes]


CONFIG_ENTRY_ENABLED = ConfigEntry(
    key="enabled", type=ConfigEntryType.BOOL, require_reload=True
)
