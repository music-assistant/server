"""Model/base for a Core controller within Music Assistant."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from music_assistant.common.models.enums import ProviderType
from music_assistant.common.models.provider import ProviderManifest
from music_assistant.constants import CONF_LOG_LEVEL, ROOT_LOGGER_NAME

if TYPE_CHECKING:
    from music_assistant.common.models.config_entries import (
        ConfigEntry,
        ConfigValueType,
        CoreConfig,
    )
    from music_assistant.server import MusicAssistant


class CoreController:
    """Base representation of a Core controller within Music Assistant."""

    domain: str  # used as identifier (=name of the module)
    manifest: ProviderManifest  # some info for the UI only

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize MusicProvider."""
        self.mass = mass
        self._set_logger()
        self.manifest = ProviderManifest(
            type=ProviderType.CORE,
            domain=self.domain,
            name=f"{self.domain.title()} Core controller",
            description=f"{self.domain.title()} Core controller",
            codeowners=["@music-assistant"],
            icon="puzzle-outline",
        )

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> tuple[ConfigEntry, ...]:
        """Return all Config Entries for this core module (if any)."""
        return ()

    async def setup(self, config: CoreConfig) -> None:
        """Async initialize of module."""

    async def close(self) -> None:
        """Handle logic on server stop."""

    async def reload(self, config: CoreConfig | None = None) -> None:
        """Reload this core controller."""
        await self.close()
        if config is None:
            config = await self.mass.config.get_core_config(self.domain)
        log_level = config.get_value(CONF_LOG_LEVEL)
        self._set_logger(log_level)
        await self.setup(config)

    def _set_logger(self, log_level: str | None = None) -> None:
        """Set the logger settings."""
        mass_logger = logging.getLogger(ROOT_LOGGER_NAME)
        self.logger = logging.getLogger(f"{ROOT_LOGGER_NAME}.{self.domain}")
        if log_level is None:
            log_level = self.mass.config.get_raw_core_config_value(
                self.domain, CONF_LOG_LEVEL, "GLOBAL"
            )
        self.log_level = log_level
        if log_level == "GLOBAL":
            self.logger.setLevel(mass_logger.level)
        else:
            self.logger.setLevel("DEBUG" if log_level == "VERBOSE" else log_level)
            # if the root logger's level is higher, we need to adjust that too
            if logging.getLogger().level > self.logger.level:
                logging.getLogger().setLevel(self.logger.level)
