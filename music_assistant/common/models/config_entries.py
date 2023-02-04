"""Model and helpers for Config entries."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

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


ConfigValueType = str | int | float | bool | dict | None

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
    value: ConfigValueType


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
    default_value: ConfigValueType = None
    required: bool = True
    # options [optional]: select from list of possible values/options
    options: tuple[ConfigValueOption] | None = None
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
    # advanced: this is an advanced setting (frontend hides it in some corner)
    advanced: bool = False


@dataclass
class ConfigEntryValue(ConfigEntry):
    """Config Entry with its value parsed."""

    value: ConfigValueType = None

    @classmethod
    def parse(cls, entry: ConfigEntry, value: ConfigValueType) -> "ConfigEntryValue":
        """Parse ConfigEntryValue from the config entry and plain value."""
        result = ConfigEntryValue.from_dict(entry.to_dict)
        result.value = value
        expected_type = ConfigEntryTypeMap.get(result.type)
        if result.value is None and not entry.required:
            expected_type = None
        if not isinstance(result.value, expected_type):
            raise ValueError(f"{result.key} has unexpected type: {type(result.value)}")
        return result


@dataclass
class Config(DataClassDictMixin):
    """Base Configuration object."""

    values: dict[str, ConfigEntryValue]

    def get_value(self, key: str) -> ConfigValueType:
        """Return config value for given key."""
        return self.values[key].value

    @classmethod
    def parse(
        cls: object, config_entries: list[ConfigEntry], raw: dict[str, Any]
    ) -> "PlayerConfig":
        """Parse PlayerConfig from the raw values (as stored in persistent storage)."""
        values = {
            x.key: ConfigEntryValue.parse(x, raw.get("values", {}).get(x.key))
            for x in config_entries
        }
        return cls(**raw, values=values)

    def to_raw(self) -> dict[str, Any]:
        """Return minimized/raw dict to store in persistent storage."""
        return {
            **self.to_dict(),
            "values": {
                x.key: x.value
                for x in self.values.values()
                if x.value != x.default_value
            },
        }


@dataclass
class ProviderConfig(Config):
    """Provider(instance) Configuration."""

    type: ProviderType
    domain: str
    instance_id: str
    # enabled: boolean to indicate if the provider is enabled
    enabled: bool = True
    # name: an (optional) custom name for this provider instance/config
    title: str | None = None


@dataclass
class PlayerConfig(Config):
    """Player Configuration."""

    provider: str
    player_id: str
    # enabled: boolean to indicate if the player is enabled
    enabled: bool = True
    # name: an (optional) custom name for this player
    name: str | None = None
