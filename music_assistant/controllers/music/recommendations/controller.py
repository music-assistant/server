"""Recommendations subcontroller: aggregates library + provider recommendation rows."""

from __future__ import annotations

import asyncio
import logging
from itertools import zip_longest
from typing import TYPE_CHECKING, cast

from music_assistant_models.enums import ProviderFeature

from music_assistant.constants import MASS_LOGGER_NAME

from .sources.defaults import build_default_sources

if TYPE_CHECKING:
    from music_assistant_models.media_items import RecommendationFolder

    from music_assistant.mass import MusicAssistant
    from music_assistant.models.metadata_provider import MetadataProvider
    from music_assistant.models.music_provider import MusicProvider
    from music_assistant.models.plugin import PluginProvider

    from .sources.base import RecommendationSource


class RecommendationsController:
    """Owns the registry of recommendation sources and builds the recommendations response."""

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize the controller and register its api command."""
        self.mass = mass
        self.logger = logging.getLogger(f"{MASS_LOGGER_NAME}.music.recommendations")
        self._sources: list[RecommendationSource] = build_default_sources(mass)
        self.mass.register_api_command("music/recommendations", self.get_recommendations)

    @property
    def sources(self) -> list[RecommendationSource]:
        """Return a copy of the registered sources."""
        return list(self._sources)

    def register(self, source: RecommendationSource) -> None:
        """Register an additional recommendation source."""
        self._sources.append(source)

    async def get_recommendations(self) -> list[RecommendationFolder]:
        """Get all recommendations (default library rows + provider rows, interleaved)."""
        providers = self.mass.music._apply_user_provider_filter(
            self.mass.get_providers_supporting_feature(ProviderFeature.RECOMMENDATIONS)
        )
        results_per_provider: list[list[RecommendationFolder]] = await asyncio.gather(
            self._default_recommendations(),
            *[
                self._provider_recommendations(
                    cast("MusicProvider | MetadataProvider | PluginProvider", provider)
                )
                for provider in providers
            ],
        )
        # keep each provider's index so the result is interleaved as today
        return [item for sublist in zip_longest(*results_per_provider) for item in sublist if item]

    async def _default_recommendations(self) -> list[RecommendationFolder]:
        """Build the default library recommendation rows from the source registry."""
        folders = await asyncio.gather(*[source.build() for source in self._sources])
        return [folder for folder in folders if folder is not None]

    async def _provider_recommendations(
        self, provider: MusicProvider | MetadataProvider | PluginProvider
    ) -> list[RecommendationFolder]:
        """Return recommendations from a single provider, swallowing errors."""
        try:
            return await provider.recommendations()
        except Exception as err:
            self.logger.warning(
                "Error while fetching recommendations from %s: %s",
                provider.name,
                str(err),
                exc_info=err if self.logger.isEnabledFor(logging.DEBUG) else None,
            )
            return []
