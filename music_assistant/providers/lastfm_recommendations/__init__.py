"""Last.fm Recommendations music provider for Music Assistant."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from music_assistant_models.background_task import TaskSchedule
from music_assistant_models.config_entries import (
    ConfigActionResult,
    ConfigEntry,
    ConfigValueOption,
)
from music_assistant_models.enums import ConfigEntryType, ExternalID, ProviderFeature
from music_assistant_models.errors import (
    AuthenticationFailed,
    InvalidToken,
    MusicAssistantError,
    ResourceTemporarilyUnavailable,
)
from music_assistant_models.media_items import RecommendationFolder, Track, UniqueList

from music_assistant.constants import CONF_USERNAME
from music_assistant.controllers.cache import use_cache
from music_assistant.models.metadata_provider import MetadataProvider
from music_assistant.providers.lastfm_recommendations.api_client import LastFMAPIClient
from music_assistant.providers.lastfm_recommendations.constants import (
    CACHE_CATEGORY_RESOLVED_ITEMS,
    CACHE_EXPIRATION_SECONDS,
    CONF_ACTION_CLEAR_CACHE,
    CONF_API_KEY,
    CONF_ENABLE_GENRE,
    CONF_ENABLE_GEO,
    CONF_ENABLE_GLOBAL_CHARTS,
    CONF_ENABLE_PERSONALIZED,
    CONF_GEO_COUNTRY,
    GEO_COUNTRIES,
    REFRESH_TASK_ID,
)
from music_assistant.providers.lastfm_recommendations.recommendations import (
    LastFMRecommendationManager,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.media_items import (
        Artist,
        BrowseFolder,
        ItemMapping,
        MediaItemType,
    )
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant


SUPPORTED_FEATURES = {
    ProviderFeature.RECOMMENDATIONS,
    ProviderFeature.SIMILAR_ARTISTS,
    ProviderFeature.SIMILAR_TRACKS,
    ProviderFeature.ARTIST_TOPTRACKS,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> LastFMRecommendationsProvider:
    """Initialize provider(instance) with given configuration."""
    return LastFMRecommendationsProvider(mass, manifest, config, SUPPORTED_FEATURES)


class LastFMRecommendationsProvider(MetadataProvider):
    """Last.fm Recommendations Provider for Music Assistant."""

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider."""
        return (
            ConfigEntry(
                key=CONF_API_KEY,
                type=ConfigEntryType.SECURE_STRING,
                required=False,
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_USERNAME,
                type=ConfigEntryType.STRING,
                required=False,
            ),
            ConfigEntry(
                key=CONF_ENABLE_PERSONALIZED,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
                category="recommendations",
            ),
            ConfigEntry(
                key=CONF_ENABLE_GLOBAL_CHARTS,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
                category="recommendations",
            ),
            ConfigEntry(
                key=CONF_ENABLE_GENRE,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
                category="recommendations",
            ),
            ConfigEntry(
                key=CONF_ENABLE_GEO,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
                category="recommendations",
            ),
            ConfigEntry(
                key=CONF_GEO_COUNTRY,
                type=ConfigEntryType.STRING,
                default_value="Argentina",
                options=[ConfigValueOption(country, title=country) for country in GEO_COUNTRIES],
                category="recommendations",
            ),
            ConfigEntry(
                key=CONF_ACTION_CLEAR_CACHE,
                type=ConfigEntryType.ACTION,
                action=CONF_ACTION_CLEAR_CACHE,
                category="recommendations",
                advanced=True,
                required=False,
            ),
        )

    async def handle_config_action(
        self, action: str
    ) -> tuple[ConfigEntry, ...] | ConfigActionResult | None:
        """Handle a one-shot config action button press."""
        if action == CONF_ACTION_CLEAR_CACHE:
            await self.recommendations_manager.clear_cache()
            self.mass.create_task(self._refresh_recommendations())
            return None
        return await super().handle_config_action(action)

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.api = LastFMAPIClient(self)
        self.recommendations_manager = LastFMRecommendationManager(self)

        self._recommendation_folders: list[RecommendationFolder] = []

        # Register recurring refresh task (runs every 6 hours).
        self.mass.tasks.register_scheduled_task(
            task_id=f"{REFRESH_TASK_ID}_{self.instance_id}",
            name="Refresh Last.fm recommendations",
            handler=self._refresh_recommendations,
            schedule=TaskSchedule.hourly(every=6),
            translation_key="refresh_lastfm_recommendations",
            translation_owner=self.translation_owner,
        )

        # Populate on every startup so the UI isn't empty until the next scheduled refresh.
        # Delayed 20s to let streaming providers finish loading first.
        self.mass.call_later(
            20,
            self._refresh_recommendations,
            task_id=f"{REFRESH_TASK_ID}_initial_{self.instance_id}",
        )

    async def unload(self, is_removed: bool = False) -> None:
        """Unload the provider."""
        self.mass.tasks.unregister_scheduled_task(
            f"{REFRESH_TASK_ID}_{self.instance_id}",
            clear_persisted_state=is_removed,
        )

    async def get_recommendations(self) -> list[RecommendationFolder]:
        """Get this provider's available recommendation rows, without items."""
        # rows come from the precomputed in-memory folders: no backend I/O
        return [
            RecommendationFolder(
                item_id=folder.item_id,
                provider=folder.provider,
                name=folder.name,
                translation_key=folder.translation_key,
                translation_params=folder.translation_params,
                icon=folder.icon,
                subtitle=folder.subtitle,
            )
            for folder in self._recommendation_folders
        ]

    async def get_recommendation_items(
        self, item_id: str
    ) -> UniqueList[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Get the items for a single recommendation row.

        :param item_id: The item_id of the row, as returned by get_recommendations.
        """
        for folder in self._recommendation_folders:
            if folder.item_id == item_id:
                return folder.items
        return UniqueList()

    async def _refresh_recommendations(self) -> None:
        """Rebuild recommendation folders."""
        # Build into a local list and swap it in atomically at the end, so a slow,
        # rate-limited rebuild keeps serving the previous generation's rows instead of
        # returning empty for folders that haven't been rebuilt yet.
        new_folders: list[RecommendationFolder] = []

        try:
            self.logger.info("Building Last.fm recommendations")
            async for folder in self.recommendations_manager.build_recommendation_folders():
                new_folders.append(folder)
            self._recommendation_folders = new_folders
            self.logger.info(
                "Last.fm recommendations built (%d folders)",
                len(self._recommendation_folders),
            )
        except (AuthenticationFailed, InvalidToken) as err:
            self.logger.error(
                "Last.fm authentication failed — check your API key in the provider settings: %s",
                err,
            )
        except ResourceTemporarilyUnavailable as err:
            self.logger.warning("Last.fm rate-limited the refresh, will retry later: %s", err)
        except MusicAssistantError as err:
            self.logger.warning("Failed to build recommendations: %s", err)

    async def get_similar_artists(self, artist: Artist, limit: int = 25) -> list[Artist]:
        """
        Retrieve similar artists from Last.fm.

        :param artist: The reference artist.
        :param limit: Maximum number of similar artists to return.
        """
        artist_mbid = artist.get_external_id(ExternalID.MB_ARTIST)
        similar_raw = await self.api.get_similar_artists(artist.name, artist_mbid, limit)
        if not similar_raw:
            return []

        resolved = await asyncio.gather(
            *[self.recommendations_manager.get_or_resolve_artist(raw) for raw in similar_raw]
        )
        return [a for a in resolved if a is not None]

    async def get_similar_tracks(self, track: Track, limit: int = 25) -> list[Track]:
        """
        Retrieve similar tracks from Last.fm.

        :param track: The reference track.
        :param limit: Maximum number of similar tracks to return.
        """
        artist_name = track.artists[0].name if track.artists else "Unknown Artist"
        track_mbid = track.get_external_id(ExternalID.MB_RECORDING)
        similar_raw = await self.api.get_similar_tracks(artist_name, track.name, track_mbid, limit)
        if not similar_raw:
            return []

        resolved = await asyncio.gather(
            *[self.recommendations_manager.get_or_resolve_track(raw) for raw in similar_raw]
        )
        return [t for t in resolved if t is not None]

    async def get_artist_toptracks(self, artist: Artist, limit: int = 25) -> list[Track]:
        """
        Retrieve an artist's top tracks from Last.fm.

        :param artist: The reference artist.
        :param limit: Maximum number of top tracks to return.
        """
        artist_mbid = artist.get_external_id(ExternalID.MB_ARTIST)
        return await self._get_artist_toptracks(artist.name, artist_mbid, limit)

    @use_cache(
        CACHE_EXPIRATION_SECONDS,
        category=CACHE_CATEGORY_RESOLVED_ITEMS,
        allow_expired_cache=True,
    )
    async def _get_artist_toptracks(
        self, artist_name: str, artist_mbid: str | None, limit: int
    ) -> list[Track]:
        """Fetch and resolve an artist's top tracks, keyed by name/mbid for caching."""
        top_raw = await self.api.get_artist_top_tracks(artist_name, artist_mbid, limit)
        if not top_raw:
            return []

        # Tolerate individual resolution failures (e.g. a rate-limited lookup) so one bad
        # track can't sink the whole listing.
        resolved = await asyncio.gather(
            *[self.recommendations_manager.get_or_resolve_track(raw) for raw in top_raw],
            return_exceptions=True,
        )
        tracks = [t for t in resolved if isinstance(t, Track)]
        self.logger.debug(
            "Resolved %d/%d top tracks to playable items for '%s'",
            len(tracks),
            len(top_raw),
            artist_name,
        )
        return tracks
