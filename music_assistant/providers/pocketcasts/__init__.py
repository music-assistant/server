"""Pocket Casts music provider for Music Assistant - with custom API client."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import LoginFailed, MediaNotFoundError
from music_assistant_models.media_items import (
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    MediaItemImage,
    MediaItemMetadata,
    MediaItemType,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    SearchResults,
    UniqueList,
)
from music_assistant_models.provider import ProviderManifest
from music_assistant_models.streamdetails import StreamDetails

from music_assistant import MusicAssistant
from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.models import ProviderInstanceType

# Import our custom API client
from .api_client import LoginError, PocketCastsClient

LOGGER = logging.getLogger(__name__)

SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_PODCASTS,
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return PocketCastsProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    return (
        ConfigEntry(
            key=CONF_USERNAME,
            type=ConfigEntryType.STRING,
            label="Email",
            description="Your Pocket Casts email address",
            required=True,
        ),
        ConfigEntry(
            key=CONF_PASSWORD,
            type=ConfigEntryType.SECURE_STRING,
            label="Password",
            description="Your Pocket Casts password",
            required=True,
        ),
    )


class PocketCastsProvider(MusicProvider):
    """Provider for Pocket Casts podcast service."""

    _client: PocketCastsClient | None = None

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        email = str(self.config.get_value(CONF_USERNAME))
        password = str(self.config.get_value(CONF_PASSWORD))

        if not email or not password:
            msg = "Email and password are required for Pocket Casts"
            raise LoginFailed(msg)

        try:
            LOGGER.info("Initializing Pocket Casts provider")
            self._client = PocketCastsClient()
            await self._client.login(email, password)

            # Test basic functionality
            podcasts = await self._client.get_subscribed_podcasts()
            LOGGER.info("Successfully initialized with %d podcasts", len(podcasts))

        except LoginError as err:
            LOGGER.error("Failed to login: %s", err)
            raise LoginFailed(f"Failed to login to Pocket Casts: {err}") from err
        except Exception as err:
            LOGGER.exception("Failed to initialize: %s", err)
            raise LoginFailed(f"Failed to initialize Pocket Casts: {err}") from err

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        if self._client and self._client.session:
            await self._client.session.close()

    def _convert_podcast(self, podcast_data: dict[str, Any]) -> Podcast:
        """Convert API podcast data to Podcast object."""
        # Extract podcast metadata from nested structure
        podcast_info = podcast_data.get("podcast", podcast_data)  # Handle both structures

        return Podcast(
            item_id=podcast_info["uuid"],
            provider=self.domain,
            name=podcast_info.get("title", ""),
            provider_mappings={
                ProviderMapping(
                    item_id=podcast_info["uuid"],
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
            metadata=MediaItemMetadata(
                description=podcast_info.get("description"),
                images=UniqueList(
                    [
                        MediaItemImage(
                            type=ImageType.THUMB,
                            path=f"https://static.pocketcasts.com/discover/images/280/{podcast_info['uuid']}.jpg",
                            provider=self.instance_id,
                        )
                    ]
                ),
            ),
        )

    def _convert_episode(
        self, episode_data: dict[str, Any], podcast_uuid: str
    ) -> PodcastEpisode | None:
        """Convert Pocket Casts episode data to MA PodcastEpisode object."""
        try:
            episode_uuid = episode_data.get("uuid")
            if not episode_uuid:
                return None

            # Create composite ID: podcast_uuid:episode_uuid
            item_id = f"{podcast_uuid}:{episode_uuid}"

            episode_item = PodcastEpisode(
                item_id=item_id,
                provider=self.lookup_key,
                name=episode_data.get("title", "Unknown Episode"),
                podcast=ItemMapping(
                    media_type=MediaType.PODCAST,
                    item_id=podcast_uuid,
                    provider=self.lookup_key,
                    name="",
                ),
                position=episode_data.get("episode_number", 0),
                provider_mappings={
                    ProviderMapping(
                        item_id=item_id,
                        provider_domain=self.domain,
                        provider_instance=self.instance_id,
                        audio_format=AudioFormat(
                            content_type=ContentType.try_parse(
                                episode_data.get("file_type", "audio/mpeg")
                            ),
                        ),
                        url=episode_data.get("url", ""),
                    )
                },
            )

            # Add duration
            if episode_data.get("duration"):
                episode_item.duration = int(episode_data["duration"])

            # Add metadata
            if episode_data.get("title"):
                episode_item.metadata.label = episode_data["title"]
            if episode_data.get("show_notes"):
                episode_item.metadata.description = episode_data["show_notes"]
            if episode_data.get("thumbnail_url"):
                episode_item.metadata.images = UniqueList(
                    [
                        MediaItemImage(
                            type=ImageType.THUMB,
                            path=episode_data["thumbnail_url"],
                            provider=self.lookup_key,
                            remotely_accessible=True,
                        )
                    ]
                )
            return episode_item

        except Exception as err:
            LOGGER.debug("Error converting episode: %s", err)
            return None

    async def get_library_podcasts(self) -> AsyncGenerator[Podcast, None]:
        """Retrieve library podcasts from Pocket Casts."""
        if not self._client:
            return

        try:
            podcasts = await self._client.get_subscribed_podcasts()

            for podcast_data in podcasts:
                podcast_item = self._convert_podcast(podcast_data)
                if podcast_item:
                    yield podcast_item

        except Exception as err:
            LOGGER.error("Error getting library podcasts: %s", err)

    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Get full podcast details."""
        # First try library
        async for podcast in self.get_library_podcasts():
            if podcast.item_id == prov_podcast_id:
                return podcast

        # Not in library - fetch from podcast-api which redirects to static JSON
        LOGGER.debug("Podcast not in library, fetching from API: %s", prov_podcast_id)

        if not self._client:
            raise MediaNotFoundError("API client not initialized")

        try:
            # This endpoint returns a 302 redirect to the static JSON with timestamp
            async with self._client.session.get(
                f"https://podcast-api.pocketcasts.com/podcast/full/{prov_podcast_id}"
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    return self._convert_podcast(data)
                else:
                    raise MediaNotFoundError(
                        f"Podcast {prov_podcast_id} not found (status {response.status})"
                    )

        except Exception as err:
            LOGGER.error("Error fetching podcast %s: %s", prov_podcast_id, err)
            raise MediaNotFoundError(
                f"podcast://{prov_podcast_id} not found on provider {self.domain}"
            )

    async def get_podcast_episodes(
        self, prov_podcast_id: str
    ) -> AsyncGenerator[PodcastEpisode, None]:
        """Get all episodes for a podcast."""
        if not self._client:
            return

        try:
            episodes = await self._client.get_podcast_episodes(prov_podcast_id)
            LOGGER.debug(
                "Got %d episodes from client for podcast %s", len(episodes), prov_podcast_id
            )

            for episode_data in episodes:
                episode_item = self._convert_episode(episode_data, prov_podcast_id)
                if episode_item:
                    yield episode_item

        except Exception as err:
            LOGGER.error("Error getting episodes for podcast %s: %s", prov_podcast_id, err)

    async def browse(self, path: str) -> list[MediaItemType | BrowseFolder]:
        """Browse this provider's items."""
        if not self._client:
            return []

        # Parse the path
        item_path = path.split("://", 1)[1] if "://" in path else path
        LOGGER.debug("Browse called with path: %s, parsed to: %s", path, item_path)

        items: list[MediaItemType | BrowseFolder] = []
        if not item_path:
            # Root level - show subscribed podcasts
            try:
                podcasts = await self._client.get_subscribed_podcasts()
                for podcast_data in podcasts:
                    podcast_item = self._convert_podcast(podcast_data)
                    if podcast_item:
                        items.append(podcast_item)
                LOGGER.debug("Returning %d podcasts at root level", len(items))
                return items
            except Exception as err:
                LOGGER.exception("Error browsing podcasts: %s", err)
                return []
        else:
            # Sub-path - show episodes for the podcast
            LOGGER.debug("Fetching episodes for podcast: %s", item_path)
            try:
                episodes = await self._client.get_podcast_episodes(item_path)
                LOGGER.debug("Got %d episodes from API", len(episodes))
                for episode_data in episodes:
                    episode_item = self._convert_episode(episode_data, item_path)
                    if episode_item:
                        items.append(episode_item)
                LOGGER.debug("Converted %d episodes successfully", len(items))
                return items
            except Exception as err:
                LOGGER.exception("Error browsing episodes for %s: %s", item_path, err)
                return []

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get streamable URL and details for the given media item."""
        # Parse composite ID
        if ":" not in item_id:
            raise MediaNotFoundError(f"Invalid episode ID format: {item_id}")

        podcast_uuid, episode_uuid = item_id.split(":", 1)

        if not self._client:
            raise MediaNotFoundError("Client not initialized")
        # Get episode data
        episodes = await self._client.get_podcast_episodes(podcast_uuid)
        for episode_data in episodes:
            if episode_data.get("uuid") == episode_uuid:
                url = episode_data.get("url", "")
                if not url:
                    raise MediaNotFoundError(f"No URL found for episode {item_id}")

                return StreamDetails(
                    item_id=item_id,
                    provider=self.instance_id,
                    audio_format=AudioFormat(
                        content_type=ContentType.try_parse(
                            episode_data.get("file_type", "audio/mpeg")
                        ),
                    ),
                    stream_type=StreamType.HTTP,
                    path=url,
                )

        raise MediaNotFoundError(f"Episode {item_id} not found")

    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 5
    ) -> SearchResults:
        """Search for podcasts."""
        results = SearchResults()

        if not self._client:
            return results

        if not media_types or MediaType.PODCAST in media_types:
            try:
                podcasts = await self._client.search_podcasts(search_query)

                podcast_results = []
                for podcast_data in podcasts[:limit]:
                    podcast_item = self._convert_podcast(podcast_data)
                    if podcast_item:
                        podcast_results.append(podcast_item)

                results.podcasts = podcast_results

            except Exception as err:
                LOGGER.debug("Error searching Pocket Casts: %s", err)

        return results
