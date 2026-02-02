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
    ProviderFeature.LIBRARY_PODCASTS_EDIT,
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
                provider=self.instance_id,
                name=episode_data.get("title", "Unknown Episode"),
                podcast=ItemMapping(
                    media_type=MediaType.PODCAST,
                    item_id=podcast_uuid,
                    provider=self.instance_id,
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
            # Add thumbnail - use episode thumbnail if available, otherwise use podcast thumbnail
            thumbnail_url = episode_data.get("thumbnail_url")
            if not thumbnail_url:
                # Fallback to podcast thumbnail
                thumbnail_url = (
                    f"https://static.pocketcasts.com/discover/images/280/{podcast_uuid}.jpg"
                )

            episode_item.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=thumbnail_url,
                        provider=self.instance_id,
                        remotely_accessible=True,
                    )
                ]
            )
            return episode_item

        except Exception as err:
            LOGGER.debug("Error converting episode: %s", err)
            return None

    async def get_library_podcasts(self) -> AsyncGenerator[Podcast, None]:
        """Get all podcasts from user's library."""
        if not self._client:
            return

        try:
            podcasts = await self._client.get_subscribed_podcasts()
            for podcast_data in podcasts:
                podcast_item = self._convert_podcast({"podcast": podcast_data})
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

        if not self._client or not self._client.session:
            msg = f"Podcast {prov_podcast_id} not found in library and client not available"
            raise MediaNotFoundError(msg)

        try:
            # This endpoint returns a 302 redirect to the static JSON with timestamp
            async with self._client.session.get(
                f"https://podcast-api.pocketcasts.com/podcast/full/{prov_podcast_id}"
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    return self._convert_podcast(data)
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

    async def _get_special_folder_episodes(
        self, folder_name: str
    ) -> list[MediaItemType | BrowseFolder]:
        """Get episodes for a special browse folder.

        :param folder_name: Name of the special folder (up_next, new_releases, etc.)
        """
        if not self._client:
            return []

        # Get episodes from the appropriate API endpoint
        if folder_name == "up_next":
            episode_list = await self._client.get_up_next_episodes()
        elif folder_name == "new_releases":
            episode_list = await self._client.get_new_releases()
        elif folder_name == "in_progress":
            episode_list = await self._client.get_in_progress_episodes()
        elif folder_name == "starred":
            episode_list = await self._client.get_starred_episodes()
        else:  # history
            episode_list = await self._client.get_history()

        LOGGER.debug("Got %d episodes from %s API", len(episode_list), folder_name)

        items: list[MediaItemType | BrowseFolder] = []
        # Convert episodes to PodcastEpisode objects
        # Note: up_next returns dict with UUIDs as keys, others return list
        if isinstance(episode_list, dict):
            # Up Next format: {episode_uuid: {episode_data}}
            episode_items: list[tuple[str | None, dict[str, Any]]] = list(episode_list.items())
        else:
            # Other formats: [{episode_data}, ...]
            episode_items = [(None, ep) for ep in episode_list]

        for episode_uuid_key, episode_data in episode_items:
            # For up_next, add the UUID from the dict key to the episode data
            if episode_uuid_key and "uuid" not in episode_data:
                episode_data["uuid"] = episode_uuid_key

            # Extract podcast UUID from episode data
            # Note: up_next endpoint returns podcast as string, others return object
            podcast_field = episode_data.get("podcast")
            podcast_uuid: str | None = None
            if isinstance(podcast_field, str):
                podcast_uuid = podcast_field
            elif isinstance(podcast_field, dict):
                podcast_uuid = podcast_field.get("uuid", None)
            else:
                podcast_uuid = episode_data.get("podcastUuid", None)

            if podcast_uuid:
                episode_item = self._convert_episode(episode_data, podcast_uuid)
                if episode_item:
                    items.append(episode_item)

        LOGGER.debug("Converted %d episodes successfully from %s", len(items), folder_name)
        return items

    def _create_browse_folders(self) -> list[BrowseFolder]:
        """Create special browse folders for root level."""
        return [
            BrowseFolder(
                item_id="up_next",
                provider=self.instance_id,
                path=f"{self.instance_id}://up_next",
                name="Up Next",
                image=MediaItemImage(
                    type=ImageType.THUMB,
                    path="https://static.pocketcasts.com/discover/images/280/default.jpg",
                    provider=self.instance_id,
                ),
            ),
            BrowseFolder(
                item_id="new_releases",
                provider=self.instance_id,
                path=f"{self.instance_id}://new_releases",
                name="New Releases",
                image=MediaItemImage(
                    type=ImageType.THUMB,
                    path="https://static.pocketcasts.com/discover/images/280/default.jpg",
                    provider=self.instance_id,
                ),
            ),
            BrowseFolder(
                item_id="in_progress",
                provider=self.instance_id,
                path=f"{self.instance_id}://in_progress",
                name="In Progress",
                image=MediaItemImage(
                    type=ImageType.THUMB,
                    path="https://static.pocketcasts.com/discover/images/280/default.jpg",
                    provider=self.instance_id,
                ),
            ),
            BrowseFolder(
                item_id="starred",
                provider=self.instance_id,
                path=f"{self.instance_id}://starred",
                name="Starred",
                image=MediaItemImage(
                    type=ImageType.THUMB,
                    path="https://static.pocketcasts.com/discover/images/280/default.jpg",
                    provider=self.instance_id,
                ),
            ),
            BrowseFolder(
                item_id="history",
                provider=self.instance_id,
                path=f"{self.instance_id}://history",
                name="History",
                image=MediaItemImage(
                    type=ImageType.THUMB,
                    path="https://static.pocketcasts.com/discover/images/280/default.jpg",
                    provider=self.instance_id,
                ),
            ),
        ]

    async def browse(self, path: str) -> list[MediaItemType | BrowseFolder]:
        """Browse this provider's items."""
        if not self._client:
            return []

        # Parse the path
        item_path = path.split("://", 1)[1] if "://" in path else path
        LOGGER.debug("Browse called with path: %s, parsed to: %s", path, item_path)

        items: list[MediaItemType | BrowseFolder] = []

        if not item_path:
            # Root level - show special folders and subscribed podcasts
            try:
                # Add special browse folders at the top
                items.extend(self._create_browse_folders())

                # Add subscribed podcasts
                podcasts = await self._client.get_subscribed_podcasts()
                for podcast_data in podcasts:
                    podcast_item = self._convert_podcast(podcast_data)
                    if podcast_item:
                        items.append(podcast_item)
                LOGGER.debug(
                    "Returning %d items at root level (5 folders + %d podcasts)",
                    len(items),
                    len(items) - 5,
                )
                return items
            except Exception as err:
                LOGGER.exception("Error browsing podcasts: %s", err)
                return []

        elif item_path in ("up_next", "new_releases", "in_progress", "starred", "history"):
            # Special folder - show episodes from appropriate API endpoint
            LOGGER.debug("Fetching episodes for special folder: %s", item_path)
            try:
                return await self._get_special_folder_episodes(item_path)
            except Exception as err:
                LOGGER.exception("Error browsing special folder %s: %s", item_path, err)
                return []
        else:
            # Regular podcast path - show episodes for the podcast
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
                    can_seek=True,
                    allow_seek=True,
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

    async def get_podcast_episode(self, prov_item_id: str) -> PodcastEpisode:
        """Get full podcast episode details by id."""
        if not self._client:
            msg = "Client not available"
            raise MediaNotFoundError(msg)

        # prov_item_id format is "podcast_uuid:episode_uuid"
        podcast_uuid, episode_uuid = prov_item_id.split(":", 1)

        LOGGER.debug("Getting episode %s from podcast %s", episode_uuid, podcast_uuid)

        # Fetch all episodes for the podcast
        episodes = await self._client.get_podcast_episodes(podcast_uuid)

        # Find the specific episode
        for episode_data in episodes:
            if episode_data["uuid"] == episode_uuid:
                episode_item = self._convert_episode(episode_data, podcast_uuid)
                if not episode_item:
                    msg = f"Failed to convert episode {episode_uuid}"
                    raise MediaNotFoundError(msg)

                # Get playback position from in-progress list
                in_progress = await self._client.get_in_progress_episodes()
                for ep in in_progress:
                    if ep.get("uuid") == episode_uuid:
                        played_up_to = ep.get("playedUpTo", 0)  # seconds
                        duration = ep.get("duration", 0)  # seconds

                        # Set resume position in milliseconds
                        episode_item.resume_position_ms = played_up_to * 1000

                        # Consider played if > 90% complete
                        if duration > 0:
                            episode_item.fully_played = (played_up_to / duration) > 0.9

                        LOGGER.debug(
                            "Episode %s resume position: %d ms (%.1f%%)",
                            episode_uuid,
                            episode_item.resume_position_ms,
                            (played_up_to / duration * 100) if duration > 0 else 0,
                        )
                        break

                return episode_item

        msg = f"Episode {episode_uuid} not found in podcast {podcast_uuid}"
        raise MediaNotFoundError(msg)

    async def get_resume_position(self, item_id: str, media_type: MediaType) -> tuple[bool, int]:
        """Return the resume position for a podcast episode.

        :param item_id: The episode item ID (format: podcast_uuid:episode_uuid).
        :param media_type: The media type (should be PODCAST_EPISODE).

        Returns: (fully_played, position_milliseconds)
        """
        LOGGER.debug("Getting resume position for episode: %s", item_id)

        if not self._client:
            return (False, 0)

        try:
            # item_id format is "podcast_uuid:episode_uuid"
            if ":" not in item_id:
                return (False, 0)

            _, episode_uuid = item_id.split(":", 1)

            # Get in-progress episodes
            in_progress = await self._client.get_in_progress_episodes()

            for ep in in_progress:
                if ep.get("uuid") == episode_uuid:
                    played_up_to = int(ep.get("playedUpTo", 0))  # seconds from API
                    duration = int(ep.get("duration", 0))

                    # Consider fully played if > 90%
                    fully_played = duration > 0 and (played_up_to / duration) > 0.9

                    LOGGER.debug(
                        "Resume position for %s: %d seconds / %d ms (fully_played=%s)",
                        episode_uuid,
                        played_up_to,
                        played_up_to * 1000,
                        fully_played,
                    )
                    # Return position in milliseconds as expected by Music Assistant
                    return (fully_played, played_up_to * 1000)

            # Not in progress list
            return (False, 0)

        except Exception as err:
            LOGGER.error("Error getting resume position: %s", err)
            return (False, 0)
