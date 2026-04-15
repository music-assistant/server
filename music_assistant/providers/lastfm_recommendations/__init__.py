"""Last.fm Recommendations music provider for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.background_task import TaskSchedule
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

CONF_ACTION_CLEAR_CACHE = "clear_cache"
REFRESH_TASK_ID = "lastfm_recommendations_refresh"

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
            mass.create_task(provider._refresh_recommendations())

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
            label="Refresh Recommendations",
            description=(
                "Rebuild recommendations immediately instead of waiting for the next "
                "scheduled refresh."
            ),
            action=CONF_ACTION_CLEAR_CACHE,
            action_label="Refresh Now",
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

        # Register recurring refresh task (default: every 6 hours).
        # Initial delay of 20s allows streaming providers to finish loading first.
        self.mass.tasks.register_scheduled_task(
            task_id=f"{REFRESH_TASK_ID}_{self.instance_id}",
            name="Refresh Last.fm recommendations",
            handler=self._refresh_recommendations,
            schedule=TaskSchedule.hourly(every=6),
            initial_delay=20,
            translation_key="background_task.refresh_lastfm_recommendations",
        )

    async def unload(self, is_removed: bool = False) -> None:
        """Unload the provider."""
        self.mass.tasks.unregister_scheduled_task(
            f"{REFRESH_TASK_ID}_{self.instance_id}",
            clear_persisted_state=is_removed,
        )

    async def _refresh_recommendations(self) -> None:
        """Rebuild recommendation folders."""
        self._recommendation_folders.clear()
        self._recommendations_populated = False

        try:
            self.logger.info("Building Last.fm recommendations")
            folders = await self.recommendations_manager.build_recommendation_folders()
            self._recommendation_folders.extend(folders)
            self._recommendations_populated = True
            self.logger.info(
                "Last.fm recommendations built (%d folders)",
                len(self._recommendation_folders),
            )
        except MusicAssistantError as err:
            self.logger.warning("Failed to build recommendations: %s", err)

    async def recommendations(self) -> list[RecommendationFolder]:
        """Return this provider's recommendation folders.

        On first call (before background population completes) this returns an empty list.
        Subsequent calls return progressively more populated folders.
        """
        return self._recommendation_folders
