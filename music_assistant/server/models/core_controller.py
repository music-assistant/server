"""Model/base for a Core controller within Music Assistant."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from music_assistant.constants import CONF_LOG_LEVEL, ROOT_LOGGER_NAME

if TYPE_CHECKING:
    from music_assistant.common.models.config_entries import ConfigEntry, ConfigValueType
    from music_assistant.server import MusicAssistant


class CoreController:
    """Base representation of a Core controller within Music Assistant."""

    name: str
    friendly_name: str

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize MusicProvider."""
        self.mass = mass
        self.logger = logging.getLogger(f"{ROOT_LOGGER_NAME}.{self.name}")
        log_level = self.mass.config.get_raw_core_config_value(self.name, CONF_LOG_LEVEL, "GLOBAL")
        if log_level != "GLOBAL":
            self.logger.setLevel(log_level)

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> tuple[ConfigEntry, ...]:
        """Return all Config Entries for this core module (if any)."""

    async def setup(self) -> None:
        """Async initialize of module."""

    async def close(self) -> None:
        """Handle logic on server stop."""

    async def reload(self) -> None:
        """Reload this core controller."""
        await self.close()
        log_level = self.mass.config.get_raw_core_config_value(self.name, CONF_LOG_LEVEL, "GLOBAL")
        if log_level == "GLOBAL":
            log_level = logging.getLogger(ROOT_LOGGER_NAME).level
        self.logger.setLevel(log_level)
        await self.setup()
