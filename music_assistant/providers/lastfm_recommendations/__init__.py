"""Last.fm Recommendations music provider for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import (
    ConfigEntry,
    ConfigValueOption,
    ConfigValueType,
)
from music_assistant_models.enums import ConfigEntryType, ProviderFeature
from music_assistant_models.errors import MusicAssistantError

from music_assistant.models.metadata_provider import MetadataProvider
from music_assistant.providers.lastfm_recommendations.api_client import LastFMAPIClient
from music_assistant.providers.lastfm_recommendations.mbid_resolver import MBIDResolver
from music_assistant.providers.lastfm_recommendations.recommendations import (
    LastFMRecommendationManager,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.media_items import RecommendationFolder
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant


SUPPORTED_FEATURES = {
    ProviderFeature.RECOMMENDATIONS,
}

# Config action constants
CONF_ACTION_CLEAR_CACHE = "clear_cache"

# Curated list of popular countries for Last.fm geo charts
# Last.fm API expects full country names (not ISO codes)
# This list covers major music markets and can be expanded based on user requests
GEO_COUNTRIES = [
    "Argentina",
    "Australia",
    "Austria",
    "Belgium",
    "Brazil",
    "Canada",
    "China",
    "Czech Republic",
    "Denmark",
    "Finland",
    "France",
    "Germany",
    "Greece",
    "Hungary",
    "Iceland",
    "India",
    "Ireland",
    "Israel",
    "Italy",
    "Japan",
    "Lithuania",
    "Mexico",
    "Netherlands",
    "New Zealand",
    "Norway",
    "Philippines",
    "Poland",
    "Portugal",
    "Serbia",
    "Singapore",
    "Slovenia",
    "South Africa",
    "South Korea",
    "Spain",
    "Sweden",
    "Switzerland",
    "Thailand",
    "Turkey",
    "Ukraine",
    "United Arab Emirates",
    "United Kingdom",
    "United States",
]


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> LastFMRecommendationsProvider:
    """Initialize provider(instance) with given configuration."""
    return LastFMRecommendationsProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    if action == CONF_ACTION_CLEAR_CACHE and instance_id:
        provider = mass.get_provider(instance_id)
        if isinstance(provider, LastFMRecommendationsProvider):
            await provider.recommendations_manager.clear_cache()
            mass.create_task(provider._populate_recommendations())

    return (
        ConfigEntry(
            key="api_key",
            type=ConfigEntryType.SECURE_STRING,
            label="Last.fm API Key",
            required=True,
            description="Get your API key from https://www.last.fm/api/account/create",
            value=values.get("api_key") if values else None,
        ),
        ConfigEntry(
            key="username",
            type=ConfigEntryType.STRING,
            label="Last.fm Username",
            required=False,
            description="Your Last.fm username for genre-based recommendations (optional)",
            value=values.get("username") if values else None,
        ),
        ConfigEntry(
            key="refresh_interval",
            type=ConfigEntryType.INTEGER,
            label="Refresh Interval (hours)",
            default_value=6,
            description="How often to refresh recommendations (0 to disable automatic refresh)",
            category="Recommendations",
            range=(0, 168),  # 0 to 1 week
        ),
        ConfigEntry(
            key="enable_personalized",
            type=ConfigEntryType.BOOLEAN,
            label="Enable Personalized Recommendations",
            default_value=False,
            description=(
                "Provide 'Similar Artists' and 'Similar Tracks' rows based on your "
                "listening history"
            ),
            category="Recommendations",
        ),
        ConfigEntry(
            key="enable_global_charts",
            type=ConfigEntryType.BOOLEAN,
            label="Enable Global Charts",
            default_value=False,
            description=(
                "Provide 'Global Top Artists' and 'Global Top Tracks' rows from "
                "Last.fm's worldwide charts"
            ),
            category="Recommendations",
        ),
        ConfigEntry(
            key="enable_genre",
            type=ConfigEntryType.BOOLEAN,
            label="Enable Genre Recommendations",
            default_value=False,
            description=(
                "Provide 'Top Artists', 'Top Albums' and 'Top Tracks' rows for your "
                "most played genre (requires username)"
            ),
            category="Recommendations",
        ),
        ConfigEntry(
            key="geo_country",
            type=ConfigEntryType.STRING,
            label="Country for Geographic Charts",
            default_value="Argentina",
            description="Select country for geography-based top artists and tracks",
            options=[ConfigValueOption(country, country) for country in GEO_COUNTRIES],
            category="Recommendations",
        ),
        ConfigEntry(
            key="enable_geo",
            type=ConfigEntryType.BOOLEAN,
            label="Enable Geographic Charts",
            default_value=False,
            description=("Provide 'Top Artists' and 'Top Tracks' rows for the selected country"),
            category="Recommendations",
        ),
        ConfigEntry(
            key=CONF_ACTION_CLEAR_CACHE,
            type=ConfigEntryType.ACTION,
            label="Clear Recommendation Cache",
            description=(
                "Clear all cached recommendations. "
                "Use if a provider was removed or recommendations are stale."
            ),
            action=CONF_ACTION_CLEAR_CACHE,
            action_label="Clear Cache",
            category="Recommendations",
            advanced=True,
            required=False,
        ),
    )


class LastFMRecommendationsProvider(MetadataProvider):
    """Last.fm Recommendations Provider for Music Assistant."""

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.api = LastFMAPIClient(self)
        self.mbid_resolver = MBIDResolver(self)
        self.recommendations_manager = LastFMRecommendationManager(self)

        self._recommendation_folders: list[RecommendationFolder] = []
        self._recommendations_populated = False

        # Delay the initial populate so other providers (e.g. Spotify) finish loading first;
        # without this, resolution fails when no streaming providers are available yet.
        self.mass.call_later(
            20,
            self._populate_recommendations,
            task_id=f"lastfm_recommendations_initial_populate_{self.instance_id}",
        )

        self._schedule_refresh()

    async def _populate_recommendations(self) -> None:
        """Populate recommendation folders in the background."""
        try:
            self.logger.info("Building Last.fm recommendations")

            # Build folders incrementally so each category appears as soon as it's ready.
            personalized_folders = (
                await self.recommendations_manager._get_personalized_recommendations()
            )
            if personalized_folders:
                self._recommendation_folders.extend(personalized_folders)
                self.logger.debug(
                    "Added %d personalized recommendation folder(s)", len(personalized_folders)
                )

            global_folders = await self.recommendations_manager._get_global_recommendations()
            if global_folders:
                self._recommendation_folders.extend(global_folders)
                self.logger.debug("Added %d global recommendation folder(s)", len(global_folders))

            genre_folders = await self.recommendations_manager._get_genre_based_recommendations()
            if genre_folders:
                self._recommendation_folders.extend(genre_folders)
                self.logger.debug(
                    "Added %d genre-based recommendation folder(s)", len(genre_folders)
                )

            geo_folders = await self.recommendations_manager._get_geo_based_recommendations()
            if geo_folders:
                self._recommendation_folders.extend(geo_folders)
                self.logger.debug(
                    "Added %d geography-based recommendation folder(s)", len(geo_folders)
                )

            self._recommendations_populated = True
            self.logger.info(
                "Last.fm recommendations built (%d folders)",
                len(self._recommendation_folders),
            )
        except MusicAssistantError as err:
            self.logger.warning("Failed to populate recommendations: %s", err)

    def _schedule_refresh(self) -> None:
        """Schedule the next periodic refresh of recommendations."""
        refresh_interval_value = self.config.get_value("refresh_interval")
        if isinstance(refresh_interval_value, (int, float)):
            refresh_interval_hours = int(refresh_interval_value)
        else:
            refresh_interval_hours = 6
        if refresh_interval_hours <= 0:
            self.logger.debug("Automatic refresh disabled (interval set to 0)")
            return

        refresh_interval_seconds = float(refresh_interval_hours * 3600)

        self.mass.call_later(
            refresh_interval_seconds,
            self._refresh_recommendations,
            task_id=f"lastfm_recommendations_refresh_{self.instance_id}",
        )
        self.logger.debug(
            "Scheduled next recommendations refresh in %d hours", refresh_interval_hours
        )

    async def _refresh_recommendations(self) -> None:
        """Re-populate recommendations and reschedule the next refresh."""
        try:
            self.logger.debug("Refreshing Last.fm recommendations (scheduled)")
            await self._populate_recommendations()
        except MusicAssistantError as err:
            self.logger.warning("Failed to refresh recommendations: %s", err)
        finally:
            self._schedule_refresh()

    async def recommendations(self) -> list[RecommendationFolder]:
        """Return this provider's recommendation folders.

        On first call (before background population completes) this returns an empty list.
        Subsequent calls return progressively more populated folders.
        """
        return self._recommendation_folders
