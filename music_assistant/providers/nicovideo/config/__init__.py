"""Nicovideo provider configuration system."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .categories import (
    AuthConfigCategory,
    ContentConfigCategory,
    RecommendationsConfigCategory,
)
from .factory import get_config_entries_impl

if TYPE_CHECKING:
    from music_assistant.models.provider import Provider


class NicovideoConfig:
    """New category-based configuration system."""

    def __init__(self, provider: Provider) -> None:
        """Initialize with all category instances."""
        self.auth = AuthConfigCategory(provider)
        self.content = ContentConfigCategory(provider)
        self.recommendations = RecommendationsConfigCategory(provider)


__all__ = [
    "NicovideoConfig",
    "get_config_entries_impl",
]
