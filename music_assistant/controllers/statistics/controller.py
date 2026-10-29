"""Statistics controller lifecycle and API composition."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.constants import DB_TABLE_PLAY_HISTORY
from music_assistant.controllers.statistics.history import HistoryMixin
from music_assistant.controllers.statistics.rankings import RankingsMixin
from music_assistant.controllers.statistics.time_series import TimeSeriesMixin
from music_assistant.helpers.json import SerializableType

if TYPE_CHECKING:
    from music_assistant_models.config_entries import CoreConfig

    from music_assistant import MusicAssistant


class StatisticsController(RankingsMixin, HistoryMixin, TimeSeriesMixin):
    """Controller handling playlog statistics and analytics."""

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize core controller."""
        super().__init__(mass)
        self.manifest.name = "Statistics controller"
        self.manifest.description = (
            "Music Assistant's core controller for playlog statistics and listening analytics."
        )
        self.manifest.icon = "poll"

    async def setup(self, config: CoreConfig) -> None:
        """Async initialize of statistics module."""
        self.logger.info("Initializing statistics controller...")

    async def get_diagnostics(self) -> dict[str, SerializableType]:
        """Return diagnostics info for this controller to include in diagnostics reports."""
        return {
            "playlog_entries": await self.mass.music.database.get_count(DB_TABLE_PLAY_HISTORY),
        }
