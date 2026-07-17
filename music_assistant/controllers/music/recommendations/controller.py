"""Recommendations subcontroller: aggregates library + provider recommendation rows."""

from __future__ import annotations

import asyncio
import inspect
import logging
from itertools import zip_longest
from typing import TYPE_CHECKING, cast

from music_assistant_models.auth import Scope
from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.helpers import create_uri

from music_assistant.constants import MASS_LOGGER_NAME
from music_assistant.controllers.music.constants import RECOMMENDATIONS_PROVIDER_TIMEOUT

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
        self.mass.register_api_command(
            "music/recommendations",
            self.get_recommendations,
            required_scope=Scope.LIBRARY_READ,
        )

    @property
    def sources(self) -> list[RecommendationSource]:
        """Return a copy of the registered sources."""
        return list(self._sources)

    def register(self, source: RecommendationSource) -> None:
        """Register an additional recommendation source."""
        self._sources.append(source)

    async def get_recommendations(
        self, wanted: list[str] | None = None
    ) -> list[RecommendationFolder]:
        """
        Get all recommendations (default library rows + provider rows, interleaved).

        :param wanted: optional list of row URIs to build; when omitted, all rows are built.
            When given, providers with no wanted rows are not called at all, and only the
            requested rows are returned.
        """
        wanted_set = set(wanted) if wanted is not None else None
        providers = self.mass.music._apply_user_provider_filter(
            self.mass.get_providers_supporting_feature(ProviderFeature.RECOMMENDATIONS)
        )
        if wanted_set is not None:
            wanted_provider_ids = {uri.split("://", 1)[0] for uri in wanted_set}
            providers = [p for p in providers if p.instance_id in wanted_provider_ids]
        results_per_provider: list[list[RecommendationFolder]] = await asyncio.gather(
            self._default_recommendations(wanted_set),
            *[
                self._provider_recommendations(
                    cast("MusicProvider | MetadataProvider | PluginProvider", provider),
                    wanted_set,
                )
                for provider in providers
            ],
        )
        # interleave: one folder per source per pass, preserving each source's ordering
        return [item for sublist in zip_longest(*results_per_provider) for item in sublist if item]

    async def _default_recommendations(
        self, wanted_set: set[str] | None = None
    ) -> list[RecommendationFolder]:
        """Build the default library recommendation rows from the source registry."""
        sources = self._sources
        if wanted_set is not None:
            sources = [
                s
                for s in self._sources
                if create_uri(MediaType.FOLDER, s.provider, s.item_id) in wanted_set
            ]
        folders = await asyncio.gather(
            *[source.build() for source in sources],
            return_exceptions=True,
        )
        result: list[RecommendationFolder] = []
        for source, outcome in zip(sources, folders, strict=True):
            if isinstance(outcome, Exception):
                self.logger.warning(
                    "Error building recommendation source '%s': %s",
                    source.item_id,
                    str(outcome),
                    exc_info=outcome if self.logger.isEnabledFor(logging.DEBUG) else None,
                )
            elif isinstance(outcome, BaseException):
                raise outcome
            elif outcome is not None:
                result.append(outcome)
        return result

    async def _provider_recommendations(
        self,
        provider: MusicProvider | MetadataProvider | PluginProvider,
        wanted_set: set[str] | None = None,
    ) -> list[RecommendationFolder]:
        """Return a provider's recommendations, or an empty list if it times out or raises."""
        wanted_ids: set[str] | None = None
        if wanted_set is not None:
            prefix = f"{provider.instance_id}://folder/"
            wanted_ids = {uri[len(prefix) :] for uri in wanted_set if uri.startswith(prefix)}
        try:
            async with asyncio.timeout(RECOMMENDATIONS_PROVIDER_TIMEOUT):
                # Providers may opt in to building only the wanted rows (Layer 2) by
                # declaring a `wanted` parameter; others are called unchanged and filtered here.
                call_kwargs: dict[str, set[str] | None] = {}
                if "wanted" in inspect.signature(provider.recommendations).parameters:
                    call_kwargs["wanted"] = wanted_ids
                folders = await provider.recommendations(**call_kwargs)
            if wanted_set is not None:
                folders = [f for f in folders if f.uri in wanted_set]
            return folders
        except TimeoutError:
            self.logger.warning(
                "Timeout while fetching recommendations from %s; skipping for this request",
                provider.name,
            )
            return []
        except Exception as err:
            self.logger.warning(
                "Error while fetching recommendations from %s: %s",
                provider.name,
                str(err),
                exc_info=err if self.logger.isEnabledFor(logging.DEBUG) else None,
            )
            return []
