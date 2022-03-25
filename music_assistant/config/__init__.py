"""All classes and helpers for the Configuration."""

from collections import OrderedDict
from typing import Dict, List

from music_assistant.config.models import ConfigEntry, ConfigItem, ValueType
from music_assistant.constants import EventType
from music_assistant.helpers.typing import MusicAssistant
from music_assistant.helpers.util import create_task


class ConfigManager:
    """Class which holds our configuration."""

    def __init__(self, mass: MusicAssistant):
        """Initialize class."""
        self.mass = mass
        self.logger = mass.logger.getChild("config")
        self._config: Dict[str, OrderedDict[str, ConfigItem]] = {}

    async def setup(self):
        """Async initialize of module."""
        # prepare database
        await self.mass.database.execute(
            """CREATE TABLE IF NOT EXISTS config(
                owner TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                UNIQUE(owner, value));"""
        )

    async def register_config_entries(
        self, owner: str, entries: List[ConfigEntry]
    ) -> None:
        """Register config entries on the config manager."""
        for entry in entries:
            if owner not in self._config:
                self._config[owner] = OrderedDict()
            sql_query = "SELECT * FROM config WHERE owner = ? AND key = ?"
            stored_value = self.mass.database.fetchone(
                sql_query, (owner, entry.entry_key)
            )
            self._config[owner][entry.entry_key] = ConfigItem.from_string(
                owner, stored_value, entry
            )

    def get_config(self, owner: str) -> OrderedDict[str, ConfigItem]:
        """Return current configuration for given owner."""
        return self._config.get(owner, {})

    def get_config_item(self, owner: str, entry_key: str) -> ConfigItem | None:
        """Return single configuration item."""
        if config := self.get_config(owner):
            return config.get(entry_key)
        return None

    def get_config_value(self, owner: str, entry_key: str) -> ValueType:
        """Return single configuration item value."""
        if config_item := self.get_config_item(owner, entry_key):
            return config_item.value
        return None

    def all_items(self) -> Dict[str, OrderedDict[str, ConfigItem]]:
        """Return entire config."""
        return self._config

    def set_config(self, owner: str, key: str, value: ValueType) -> None:
        """Set value of the given config item."""
        prev_value = self._config[owner][key].value
        if value is None:
            # restore default
            value = self._config[owner][key].entry.default_value
        if prev_value != value:
            self._config[owner][key].value = value
            create_task(
                self.mass.database.insert_or_replace(
                    "config", {"key": key, "value": str(value)}
                )
            )
            self.mass.signal_event(EventType.CONFIG_CHANGED, self._config[owner][key])
