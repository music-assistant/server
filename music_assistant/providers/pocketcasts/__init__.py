"""
Pocketcasts Music Provider for Music Assistant.

Provides access to podcasts from a Pocket Casts account.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from aiohttp import ClientTimeout
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
                self.logger.warning("Pocket Casts login failed with status %s", response.status)
                return None

            try:
                data = await response.json()
            except Exception as err:
                self.logger.warning("Failed to parse login response: %s", err)
                return None

            if "token" not in data:
                self.logger.warning("Login response missing token")
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

            self.logger.debug(
                "Fetched progress for %d episodes of podcast %s",
                len(progress_map),
                podcast_uuid,
            )
            return progress_map

    async def _sync_episode_progress(
        self,
        episode_uuid: str,
        podcast_uuid: str,
        position: int,
        duration: int,
        status: int,
    ) -> bool:
        """Sync episode progress to Pocket Casts.

        :param episode_uuid: The UUID of the episode.
        :param podcast_uuid: The UUID of the podcast.
        :param position: Playback position in seconds.
        :param duration: Episode duration in seconds.
        :param status: Playing status (1=not played, 2=in progress, 3=completed).
        :return: True if sync was successful.
        """
        headers = await self._get_headers()

        payload = {
            "uuid": episode_uuid,
            "podcast": podcast_uuid,
            "position": position,
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
                    self.logger.debug(
                        "Synced progress for episode %s: position=%d, status=%d",
                        episode_uuid,
                        position,
                        status,
                    )
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
        """
        parts = prov_episode_id.split(" ", 1)
        if len(parts) != 2:
            raise MediaNotFoundError(f"Invalid episode ID format: {prov_episode_id}")

        podcast_uuid, _episode_uuid = parts

        # Fetch all episodes for the podcast and find the matching one
        async for episode in self.get_podcast_episodes(podcast_uuid):
            if episode.item_id == prov_episode_id:
                return episode

        raise MediaNotFoundError(f"Episode not found: {prov_episode_id}")

    async def get_stream_details(
        self, item_id: str, media_type: MediaType = MediaType.PODCAST_EPISODE
    ) -> StreamDetails:
        """Get streamdetails for a podcast episode.

        :param item_id: Episode ID in format "{podcast_uuid} {episode_uuid}".
        :param media_type: Type of media (should be PODCAST_EPISODE).
        """
        # Fetch the episode to get the stream URL
        episode = await self.get_podcast_episode(item_id)

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
            stream_type=StreamType.CUSTOM,
            path=stream_url,
            duration=episode.duration,
            can_seek=True,
        )

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """Return the audio stream for the podcast episode.

        Uses HTTP Range requests to support seeking without downloading from the beginning.

        :param streamdetails: The stream details containing the URL and metadata.
        :param seek_position: Position in seconds to seek to.
        """
        assert isinstance(streamdetails.path, str)
        url = streamdetails.path
        http_session = self.mass.http_session

        headers: dict[str, str] = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        }

        # If seeking, we need the file size to calculate byte position
        if seek_position and streamdetails.duration:
            # Do HEAD request to get Content-Length if not already known
            if not streamdetails.size:
                async with http_session.head(url, allow_redirects=True, headers=headers) as resp:
                    resp.raise_for_status()
                    if size := resp.headers.get("Content-Length"):
                        streamdetails.size = int(size)

            # Calculate byte position and add Range header
            if streamdetails.size:
                skip_bytes = int(streamdetails.size / streamdetails.duration * seek_position)
                headers["Range"] = f"bytes={skip_bytes}-"
                self.logger.debug(
                    "Seeking to position %d seconds (byte %d of %d) for %s",
                    seek_position,
                    skip_bytes,
                    streamdetails.size,
                    streamdetails.uri,
                )

        timeout = ClientTimeout(total=0, connect=30, sock_read=5 * 60)

        async with http_session.get(
            url, allow_redirects=True, headers=headers, timeout=timeout
        ) as resp:
            # Check if seek was successful (206 Partial Content)
            if seek_position and resp.status == 206:
                self.logger.debug(
                    "HTTP Range seek successful for %s (status 206)",
                    streamdetails.uri,
                )
                # Keep streamdetails.seek_position as-is (already set by caller)
            elif seek_position and resp.status == 200:
                # Server ignored Range header, streaming from beginning
                self.logger.warning(
                    "HTTP server does not support Range requests for %s, playing from beginning",
                    streamdetails.uri,
                )
                # Reset seek position since we're actually starting from beginning
                streamdetails.seek_position = 0

            resp.raise_for_status()
            async for chunk in resp.content.iter_any():
                yield chunk

    async def get_resume_position(self, item_id: str, media_type: MediaType) -> tuple[bool, int]:
        """Get progress (resume point) details for the given podcast episode.

        Called right before playback starts to ensure the resume position is correct.

        :param item_id: Episode ID in format "{podcast_uuid} {episode_uuid}".
        :param media_type: Type of media (should be PODCAST_EPISODE).
        :return: Tuple of (fully_played, resume_position_ms).
        """
        if media_type != MediaType.PODCAST_EPISODE:
            raise NotImplementedError

        parts = item_id.split(" ", 1)
        if len(parts) != 2:
            raise MediaNotFoundError(f"Invalid episode ID format: {item_id}")

        podcast_uuid, episode_uuid = parts

        # Fetch fresh progress from Pocket Casts
        progress_map = await self._get_episode_progress(podcast_uuid)
        progress_info = progress_map.get(episode_uuid)

        if not progress_info:
            # No progress info available, fall back to internal state
            self.logger.debug(
                "No progress info found for episode %s, falling back to internal state",
                episode_uuid,
            )
            raise NotImplementedError

        playing_status = progress_info.get("playingStatus")
        played_up_to = progress_info.get("playedUpTo", 0)

        if playing_status == STATUS_COMPLETED:
            self.logger.debug("Episode %s is fully played, resume_position=0", episode_uuid)
            return True, 0
        elif playing_status == STATUS_IN_PROGRESS:
            resume_ms = played_up_to * 1000  # Convert to ms
            self.logger.debug(
                "Episode %s in progress, resume_position=%d ms (playedUpTo=%d s)",
                episode_uuid,
                resume_ms,
                played_up_to,
            )
            return False, resume_ms
        else:  # STATUS_NOT_PLAYED or unknown
            self.logger.debug("Episode %s not played, resume_position=0", episode_uuid)
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

        parts = prov_item_id.split(" ", 1)
        if len(parts) != 2:
            self.logger.warning("Invalid episode ID format for progress sync: %s", prov_item_id)
            return

        podcast_uuid, episode_uuid = parts

        # Determine status based on playback state
        if fully_played:
            status = STATUS_COMPLETED
        elif position > 0:
            status = STATUS_IN_PROGRESS
        else:
            status = STATUS_NOT_PLAYED

        # Get episode duration
        duration = media_item.duration or 0

        # Sync progress to Pocket Casts
        await self._sync_episode_progress(
            episode_uuid=episode_uuid,
            podcast_uuid=podcast_uuid,
            position=position,
            duration=duration,
            status=status,
        )
