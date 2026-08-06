"""Recommendation logic for Telmore Musik."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.providers.yousee.recommendations import (
    YouSeeRecommendationsManager,
)

if TYPE_CHECKING:
    from music_assistant.providers.telmore.provider import TelmoreMusikProvider


class TelmoreRecommendationsManager(YouSeeRecommendationsManager):
    """Manages Telmore Musik recommendations."""

    def __init__(self, provider: TelmoreMusikProvider):
        """Initialize recommendation manager."""
        self.provider = provider  # type: ignore[assignment]
        self.api = provider.api
        self.auth = provider.auth  # type: ignore[assignment]
        self.logger = provider.logger
        self.mass = provider.mass
