"""Core controller holding the default AI engines the features of Music Assistant use."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.constants import CONF_AI
from music_assistant.helpers.plugin_engines import (
    EngineKind,
    create_default_engine_config_entries,
)
from music_assistant.models.core_controller import CoreController

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, CoreConfig

    from music_assistant.mass import MusicAssistant


class AIController(CoreController):
    """Holds the default text, speech and image engine the features use."""

    domain: str = CONF_AI

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize the AI controller."""
        super().__init__(mass)
        self.manifest.name = "AI"
        self.manifest.description = "The default AI engines the features of Music Assistant use."
        self.manifest.icon = "creation"

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return the pickers of the default engine of each kind."""
        return (
            *await create_default_engine_config_entries(self.mass, EngineKind.TEXT),
            *await create_default_engine_config_entries(self.mass, EngineKind.SPEECH),
            *await create_default_engine_config_entries(self.mass, EngineKind.IMAGE),
        )

    async def setup(self, config: CoreConfig) -> None:
        """Async initialize of the AI controller."""
        # the config read takes the stored block before it builds the entries, so a default
        # adopted while building them only shows when that block already exists
        self.mass.config.ensure_core_config_base(self.domain)
