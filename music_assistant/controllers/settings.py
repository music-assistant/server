"""Provides a simple Settings controller to store key/value pairs."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Union

from .database import TABLE_SETTINGS

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

SETTING_TYPE_MAP = {
    "int": int,
    "float": float,
    "str": str,
    "dict": dict,
}
SettingType = Union[int, float, str, dict]


class SettingsController:
    """Basic settings controller to get/set key/value pairs."""

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize our caching class."""
        self.mass = mass
        self.logger = mass.logger.getChild("settings")
        self._settings: Dict[str, Any] = {}

    async def setup(self) -> None:
        """Async initialize of settings module."""
        # read all settings into memory at start
        for db_row in await self.mass.database.get_rows(TABLE_SETTINGS):
            value_type = SETTING_TYPE_MAP[db_row["type"]]
            self._settings[db_row["key"]] = value_type(db_row["value"])

    def get(self, key: str, default=None) -> SettingType | None:
        """Get config value by key."""
        return self._settings.get(key, default)

    def set(self, key: str, value: SettingType | None) -> None:
        """Set value in settings (or deletes if None)."""
        self.mass.create_task(self.async_set(key, value))

    async def async_set(self, key: str, value: SettingType | None) -> None:
        """Set value in settings (or deletes if None)."""
        if value is None:
            self._settings.pop(key, None)
            await self.mass.database.delete(TABLE_SETTINGS, {"key": key})
            return
        self._settings[key] = value
        # store persistent in db
        type_str = str(type(value))
        assert type_str in SettingType, "Unsupported value type"
        await self.mass.database.insert(
            TABLE_SETTINGS,
            {"key": key, "value": str(value), "type": type_str},
            allow_replace=True,
        )
