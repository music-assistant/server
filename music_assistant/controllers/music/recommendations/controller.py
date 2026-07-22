"""Recommendations subcontroller: aggregates library + provider recommendation rows."""

from __future__ import annotations

import asyncio
import logging
from itertools import zip_longest
from typing import TYPE_CHECKING, cast

from music_assistant_models.auth import Scope
from music_assistant_models.enums import ProviderFeature
from music_assistant_models.media_items import UniqueList

from music_assistant.constants import MASS_LOGGER_NAME
from music_assistant.controllers.music.constants import (
    RECOMMENDATIONS_ITEMS_TIMEOUT,
    RECOMMENDATIONS_ROWS_TIMEOUT,
)

from .library import library_items, library_rows

if TYPE_CHECKING:
    from music_assistant_models.media_items import (
        BrowseFolder,
        ItemMapping,
        MediaItemType,
        RecommendationFolder,
    )

    from music_assistant.mass import MusicAssistant
    from music_assistant.models.metadata_provider import MetadataProvider
    from music_assistant.models.music_provider import MusicProvider
    from music_assistant.models.plugin import PluginProvider


class RecommendationsController:
    """Serves the recommendations API: default library rows plus provider rows."""

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize the controller and register its api commands."""
        self.mass = mass
        self.logger = logging.getLogger(f"{MASS_LOGGER_NAME}.music.recommendations")
        self.mass.register_api_command(
            "music/recommendations",
            self.get_recommendations,
            required_scope=Scope.LIBRARY_READ,
        )
        self.mass.register_api_command(
            "music/recommendations/items",
            self.get_recommendation_items,
            required_scope=Scope.LIBRARY_READ,
        )

    async def get_recommendations(self) -> list[RecommendationFolder]:
        """Get all available recommendation rows (library + providers, interleaved), without items."""
        providers = self.mass.music._apply_user_provider_filter(
            self.mass.get_providers_supporting_feature(ProviderFeature.RECOMMENDATIONS)
        )
        rows_per_source: list[list[RecommendationFolder]] = [
            library_rows(),
            *await asyncio.gather(
                *[
                    self._provider_rows(
                        cast("MusicProvider | MetadataProvider | PluginProvider", provider)
                    )
                    for provider in providers
                ]
            ),
        ]
        # interleave: one folder per source per pass, preserving each source's ordering
        return [item for sublist in zip_longest(*rows_per_source) for item in sublist if item]

    async def get_recommendation_items(
        self, provider: str, item_id: str
    ) -> UniqueList[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Get the items for a single recommendation row.

        :param provider: The provider instance id owning the row, or "library" for builtin rows.
        :param item_id: The item_id of the row, as returned by the recommendations listing.
        """
        try:
            if provider == "library":
                return UniqueList(await library_items(self.mass, item_id))
            prov = self.mass.get_provider(provider)
            # re-apply the user provider filter the rows listing applies, so a user
            # can not fetch items from a music provider an admin has restricted them from
            if prov is None or not self.mass.music._apply_user_provider_filter([prov]):
                return UniqueList()
            if ProviderFeature.RECOMMENDATIONS not in prov.supported_features:
                # keep the base-model guarantee that this method is only called for
                # providers declaring the feature, matching the rows listing
                return UniqueList()
            async with asyncio.timeout(RECOMMENDATIONS_ITEMS_TIMEOUT):
                return await cast(
                    "MusicProvider | MetadataProvider | PluginProvider", prov
                ).get_recommendation_items(item_id)
        except TimeoutError:
            self.logger.warning(
                "Timeout while fetching recommendation items for %s/%s; skipping",
                provider,
                item_id,
            )
            return UniqueList()
        except Exception as err:
            self.logger.warning(
                "Error while fetching recommendation items for %s/%s: %s",
                provider,
                item_id,
                str(err),
                exc_info=err if self.logger.isEnabledFor(logging.DEBUG) else None,
            )
            return UniqueList()

    async def _provider_rows(
        self, provider: MusicProvider | MetadataProvider | PluginProvider
    ) -> list[RecommendationFolder]:
        """Return a provider's recommendation rows, or an empty list if it times out or raises."""
        try:
            async with asyncio.timeout(RECOMMENDATIONS_ROWS_TIMEOUT):
                return await provider.get_recommendations()
        except TimeoutError:
            self.logger.warning(
                "Timeout while fetching recommendation rows from %s; skipping for this request",
                provider.name,
            )
            return []
        except Exception as err:
            self.logger.warning(
                "Error while fetching recommendation rows from %s: %s",
                provider.name,
                str(err),
                exc_info=err if self.logger.isEnabledFor(logging.DEBUG) else None,
            )
            return []
