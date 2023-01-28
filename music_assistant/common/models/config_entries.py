"""Model and helpers for Config entries."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from mashumaro import DataClassDictMixin

from music_assistant.common.models.enums import ProviderType


class ConfigEntryType(Enum):
    """Enum for the type of a config entry."""

    BOOLEAN = "boolean"
    STRING = "string"
    PASSWORD = "password"
    INT = "integer"
    FLOAT = "float"
    LABEL = "label"
    DICT = "dict"


ConfigValueTypes = str | int | float | bool | dict | None

ConfigEntryTypeMap = {
    ConfigEntryType.BOOLEAN: bool,
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
    within Music Assistant (without the value).
    """

    # key: used as identifier for the entry, also for localization
    key: str
    type: ConfigEntryType
    # label: default label when no translation for the key is present
    label: str
    default_value: ConfigValueTypes = None
    required: bool = True
    # options [optional]: select from list of possible values/options
    options: list[ConfigValueOption] | None = None
    # range [optional]: select values within range
    range: tuple[int, int] | None = None
    # description [optional]: extended description of the setting.
    description: str | None = None
    # help_link [optional]: link to help article.
    help_link: str | None = None
    # multi_value [optional]: allow multiple values from the list
    multi_value: bool = False
    # depends_on [optional]: needs to be set before this setting shows up in frontend
    depends_on: str | None = None
    # hidden: hide from UI
    hidden: bool = False


@dataclass
class ConfigEntryValue(ConfigEntry):
    """Config Entry with its value parsed."""

    value: ConfigValueTypes = None

    @classmethod
    def parse(cls, entry: ConfigEntry, value: ConfigValueTypes) -> "ConfigEntryValue":
        """Parse ConfigEntryValue from the config entry and plain value."""
        result = ConfigEntryValue.from_dict(entry.to_dict)
        result.value = value
        expected_type = ConfigEntryTypeMap.get(result.value_type)
        if result.value is None and not entry.required:
            expected_type = None
        if not isinstance(result.value, expected_type):
            raise ValueError(f"{result.key} has unexpected type: {type(result.value)}")
        return result


CONF_KEY_ENABLED = "enabled"
CONFIG_ENTRY_ENABLED = ConfigEntry(
    key=CONF_KEY_ENABLED,
    type=ConfigEntryType.BOOLEAN,
    label="Enabled",
    default_value=True,
)


@dataclass
class ProviderConfig(DataClassDictMixin):
    """Provider(instance) Configuration."""

    type: ProviderType
    domain: str
    instance_id: str
    # values: the configuration values
    values: dict[str, ConfigValueTypes]
    # title: a custom title for this provider instance/config
    title: str | None = None
