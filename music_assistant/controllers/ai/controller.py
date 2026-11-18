"""Core controller holding the default AI engines the features of Music Assistant use."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.auth import Scope

from music_assistant.constants import CONF_AI
from music_assistant.helpers.api import api_command
from music_assistant.helpers.plugin_engines import (
    EngineInfo,
    EngineKind,
    EngineUsage,
    create_default_engine_config_entries,
    list_engine_usage,
    list_engines,
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

    @api_command("ai/engines", required_scope=Scope.CONFIG_CORE_READ)
    async def get_engines(self, kind: EngineKind | None = None) -> list[EngineInfo]:
        """
        Return the available engines, of one kind or of all kinds.

        :param kind: The kind of engine to return, or None for all kinds.
        """
        return await list_engines(self.mass, kind)

    @api_command("ai/usage", required_scope=Scope.CONFIG_CORE_READ)
    async def get_usage(self) -> list[EngineUsage]:
        """Return each engine setting of the core modules and providers, with its engine."""
        return await list_engine_usage(self.mass)

    async def setup(self, config: CoreConfig) -> None:
        """Async initialize of the AI controller."""
        # the config read takes the stored block before it builds the entries, so a default
        # adopted while building them only shows when that block already exists
        self.mass.config.ensure_core_config_base(self.domain)
