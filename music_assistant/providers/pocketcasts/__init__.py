"""
Pocketcasts Music Provider for Music Assistant.

Provides access to podcasts from a Pocket Casts account.
"""

from __future__ import annotations

from contextlib import suppress
from datetime import datetime
from typing import TYPE_CHECKING, Any

from music_assistant_models.config_entries import ConfigEntry
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
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
from music_assistant.helpers.util import lock
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


# Pocket Casts API endpoints
POCKETCASTS_API_BASE = "https://api.pocketcasts.com"
POCKETCASTS_LOGIN_URL = f"{POCKETCASTS_API_BASE}/user/login"
POCKETCASTS_SUBSCRIPTION_STATUS_URL = f"{POCKETCASTS_API_BASE}/subscription/status"
POCKETCASTS_PODCAST_LIST_URL = f"{POCKETCASTS_API_BASE}/user/podcast/list"

# Podcast episodes API (separate subdomain, uses redirect)
POCKETCASTS_PODCAST_FULL_URL = "https://podcast-api.pocketcasts.com/podcast/full/{uuid}"

# Episode progress API endpoints
POCKETCASTS_PODCAST_EPISODES_URL = f"{POCKETCASTS_API_BASE}/user/podcast/episodes"
POCKETCASTS_SYNC_UPDATE_EPISODE_URL = f"{POCKETCASTS_API_BASE}/sync/update_episode"
POCKETCASTS_IN_PROGRESS_URL = f"{POCKETCASTS_API_BASE}/user/in_progress"
POCKETCASTS_STARRED_URL = f"{POCKETCASTS_API_BASE}/user/starred"
POCKETCASTS_NEW_RELEASES_URL = f"{POCKETCASTS_API_BASE}/user/new_releases"
POCKETCASTS_HISTORY_URL = f"{POCKETCASTS_API_BASE}/user/history"
POCKETCASTS_BOOKMARKS_URL = f"{POCKETCASTS_API_BASE}/user/bookmark/list"
POCKETCASTS_UP_NEXT_URL = f"{POCKETCASTS_API_BASE}/up_next/list"

# Browse path constants
BROWSE_UP_NEXT = "up_next"
BROWSE_IN_PROGRESS = "in_progress"
BROWSE_STARRED = "starred"
BROWSE_NEW_RELEASES = "new_releases"
BROWSE_HISTORY = "history"
BROWSE_BOOKMARKS = "bookmarks"

# Artwork URL pattern
POCKETCASTS_ARTWORK_URL = "https://static.pocketcasts.com/discover/images/webp/200/{uuid}.webp"

# Episode playing status constants (from Pocket Casts API)
STATUS_NOT_PLAYED = 1
STATUS_IN_PROGRESS = 2
STATUS_COMPLETED = 3


SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.LIBRARY_PODCASTS,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return PocketCastsProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    :param mass: MusicAssistant instance.
    :param instance_id: id of an existing provider instance (None if new instance setup).
    :param action: [optional] action key called from config entries UI.
    :param values: the (intermediate) raw values for config entries sent with the action.
    """
    # ruff: noqa: ARG001
    return (
        ConfigEntry(
            key=CONF_USERNAME,
            type=ConfigEntryType.STRING,
            label="Email",
            required=True,
            description="Your Pocket Casts account email address.",
        ),
        ConfigEntry(
            key=CONF_PASSWORD,
            type=ConfigEntryType.SECURE_STRING,
            label="Password",
            required=True,
            description="Your Pocket Casts account password.",
        ),
    )


class PocketCastsProvider(MusicProvider):
    """Pocket Casts Music Provider."""

    _auth_info: dict[str, Any] | None = None

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        return True

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        if not self.config.get_value(CONF_USERNAME) or not self.config.get_value(CONF_PASSWORD):
            msg = "Invalid login credentials"
            raise LoginFailed(msg)
        # Attempt login to validate credentials
        token = await self._get_auth_token()
        if not token:
            msg = f"Login failed for user {self.config.get_value(CONF_USERNAME)}"
            raise LoginFailed(msg)

    @lock
    async def _get_auth_token(self) -> str | None:
        """
        Get authentication token, logging in if necessary.

        Returns the bearer token or None if login fails.
        """
        # If we have a cached token, verify it's still valid
        if self._auth_info and self._auth_info.get("token"):
            if await self._verify_token():
                return str(self._auth_info["token"])
            # Token invalid, clear cached auth info
            self._auth_info = None

        # Perform login
        email = self.config.get_value(CONF_USERNAME)
        password = self.config.get_value(CONF_PASSWORD)

        async with self.mass.http_session.post(
            POCKETCASTS_LOGIN_URL,
            data={"email": email, "password": password},
        ) as response:
            if response.status != 200:
                self.logger.error("Pocket Casts login failed with status %s", response.status)
                return None

            try:
                data = await response.json()
            except Exception as err:
                self.logger.error("Failed to parse login response: %s", err)
                return None

            if "token" not in data:
                self.logger.error("Login response missing token")
                return None

            # Store auth info: token, uuid, email
            self._auth_info = {
                "token": data["token"],
                "uuid": data.get("uuid"),
                "email": data.get("email"),
            }
            self.logger.info("Successfully logged in to Pocket Casts as %s", data.get("email"))
            return str(self._auth_info["token"])

    async def _verify_token(self) -> bool:
        """
        Verify the current token is still valid.

        Uses the /subscription/status endpoint to check authentication.
        Returns True if token is valid, False otherwise.
        """
        if not self._auth_info or not self._auth_info.get("token"):
            return False

        headers = {"Authorization": f"Bearer {self._auth_info['token']}"}

        try:
            async with self.mass.http_session.get(
                POCKETCASTS_SUBSCRIPTION_STATUS_URL,
                headers=headers,
            ) as response:
                if response.status == 401:
                    # UNAUTHORIZED - token is invalid
                    self.logger.debug("Pocket Casts token is invalid/expired")
                    return False
                if response.status == 200:
                    return True
                # Other status codes - treat as invalid to be safe
                self.logger.debug(
                    "Token verification returned unexpected status: %s", response.status
                )
                return False
        except Exception as err:
            self.logger.warning("Error verifying token: %s", err)
            return False

    async def _get_headers(self) -> dict[str, str]:
        """Get headers with authentication for API requests."""
        token = await self._get_auth_token()
        if not token:
            msg = "Not authenticated"
            raise LoginFailed(msg)
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def _get_episode_progress(self, podcast_uuid: str) -> dict[str, dict[str, Any]]:
        """Fetch episode progress for a podcast from Pocket Casts API.

        :param podcast_uuid: The UUID of the podcast.
        :return: Dict mapping episode UUID to progress info (playingStatus, playedUpTo).
        """
        headers = await self._get_headers()

        async with self.mass.http_session.post(
            POCKETCASTS_PODCAST_EPISODES_URL,
            headers=headers,
            json={"uuid": podcast_uuid},
        ) as response:
            if response.status != 200:
                self.logger.debug(
                    "Failed to fetch episode progress for %s, status: %s",
                    podcast_uuid,
                    response.status,
                )
                return {}

            try:
                data = await response.json(content_type=None)
            except Exception as err:
                self.logger.debug("Failed to parse episode progress response: %s", err)
                return {}

            # Build a map of episode UUID -> progress info
            progress_map: dict[str, dict[str, Any]] = {}
            for episode in data.get("episodes", []):
                ep_uuid = episode.get("uuid")
                if ep_uuid:
                    progress_map[ep_uuid] = {
                        "playingStatus": episode.get("playingStatus"),
                        "playedUpTo": episode.get("playedUpTo"),
                        "duration": episode.get("duration"),
                        "starred": episode.get("starred"),
                        "isDeleted": episode.get("isDeleted"),
                    }

            return progress_map

    async def _sync_episode_progress(
        self,
        episode_uuid: str,
        podcast_uuid: str,
        position: int | None,
        duration: int,
        status: int,
    ) -> bool:
        """Sync episode progress to Pocket Casts.

        :param episode_uuid: The UUID of the episode.
        :param podcast_uuid: The UUID of the podcast.
        :param position: Playback position in seconds (None for manual mark as played).
        :param duration: Episode duration in seconds.
        :param status: Playing status (1=not played, 2=in progress, 3=completed).
        :return: True if sync was successful.
        """
        headers = await self._get_headers()

        # For completed episodes without a position, use duration as position
        if position is not None:
            sync_position = position
        elif status == STATUS_COMPLETED:
            sync_position = duration
        else:
            sync_position = 0

        payload = {
            "uuid": episode_uuid,
            "podcast": podcast_uuid,
            "position": sync_position,
            "duration": duration,
            "status": status,
        }

        try:
            async with self.mass.http_session.post(
                POCKETCASTS_SYNC_UPDATE_EPISODE_URL,
                headers=headers,
                json=payload,
            ) as response:
                if response.status == 200:
                    return True
                self.logger.warning("Failed to sync episode progress, status: %s", response.status)
                return False
        except Exception as err:
            self.logger.warning("Error syncing episode progress: %s", err)
            return False

    async def _get_subscribed_podcasts(self) -> list[dict[str, Any]]:
        """Fetch the list of subscribed podcasts from Pocket Casts API."""
        headers = await self._get_headers()

        async with self.mass.http_session.post(
            POCKETCASTS_PODCAST_LIST_URL,
            headers=headers,
        ) as response:
            if response.status != 200:
                self.logger.warning("Failed to fetch podcast list, status: %s", response.status)
                return []

            try:
                # aiohttp handles gzip Content-Encoding automatically
                # content_type=None to accept application/octet-stream fallback
                data = await response.json(content_type=None)
            except Exception as err:
                self.logger.warning("Failed to parse podcast list response: %s", err)
                return []

            # The response contains a "podcasts" array
            podcasts: list[dict[str, Any]] = data.get("podcasts", [])
            self.logger.debug("Fetched %d podcasts from Pocket Casts", len(podcasts))
            return podcasts

    def _parse_podcast(self, podcast_data: dict[str, Any]) -> Podcast:
        """Parse a podcast from Pocket Casts API response into a Podcast object."""
        podcast_uuid = podcast_data.get("uuid", "")
        title = podcast_data.get("title", "Unknown Podcast")
        author = podcast_data.get("author", "")
        description = podcast_data.get("description", "")

        # Build artwork URL
        artwork_url = POCKETCASTS_ARTWORK_URL.format(uuid=podcast_uuid)

        # Create metadata with artwork
        metadata = MediaItemMetadata(
            description=description,
            images=UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=artwork_url,
                        provider=self.lookup_key,
                        remotely_accessible=True,
                    )
                ]
            ),
        )

        # Create provider mapping
        provider_mapping = ProviderMapping(
            item_id=podcast_uuid,
            provider_domain=self.domain,
            provider_instance=self.instance_id,
            available=True,
        )

        return Podcast(
            item_id=podcast_uuid,
            provider=self.domain,
            name=title,
            publisher=author,
            provider_mappings={provider_mapping},
            metadata=metadata,
        )

    async def get_library_podcasts(self) -> AsyncGenerator[Podcast, None]:
        """Retrieve library/subscribed podcasts from the provider."""
        podcasts = await self._get_subscribed_podcasts()

        for podcast_data in podcasts:
            try:
                podcast = self._parse_podcast(podcast_data)
                yield podcast
            except Exception as err:
                self.logger.warning(
                    "Failed to parse podcast %s: %s",
                    podcast_data.get("uuid", "unknown"),
                    err,
                )

    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Get full podcast details by id."""
        # Fetch from subscribed podcasts and find by UUID
        podcasts = await self._get_subscribed_podcasts()

        for podcast_data in podcasts:
            if podcast_data.get("uuid") == prov_podcast_id:
                return self._parse_podcast(podcast_data)

        raise MediaNotFoundError(f"Podcast not found: {prov_podcast_id}")

    async def browse(self, path: str) -> list[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse this provider's items.

        :param path: The path to browse, (e.g. provider_id://in_progress).
        """
        base = f"{self.instance_id}://"

        if path == base or not path.startswith(base):
            # Return root browse folders - add custom folders before default folders
            default_folders = await super().browse(path)
            up_next_folder = BrowseFolder(
                item_id=BROWSE_UP_NEXT,
                provider=self.domain,
                path=f"{base}{BROWSE_UP_NEXT}",
                name="Up Next",
            )
            in_progress_folder = BrowseFolder(
                item_id=BROWSE_IN_PROGRESS,
                provider=self.domain,
                path=f"{base}{BROWSE_IN_PROGRESS}",
                name="In Progress",
            )
            starred_folder = BrowseFolder(
                item_id=BROWSE_STARRED,
                provider=self.domain,
                path=f"{base}{BROWSE_STARRED}",
                name="Starred",
            )
            new_releases_folder = BrowseFolder(
                item_id=BROWSE_NEW_RELEASES,
                provider=self.domain,
                path=f"{base}{BROWSE_NEW_RELEASES}",
                name="New Releases",
            )
            history_folder = BrowseFolder(
                item_id=BROWSE_HISTORY,
                provider=self.domain,
                path=f"{base}{BROWSE_HISTORY}",
                name="History",
            )
            bookmarks_folder = BrowseFolder(
                item_id=BROWSE_BOOKMARKS,
                provider=self.domain,
                path=f"{base}{BROWSE_BOOKMARKS}",
                name="Bookmarks",
            )
            return [
                up_next_folder,
                in_progress_folder,
                new_releases_folder,
                starred_folder,
                history_folder,
                bookmarks_folder,
                *default_folders,
            ]

        # Parse subpath
        subpath = path[len(base) :]

        if subpath == BROWSE_UP_NEXT:
            return list(await self._get_up_next_episodes())

        if subpath == BROWSE_IN_PROGRESS:
            return list(await self._get_in_progress_episodes())

        if subpath == BROWSE_STARRED:
            return list(await self._get_starred_episodes())

        if subpath == BROWSE_NEW_RELEASES:
            return list(await self._get_new_releases_episodes())

        if subpath == BROWSE_HISTORY:
            return list(await self._get_history_episodes())

        if subpath == BROWSE_BOOKMARKS:
            return list(await self._get_bookmarked_episodes())

        # Fall back to default browse handling
        return list(await super().browse(path))

    async def _get_up_next_episodes(self) -> list[PodcastEpisode]:
        """Fetch Up Next queue from Pocket Casts.

        :return: List of PodcastEpisode items in the Up Next queue.
        """
        headers = await self._get_headers()

        # The Up Next API requires specific payload format
        # For initial fetch, we don't include serverModified
        payload: dict[str, Any] = {
            "version": 2,
            "model": "webplayer",
            "showPlayStatus": True,
        }

        async with self.mass.http_session.post(
            POCKETCASTS_UP_NEXT_URL,
            headers=headers,
            json=payload,
        ) as response:
            if response.status != 200:
                response_text = await response.text()
                self.logger.warning(
                    "Failed to fetch Up Next queue: status=%s, body=%s",
                    response.status,
                    response_text[:500] if response_text else "(empty)",
                )
                return []

            data = await response.json()

        # Build a map of episode sync data (playedUpTo, duration) by uuid
        episode_sync: dict[str, dict[str, Any]] = {}
        for sync_data in data.get("episodeSync", []):
            episode_uuid = sync_data.get("uuid")
            if episode_uuid:
                episode_sync[episode_uuid] = sync_data

        episodes: list[PodcastEpisode] = []
        for ep_data in data.get("episodes", []):
            try:
                episode_uuid = ep_data.get("uuid", "")
                # Merge sync data into episode data for parsing
                if episode_uuid in episode_sync:
                    ep_data["playedUpTo"] = episode_sync[episode_uuid].get("playedUpTo", 0)
                    if "duration" not in ep_data:
                        ep_data["duration"] = episode_sync[episode_uuid].get("duration", 0)
                episode = self._parse_browse_episode(ep_data)
                episodes.append(episode)
            except Exception as err:
                self.logger.warning(
                    "Failed to parse Up Next episode %s: %s",
                    ep_data.get("uuid", "unknown"),
                    err,
                )

        return episodes

    async def _get_history_episodes(self) -> list[PodcastEpisode]:
        """Fetch recently played episodes from Pocket Casts.

        :return: List of recently played PodcastEpisode items (up to 100).
        """
        headers = await self._get_headers()

        async with self.mass.http_session.post(
            POCKETCASTS_HISTORY_URL,
            headers=headers,
            json={},
        ) as response:
            if response.status != 200:
                self.logger.warning("Failed to fetch history: %s", response.status)
                return []

            data = await response.json()

        episodes: list[PodcastEpisode] = []
        for ep_data in data.get("episodes", []):
            try:
                episode = self._parse_browse_episode(ep_data)
                episodes.append(episode)
            except Exception as err:
                self.logger.warning(
                    "Failed to parse history episode %s: %s",
                    ep_data.get("uuid", "unknown"),
                    err,
                )

        return episodes

    async def _get_bookmarked_episodes(self) -> list[PodcastEpisode]:
        """Fetch bookmarked episodes from Pocket Casts.

        Bookmarks are timestamps within episodes that the user has saved.
        Each bookmark will be returned as an episode item that starts at
        the bookmarked timestamp when played.

        :return: List of PodcastEpisode items for bookmarks.
        """
        headers = await self._get_headers()

        async with self.mass.http_session.post(
            POCKETCASTS_BOOKMARKS_URL,
            headers=headers,
            json={},
        ) as response:
            if response.status != 200:
                self.logger.warning("Failed to fetch bookmarks: %s", response.status)
                return []

            data = await response.json()

        episodes: list[PodcastEpisode] = []
        for bookmark in data.get("bookmarks", []):
            try:
                episode = self._parse_bookmark(bookmark)
                episodes.append(episode)
            except Exception as err:
                self.logger.warning(
                    "Failed to parse bookmark %s: %s",
                    bookmark.get("bookmarkUuid", "unknown"),
                    err,
                )

        return episodes

    def _parse_bookmark(self, bookmark: dict[str, Any]) -> PodcastEpisode:
        """Parse a bookmark into a PodcastEpisode.

        The bookmark will be displayed with its title and will start playback
        at the bookmarked timestamp.

        :param bookmark: Bookmark data from /user/bookmark/list API.
        :return: Parsed PodcastEpisode that starts at the bookmark timestamp.
        """
        podcast_uuid = bookmark.get("podcastUuid", "")
        episode_uuid = bookmark.get("episodeUuid", "")
        bookmark_title = bookmark.get("title", "Bookmark")
        bookmark_time = bookmark.get("time", 0)  # Seconds into episode

        # Episode ID format: "{podcast_uuid} {episode_uuid}@bookmark:{timestamp}"
        # The @bookmark:{timestamp} suffix tells get_resume_position to use this timestamp
        # instead of fetching from the API
        episode_id = f"{podcast_uuid} {episode_uuid}@bookmark:{bookmark_time}"

        # Build images from podcast UUID
        images: UniqueList[MediaItemImage] = UniqueList()
        if podcast_uuid:
            artwork_url = POCKETCASTS_ARTWORK_URL.format(uuid=podcast_uuid)
            images.append(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=artwork_url,
                    provider=self.lookup_key,
                    remotely_accessible=True,
                )
            )

        # Create provider mapping - URL will be fetched when playing
        provider_mapping = ProviderMapping(
            item_id=episode_id,
            provider_domain=self.domain,
            provider_instance=self.instance_id,
            available=True,
            audio_format=AudioFormat(content_type=ContentType.UNKNOWN),
        )

        # Create metadata with bookmark info
        metadata = MediaItemMetadata(
            images=images if images else None,
        )

        # Create podcast item mapping
        podcast_mapping = ItemMapping(
            media_type=MediaType.PODCAST,
            item_id=podcast_uuid,
            provider=self.lookup_key,
            name="",  # We don't have podcast title from bookmark API
        )

        # Use bookmark title as episode name, include timestamp info
        display_name = f"{bookmark_title} @ {self._format_timestamp(bookmark_time)}"

        return PodcastEpisode(
            item_id=episode_id,
            provider=self.lookup_key,
            name=display_name,
            duration=0,  # Unknown from bookmark data
            position=0,
            podcast=podcast_mapping,
            provider_mappings={provider_mapping},
            metadata=metadata,
            fully_played=False,
            resume_position_ms=bookmark_time * 1000,  # Start at bookmark timestamp
        )

    @staticmethod
    def _format_timestamp(seconds: int) -> str:
        """Format seconds into MM:SS or HH:MM:SS string."""
        hours, remainder = divmod(seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours > 0:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        return f"{minutes}:{secs:02d}"

    async def _get_new_releases_episodes(self) -> list[PodcastEpisode]:
        """Fetch new release episodes from Pocket Casts.

        :return: List of new release PodcastEpisode items.
        """
        headers = await self._get_headers()

        async with self.mass.http_session.post(
            POCKETCASTS_NEW_RELEASES_URL,
            headers=headers,
            json={},
        ) as response:
            if response.status != 200:
                self.logger.warning("Failed to fetch new releases: %s", response.status)
                return []

            data = await response.json()

        episodes: list[PodcastEpisode] = []
        for ep_data in data.get("episodes", []):
            try:
                episode = self._parse_browse_episode(ep_data)
                episodes.append(episode)
            except Exception as err:
                self.logger.warning(
                    "Failed to parse new release episode %s: %s",
                    ep_data.get("uuid", "unknown"),
                    err,
                )

        return episodes

    async def _get_starred_episodes(self) -> list[PodcastEpisode]:
        """Fetch starred episodes from Pocket Casts.

        :return: List of starred PodcastEpisode items.
        """
        headers = await self._get_headers()

        async with self.mass.http_session.post(
            POCKETCASTS_STARRED_URL,
            headers=headers,
            json={},
        ) as response:
            if response.status != 200:
                self.logger.warning("Failed to fetch starred episodes: %s", response.status)
                return []

            data = await response.json()

        episodes: list[PodcastEpisode] = []
        for ep_data in data.get("episodes", []):
            try:
                episode = self._parse_browse_episode(ep_data)
                episodes.append(episode)
            except Exception as err:
                self.logger.warning(
                    "Failed to parse starred episode %s: %s",
                    ep_data.get("uuid", "unknown"),
                    err,
                )

        return episodes

    async def _get_in_progress_episodes(self) -> list[PodcastEpisode]:
        """Fetch episodes currently in progress from Pocket Casts.

        :return: List of PodcastEpisode items with resume positions set.
        """
        headers = await self._get_headers()

        async with self.mass.http_session.post(
            POCKETCASTS_IN_PROGRESS_URL,
            headers=headers,
            json={},
        ) as response:
            if response.status != 200:
                self.logger.warning("Failed to fetch in-progress episodes: %s", response.status)
                return []

            data = await response.json()

        episodes: list[PodcastEpisode] = []
        for ep_data in data.get("episodes", []):
            try:
                episode = self._parse_browse_episode(ep_data)
                episodes.append(episode)
            except Exception as err:
                self.logger.warning(
                    "Failed to parse in-progress episode %s: %s",
                    ep_data.get("uuid", "unknown"),
                    err,
                )

        return episodes

    def _parse_browse_episode(self, ep_data: dict[str, Any]) -> PodcastEpisode:
        """Parse an episode from browse API response.

        :param ep_data: Episode data from browse APIs (in_progress, starred, etc.).
        :return: Parsed PodcastEpisode.
        """
        episode_uuid = ep_data["uuid"]
        # Different APIs use different keys for podcast UUID
        podcast_uuid = ep_data.get("podcastUuid") or ep_data.get("podcast", "")
        title = ep_data.get("title", "Unknown Episode")
        podcast_title = ep_data.get("podcastTitle", "Unknown Podcast")

        # Episode ID format: "{podcast_uuid} {episode_uuid}" (matching main parser)
        episode_id = f"{podcast_uuid} {episode_uuid}"

        # Duration in seconds
        duration = ep_data.get("duration", 0)

        # Determine played status from playingStatus field
        playing_status = ep_data.get("playingStatus", STATUS_NOT_PLAYED)
        played_up_to = ep_data.get("playedUpTo", 0)

        if playing_status == STATUS_COMPLETED:
            fully_played = True
            resume_position_ms = 0
        elif playing_status == STATUS_IN_PROGRESS and played_up_to:
            fully_played = False
            resume_position_ms = int(played_up_to * 1000)
        else:
            fully_played = False
            resume_position_ms = 0

        # Build images from podcast UUID
        images: UniqueList[MediaItemImage] = UniqueList()
        if podcast_uuid:
            artwork_url = POCKETCASTS_ARTWORK_URL.format(uuid=podcast_uuid)
            images.append(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=artwork_url,
                    provider=self.lookup_key,
                    remotely_accessible=True,
                )
            )

        # Create provider mapping
        provider_mapping = ProviderMapping(
            item_id=episode_id,
            provider_domain=self.domain,
            provider_instance=self.instance_id,
            available=True,
            audio_format=AudioFormat(content_type=ContentType.UNKNOWN),
            url=ep_data.get("url", ""),
        )

        # Create metadata
        metadata = MediaItemMetadata(
            description=ep_data.get("showNotes"),
            images=images if images else None,
        )

        # Create podcast item mapping for the parent podcast
        podcast_mapping = ItemMapping(
            media_type=MediaType.PODCAST,
            item_id=podcast_uuid,
            provider=self.lookup_key,
            name=podcast_title,
        )

        return PodcastEpisode(
            item_id=episode_id,
            provider=self.lookup_key,
            name=title,
            duration=duration,
            position=0,
            podcast=podcast_mapping,
            provider_mappings={provider_mapping},
            metadata=metadata,
            fully_played=fully_played,
            resume_position_ms=resume_position_ms,
        )

    async def _get_podcast_episodes_data(
        self, podcast_uuid: str
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Fetch episodes for a podcast via the podcast-api redirect.

        :param podcast_uuid: The UUID of the podcast.
        :return: Tuple of (episodes list, podcast metadata dict).
        """
        headers = await self._get_headers()
        url = POCKETCASTS_PODCAST_FULL_URL.format(uuid=podcast_uuid)

        # First request to get the Location header (redirect)
        async with self.mass.http_session.get(
            url, headers=headers, allow_redirects=False
        ) as response:
            if response.status not in (301, 302, 307, 308):
                self.logger.warning(
                    "Unexpected status %s from podcast episodes API for %s",
                    response.status,
                    podcast_uuid,
                )
                return [], {}

            location = response.headers.get("Location")
            if not location:
                self.logger.warning("No Location header in redirect for podcast %s", podcast_uuid)
                return [], {}

        # Second request to the redirected URL
        async with self.mass.http_session.get(location, headers=headers) as response:
            if response.status != 200:
                self.logger.warning(
                    "Failed to fetch episodes from redirect URL, status: %s", response.status
                )
                return [], {}

            try:
                data = await response.json(content_type=None)
            except Exception as err:
                self.logger.warning("Failed to parse episodes response: %s", err)
                return [], {}

            podcast_info = data.get("podcast", {})
            episodes = podcast_info.get("episodes", [])
            self.logger.debug("Fetched %d episodes for podcast %s", len(episodes), podcast_uuid)
            return episodes, podcast_info

    def _parse_podcast_episode(
        self,
        episode_data: dict[str, Any],
        podcast_uuid: str,
        podcast_title: str,
        podcast_artwork_url: str,
        position: int,
        progress_info: dict[str, Any] | None = None,
    ) -> PodcastEpisode | None:
        """Parse an episode from Pocket Casts API response into a PodcastEpisode object.

        :param episode_data: Raw episode data from API.
        :param podcast_uuid: UUID of the parent podcast.
        :param podcast_title: Title of the parent podcast.
        :param podcast_artwork_url: Artwork URL for the podcast (fallback).
        :param position: Position/index of the episode.
        :param progress_info: Optional progress info dict with playingStatus and playedUpTo.
        :return: PodcastEpisode or None if essential data is missing.
        """
        episode_uuid = episode_data.get("uuid")
        if not episode_uuid:
            return None

        title = episode_data.get("title", "Unknown Episode")
        stream_url = episode_data.get("url")
        if not stream_url:
            self.logger.debug("Episode %s has no stream URL, skipping", episode_uuid)
            return None

        duration = episode_data.get("duration", 0)
        file_type = episode_data.get("fileType", "")
        published = episode_data.get("published")  # ISO 8601 string

        # Episode ID format: "{podcast_uuid} {episode_uuid}"
        episode_id = f"{podcast_uuid} {episode_uuid}"

        # Determine content type from file_type
        content_type = ContentType.try_parse(file_type) if file_type else ContentType.UNKNOWN
        if content_type == ContentType.UNKNOWN and stream_url:
            content_type = ContentType.try_parse(stream_url)

        # Create the episode
        episode = PodcastEpisode(
            item_id=episode_id,
            provider=self.lookup_key,
            name=title,
            duration=duration,
            position=position,
            podcast=ItemMapping(
                item_id=podcast_uuid,
                provider=self.lookup_key,
                name=podcast_title,
                media_type=MediaType.PODCAST,
            ),
            provider_mappings={
                ProviderMapping(
                    item_id=episode_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    audio_format=AudioFormat(content_type=content_type),
                    url=stream_url,
                )
            },
        )

        # Parse and set release date if available
        if published:
            try:
                # Handle ISO 8601 format (e.g., "2025-01-15T06:00:00Z")
                release_date = datetime.fromisoformat(published)
                episode.metadata.release_date = release_date
            except ValueError:
                pass  # Ignore invalid date format

        # Set episode artwork (fallback to podcast artwork)
        episode.metadata.images = UniqueList(
            [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=podcast_artwork_url,
                    provider=self.lookup_key,
                    remotely_accessible=True,
                )
            ]
        )

        # Apply progress info if available
        if progress_info:
            playing_status = progress_info.get("playingStatus")
            played_up_to = progress_info.get("playedUpTo")

            if playing_status == STATUS_COMPLETED:
                episode.fully_played = True
                episode.resume_position_ms = 0
            elif playing_status == STATUS_IN_PROGRESS and played_up_to is not None:
                episode.fully_played = False
                episode.resume_position_ms = played_up_to * 1000  # Convert to ms
            elif playing_status == STATUS_NOT_PLAYED:
                episode.fully_played = False
                episode.resume_position_ms = 0

        return episode

    async def get_podcast_episodes(
        self, prov_podcast_id: str
    ) -> AsyncGenerator[PodcastEpisode, None]:
        """Get all episodes for given podcast id."""
        episodes_data, podcast_info = await self._get_podcast_episodes_data(prov_podcast_id)

        if not episodes_data:
            return

        # Fetch episode progress info
        progress_map = await self._get_episode_progress(prov_podcast_id)

        podcast_title = podcast_info.get("title", "Unknown Podcast")
        podcast_artwork_url = POCKETCASTS_ARTWORK_URL.format(uuid=prov_podcast_id)

        for position, episode_data in enumerate(episodes_data):
            try:
                episode_uuid = episode_data.get("uuid")
                progress_info = progress_map.get(episode_uuid) if episode_uuid else None

                episode = self._parse_podcast_episode(
                    episode_data=episode_data,
                    podcast_uuid=prov_podcast_id,
                    podcast_title=podcast_title,
                    podcast_artwork_url=podcast_artwork_url,
                    position=position,
                    progress_info=progress_info,
                )
                if episode:
                    yield episode
            except Exception as err:
                self.logger.warning(
                    "Failed to parse episode %s: %s",
                    episode_data.get("uuid", "unknown"),
                    err,
                )

    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """Get full podcast episode details by id.

        :param prov_episode_id: Episode ID in format "{podcast_uuid} {episode_uuid}".
            May include "@bookmark:{timestamp}" suffix for bookmark playback.
        """
        # Check for bookmark suffix (format: "@bookmark:{timestamp}")
        bookmark_suffix = ""
        bookmark_time_ms = 0
        if "@bookmark:" in prov_episode_id:
            base_episode_id, bookmark_time_str = prov_episode_id.split("@bookmark:", 1)
            bookmark_suffix = f"@bookmark:{bookmark_time_str}"
            with suppress(ValueError):
                bookmark_time_ms = int(bookmark_time_str) * 1000
        else:
            base_episode_id = prov_episode_id

        parts = base_episode_id.split(" ", 1)
        if len(parts) != 2:
            raise MediaNotFoundError(f"Invalid episode ID format: {prov_episode_id}")

        podcast_uuid, _episode_uuid = parts

        # Fetch all episodes for the podcast and find the matching one
        async for episode in self.get_podcast_episodes(podcast_uuid):
            if episode.item_id == base_episode_id:
                # If this is a bookmark request, modify the episode to use bookmark ID
                if bookmark_suffix:
                    bookmark_id = f"{episode.item_id}{bookmark_suffix}"
                    # Update the episode item_id and provider mapping to include bookmark
                    episode.item_id = bookmark_id
                    episode.resume_position_ms = bookmark_time_ms
                    # Update provider mappings with bookmark ID
                    new_mappings = set()
                    for mapping in episode.provider_mappings:
                        new_mapping = ProviderMapping(
                            item_id=bookmark_id,
                            provider_domain=mapping.provider_domain,
                            provider_instance=mapping.provider_instance,
                            available=mapping.available,
                            url=mapping.url,
                            audio_format=mapping.audio_format,
                        )
                        new_mappings.add(new_mapping)
                    episode.provider_mappings = new_mappings
                return episode

        raise MediaNotFoundError(f"Episode not found: {prov_episode_id}")

    async def get_stream_details(
        self, item_id: str, media_type: MediaType = MediaType.PODCAST_EPISODE
    ) -> StreamDetails:
        """Get streamdetails for a podcast episode.

        :param item_id: Episode ID in format "{podcast_uuid} {episode_uuid}".
            May include "@bookmark:{timestamp}" suffix for bookmark playback.
        :param media_type: Type of media (should be PODCAST_EPISODE).
        """
        # Strip bookmark suffix if present - get_podcast_episode handles this too
        # but we need the base ID for the StreamDetails.item_id
        base_item_id = item_id.split("@bookmark:")[0]

        # Fetch the episode to get the stream URL
        episode = await self.get_podcast_episode(base_item_id)

        # Get stream URL from provider mapping
        stream_url: str | None = None
        content_type = ContentType.UNKNOWN
        for mapping in episode.provider_mappings:
            if mapping.url:
                stream_url = mapping.url
                if mapping.audio_format:
                    content_type = mapping.audio_format.content_type
                break

        if not stream_url:
            raise MediaNotFoundError(f"No stream URL for episode: {item_id}")

        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(content_type=content_type),
            media_type=media_type,
            stream_type=StreamType.HTTP,
            path=stream_url,
            duration=episode.duration,
            allow_seek=True,
            extra_input_args=[
                "-user_agent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            ],
        )

    async def get_resume_position(self, item_id: str, media_type: MediaType) -> tuple[bool, int]:
        """Get progress (resume point) details for the given podcast episode.

        Called right before playback starts to ensure the resume position is correct.

        :param item_id: Episode ID in format "{podcast_uuid} {episode_uuid}".
            May include "@bookmark:{timestamp}" suffix for bookmark playback.
        :param media_type: Type of media (should be PODCAST_EPISODE).
        :return: Tuple of (fully_played, resume_position_ms).
        """
        if media_type != MediaType.PODCAST_EPISODE:
            raise NotImplementedError

        # Check for bookmark timestamp suffix (format: "@bookmark:{timestamp}")
        if "@bookmark:" in item_id:
            base_id, bookmark_suffix = item_id.split("@bookmark:", 1)
            try:
                bookmark_seconds = int(bookmark_suffix)
                bookmark_ms = bookmark_seconds * 1000
                self.logger.debug(
                    "Using bookmark timestamp for %s: %d ms",
                    base_id,
                    bookmark_ms,
                )
                return False, bookmark_ms
            except ValueError:
                self.logger.warning(
                    "Invalid bookmark timestamp in %s, falling back to API",
                    item_id,
                )
                item_id = base_id  # Continue with normal flow

        parts = item_id.split(" ", 1)
        if len(parts) != 2:
            raise MediaNotFoundError(f"Invalid episode ID format: {item_id}")

        podcast_uuid, episode_uuid = parts

        # Fetch fresh progress from Pocket Casts
        progress_map = await self._get_episode_progress(podcast_uuid)
        progress_info = progress_map.get(episode_uuid)

        if not progress_info:
            # No progress info available, fall back to internal state
            raise NotImplementedError

        playing_status = progress_info.get("playingStatus")
        played_up_to = progress_info.get("playedUpTo", 0)

        if playing_status == STATUS_COMPLETED:
            return True, 0
        elif playing_status == STATUS_IN_PROGRESS:
            resume_ms = played_up_to * 1000  # Convert to ms
            return False, resume_ms
        else:  # STATUS_NOT_PLAYED or unknown
            return False, 0

    async def on_played(
        self,
        media_type: MediaType,
        prov_item_id: str,
        fully_played: bool,
        position: int,
        media_item: MediaItemType,
        is_playing: bool = False,
    ) -> None:
        """Handle callback when a podcast episode has been played.

        Called by the Queue controller when:
        - A track has been fully played
        - A track has been stopped (or skipped) after being played
        - Every 30s when a track is playing

        :param media_type: Type of media.
        :param prov_item_id: Episode ID in format "{podcast_uuid} {episode_uuid}".
        :param fully_played: True when the episode has been played to the end.
        :param position: Last known position in seconds.
        :param media_item: The full media item details.
        :param is_playing: True when the episode is currently playing.
        """
        if media_type != MediaType.PODCAST_EPISODE:
            return

        if not isinstance(media_item, PodcastEpisode):
            return

        # Strip bookmark suffix if present (format: "@bookmark:{timestamp}")
        base_item_id = prov_item_id.split("@bookmark:")[0]

        parts = base_item_id.split(" ", 1)
        if len(parts) != 2:
            self.logger.warning("Invalid episode ID format for progress sync: %s", prov_item_id)
            return

        podcast_uuid, episode_uuid = parts

        # Get episode duration
        duration = media_item.duration or 0

        # Determine status based on playback state
        # For podcasts, consider it fully played if within 60 seconds of the end
        # This matches MA's internal logic for podcast episodes
        if fully_played:
            status = STATUS_COMPLETED
        elif position is not None and duration > 0 and position >= duration - 60:
            # Episode is near the end (within 60 seconds), mark as completed
            status = STATUS_COMPLETED
        elif position is not None and position > 0:
            status = STATUS_IN_PROGRESS
        else:
            status = STATUS_NOT_PLAYED

        # Sync progress to Pocket Casts
        await self._sync_episode_progress(
            episode_uuid=episode_uuid,
            podcast_uuid=podcast_uuid,
            position=position,
            duration=duration,
            status=status,
        )
