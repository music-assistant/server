"""Recommendations subcontroller: aggregates library + provider recommendation rows."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from itertools import zip_longest
from typing import TYPE_CHECKING, cast

from music_assistant_models.auth import Scope
from music_assistant_models.enums import ProviderFeature
from music_assistant_models.media_items import BrowseFolder, UniqueList

from music_assistant.constants import MASS_LOGGER_NAME
from music_assistant.controllers.music.constants import (
    RECOMMENDATIONS_BROWSE_LIMIT,
    RECOMMENDATIONS_BROWSE_SCHEME,
    RECOMMENDATIONS_ITEMS_TIMEOUT,
    RECOMMENDATIONS_ROWS_TIMEOUT,
)
from music_assistant.controllers.webserver.helpers.auth_middleware import get_current_user
from music_assistant.helpers.provider_access import visible_items
from music_assistant.providers.recommendations import LibraryRecommendationsProvider

if TYPE_CHECKING:
    from music_assistant_models.media_items import (
        ItemMapping,
        MediaItemType,
        RecommendationFolder,
    )

    from music_assistant.mass import MusicAssistant
    from music_assistant.models.media_capabilities import RecommendationsMixin


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
            allow_impersonation=True,
        )
        self.mass.register_api_command(
            "music/recommendations/items",
            self.get_recommendation_items,
            required_scope=Scope.LIBRARY_READ,
            allow_impersonation=True,
        )

    async def get_recommendations(self) -> list[RecommendationFolder]:
        """Get all available recommendation rows (library + providers, interleaved), without items."""
        providers = self.mass.music._apply_user_provider_filter(
            self.mass.get_providers_supporting_feature(ProviderFeature.RECOMMENDATIONS)
        )
        rows_per_source: list[list[RecommendationFolder]] = [
            *await asyncio.gather(
                *[
                    self._provider_rows(cast("RecommendationsMixin", provider))
                    for provider in providers
                ]
            ),
        ]
        # interleave: one folder per source per pass, preserving each source's ordering
        return [item for sublist in zip_longest(*rows_per_source) for item in sublist if item]

    async def get_recommendation_items(
        self, provider: str, item_id: str, providers: list[str] | None = None
    ) -> UniqueList[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Get the items for a single recommendation row.

        :param provider: The provider instance id owning the row.
        :param item_id: The item_id of the row, as returned by the recommendations listing.
        :param providers: Restrict items to those reachable through one of these provider
            instance ids (OR semantics). Only honored on rows that advertise
            `supports_provider_filter`; ignored on other rows for backwards compatibility.
        """
        return await self._get_row_items(provider, item_id, providers)

    async def browse(
        self, sub_path: str, random_seed: int | None = None
    ) -> list[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Browse the recommendation rows under the discover:// path.

        :param sub_path: The path after discover://: empty for the providers owning rows,
            "<provider>" for the rows of that provider, "<provider>/<item_id>" for all
            items of that row.
        :param random_seed: Keep the order of a random row stable across pages.
        """
        provider, _, item_id = sub_path.partition("/")
        if item_id:
            # the browse view pages through the row, so a random row needs a seed even
            # when the caller does not ask for one: its own identity keeps it stable
            seed = random_seed if random_seed is not None else _row_seed(provider, item_id)
            return list(
                await self._get_row_items(
                    provider,
                    item_id,
                    limit=RECOMMENDATIONS_BROWSE_LIMIT,
                    random_seed=seed,
                )
            )
        rows = await self.get_recommendations()
        if provider:
            return [
                BrowseFolder(
                    item_id=row.item_id,
                    provider=row.provider,
                    path=f"{RECOMMENDATIONS_BROWSE_SCHEME}://{row.provider}/{row.item_id}",
                    name=row.name,
                    translation_key=row.translation_key,
                )
                for row in rows
                if row.provider == provider
            ]
        folders: list[MediaItemType | ItemMapping | BrowseFolder] = []
        for row_provider in dict.fromkeys(row.provider for row in rows):
            if not (prov := self.mass.get_provider(row_provider)):
                continue
            folders.append(
                BrowseFolder(
                    item_id=row_provider,
                    provider=prov.domain,
                    path=f"{RECOMMENDATIONS_BROWSE_SCHEME}://{row_provider}",
                    name=prov.name,
                )
            )
        return folders

    async def _get_row_items(
        self,
        provider: str,
        item_id: str,
        providers: list[str] | None = None,
        limit: int | None = None,
        random_seed: int | None = None,
    ) -> UniqueList[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Get the items for a single recommendation row, empty when it can not be fetched.

        :param provider: The provider instance id owning the row.
        :param item_id: The item_id of the row.
        :param providers: Restrict items to those reachable through one of these provider
            instance ids, on rows that support it.
        :param limit: The maximum number of items a library row returns, None for its default.
        :param random_seed: Keep the order of a random row stable across pages.
        """
        # the rows listing leaves out the music sources the user may not see, and only lists
        # providers declaring the feature; a row of any other provider does not exist, nor
        # does another account of the same service stand in for the rows of a hidden one
        # (a row naming its service by domain is served by a visible account of it)
        prov = self.mass.music.get_visible_provider(provider)
        if (
            prov is None
            or provider not in (prov.instance_id, prov.domain)
            or ProviderFeature.RECOMMENDATIONS not in prov.supported_features
        ):
            return UniqueList()
        try:
            async with asyncio.timeout(RECOMMENDATIONS_ITEMS_TIMEOUT):
                if isinstance(prov, LibraryRecommendationsProvider):
                    if limit is None:
                        return await prov.get_recommendation_items(item_id, providers=providers)
                    return await prov.get_recommendation_items(
                        item_id, providers=providers, limit=limit, random_seed=random_seed
                    )
                # external provider rows don't support provider filtering, a limit or a
                # seed: their SPI signature is unchanged, so those are ignored here
                items = await cast("RecommendationsMixin", prov).get_recommendation_items(item_id)
            # a plugin or metadata row can list items on any music source
            return UniqueList(visible_items(self.mass, items, get_current_user()))
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

    async def _provider_rows(self, provider: RecommendationsMixin) -> list[RecommendationFolder]:
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


def _row_seed(provider: str, item_id: str) -> int:
    """
    Return the seed that keeps a row's random order stable across pages.

    :param provider: The provider instance id owning the row.
    :param item_id: The item_id of the row.
    """
    digest = hashlib.blake2s(f"{provider}/{item_id}".encode(), digest_size=4).digest()
    return int.from_bytes(digest, "big")
