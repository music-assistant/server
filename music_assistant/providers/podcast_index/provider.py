"""Podcast Index provider implementation."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from typing import Any, cast

import aiohttp
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import (
    InvalidDataError,
    LoginFailed,
    MediaNotFoundError,
    ProviderUnavailableError,
)
from music_assistant_models.media_items import (
    AudioFormat,
    BrowseFolder,
    MediaItemType,
    Podcast,
    PodcastEpisode,
    SearchResults,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.controllers.cache import use_cache
from music_assistant.models.music_provider import MusicProvider

from .constants import (
    BROWSE_CATEGORIES,
    BROWSE_RECENT,
    BROWSE_TRENDING,
    CONF_API_KEY,
    CONF_API_SECRET,
    CONF_STORED_PODCASTS,
)
from .helpers import make_api_request, parse_episode_from_data, parse_podcast_from_feed


class PodcastIndexProvider(MusicProvider):
    """Podcast Index provider for Music Assistant."""

    api_key: str = ""
    api_secret: str = ""

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.api_key = str(self.config.get_value(CONF_API_KEY))
        self.api_secret = str(self.config.get_value(CONF_API_SECRET))

        if not self.api_key or not self.api_secret:
            raise LoginFailed("API key and secret are required")

        # Test API connection
        try:
            await self._api_request("stats/current")
        except (LoginFailed, ProviderUnavailableError):
            # Re-raise these specific errors as they have proper context
            raise
        except aiohttp.ClientConnectorError as err:
            raise ProviderUnavailableError(
                f"Failed to connect to Podcast Index API: {err}"
            ) from err
        except aiohttp.ServerTimeoutError as err:
            raise ProviderUnavailableError(f"Podcast Index API timeout: {err}") from err
        except Exception as err:
            raise LoginFailed(f"Failed to connect to API: {err}") from err

    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 10
    ) -> SearchResults:
        """
        Perform search on Podcast Index.

        Searches for podcasts by term. Future enhancement could include
        category search if needed.
        """
        result = SearchResults()
        if MediaType.PODCAST not in media_types:
            return result

        response = await self._api_request(
            "search/byterm", params={"q": search_query, "max": limit}
        )

        podcasts = []
        for feed_data in response.get("feeds", []):
            podcast = parse_podcast_from_feed(feed_data, self.instance_id, self.domain)
            if podcast:
                podcasts.append(podcast)

        result.podcasts = podcasts
        return result

    async def browse(self, path: str) -> Sequence[BrowseFolder | Podcast | PodcastEpisode]:
        """Browse this provider's items."""
        base = f"{self.instance_id}://"

        if path == base:
            # Return main browse categories
            return [
                BrowseFolder(
                    item_id=BROWSE_TRENDING,
                    provider=self.domain,
                    path=f"{base}{BROWSE_TRENDING}",
                    name="Trending Podcasts",
                ),
                BrowseFolder(
                    item_id=BROWSE_RECENT,
                    provider=self.domain,
                    path=f"{base}{BROWSE_RECENT}",
                    name="Recent Episodes",
                ),
                BrowseFolder(
                    item_id=BROWSE_CATEGORIES,
                    provider=self.domain,
                    path=f"{base}{BROWSE_CATEGORIES}",
                    name="Categories",
                ),
            ]

        # Parse path after base
        if path.startswith(base):
            subpath_parts = path[len(base) :].split("/")
            subpath = subpath_parts[0] if subpath_parts else ""

            if subpath == BROWSE_TRENDING:
                return await self._browse_trending()
            elif subpath == BROWSE_RECENT:
                return await self._browse_recent_episodes()
            elif subpath == BROWSE_CATEGORIES:
                if len(subpath_parts) > 1:
                    # Browse specific category - category name is directly in path
                    category_name = subpath_parts[1]
                    return await self._browse_category_podcasts(category_name)
                else:
                    # Browse categories
                    return await self._browse_categories()

        return []

    async def library_add(self, item: MediaItemType) -> bool:
        """
        Add podcast to library.

        Retrieves the RSS feed URL for the podcast and adds it to the stored
        podcasts configuration. Returns True if successfully added, False if
        the podcast was already in the library or if the feed URL couldn't be found.
        """
        # Only handle podcasts - delegate others to base class
        if not isinstance(item, Podcast):
            return await super().library_add(item)

        stored_podcasts = cast("list[str]", self.config.get_value(CONF_STORED_PODCASTS))

        # Get the RSS URL from the podcast via API
        try:
            feed_url = await self._get_feed_url_for_podcast(item.item_id)
        except Exception as err:
            self.logger.warning(
                "Failed to retrieve feed URL for podcast %s: %s", item.name, err, exc_info=True
            )
            return False

        if not feed_url:
            self.logger.warning(
                "No feed URL found for podcast %s (ID: %s)", item.name, item.item_id
            )
            return False

        if feed_url in stored_podcasts:
            return False

        self.logger.debug("Adding podcast %s to library", item.name)
        stored_podcasts.append(feed_url)
        self.update_config_value(CONF_STORED_PODCASTS, stored_podcasts)
        return True

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """
        Remove podcast from library.

        Removes the podcast's RSS feed URL from the stored podcasts configuration.
        Always returns True for idempotent operation. If feed URL retrieval fails,
        logs a warning but still returns True to maintain the idempotent contract
        as required by MA convention.
        """
        stored_podcasts = cast("list[str]", self.config.get_value(CONF_STORED_PODCASTS))

        # Get the RSS URL for this podcast
        try:
            feed_url = await self._get_feed_url_for_podcast(prov_item_id)
        except Exception as err:
            self.logger.warning(
                "Failed to retrieve feed URL for podcast removal %s: %s",
                prov_item_id,
                err,
                exc_info=True,
            )
            # Still return True for idempotent operation
            return True

        if not feed_url or feed_url not in stored_podcasts:
            return True

        self.logger.debug("Removing podcast %s from library", prov_item_id)
        stored_podcasts = [x for x in stored_podcasts if x != feed_url]
        self.update_config_value(CONF_STORED_PODCASTS, stored_podcasts)
        return True

    @use_cache(3600 * 24 * 14)  # Cache for 14 days
    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Get podcast details."""
        try:
            # Try by ID first
            response = await self._api_request("podcasts/byfeedid", params={"id": prov_podcast_id})
            if response.get("feed"):
                podcast = parse_podcast_from_feed(response["feed"], self.instance_id, self.domain)
                if podcast:
                    return podcast
        except (ProviderUnavailableError, InvalidDataError):
            # Re-raise these specific errors
            raise
        except Exception as err:
            self.logger.debug("Unexpected error getting podcast %s: %s", prov_podcast_id, err)

        raise MediaNotFoundError(f"Podcast {prov_podcast_id} not found")

    async def get_podcast_episodes(
        self, prov_podcast_id: str
    ) -> AsyncGenerator[PodcastEpisode, None]:
        """Get episodes for a podcast."""
        self.logger.debug("Getting episodes for podcast ID: %s", prov_podcast_id)

        # Try to get the podcast name from the current context first
        podcast_name = None
        try:
            podcast = await self.mass.music.podcasts.get_provider_item(
                prov_podcast_id, self.instance_id
            )
            if podcast:
                podcast_name = podcast.name
                self.logger.debug("Got podcast name from MA context: %s", podcast_name)
        except Exception as err:
            self.logger.debug("Could not get podcast from MA context: %s", err)

        # If we don't have the name, get it from the API
        if not podcast_name:
            try:
                podcast_response = await self._api_request(
                    "podcasts/byfeedid", params={"id": prov_podcast_id}
                )
                if podcast_response.get("feed"):
                    podcast_name = podcast_response["feed"].get("title")
                    self.logger.debug("Got podcast name from API fallback: %s", podcast_name)
            except Exception as err:
                self.logger.warning("Could not get podcast name from API: %s", err)

        try:
            response = await self._api_request(
                "episodes/byfeedid", params={"id": prov_podcast_id, "max": 1000}
            )

            episodes = response.get("items", [])
            for idx, episode_data in enumerate(episodes):
                episode = parse_episode_from_data(
                    episode_data,
                    prov_podcast_id,
                    idx,
                    self.instance_id,
                    self.domain,
                    podcast_name,
                )
                if episode:
                    yield episode

        except (ProviderUnavailableError, InvalidDataError):
            # Re-raise these specific errors
            raise
        except Exception as err:
            self.logger.warning(
                "Unexpected error getting episodes for %s: %s", prov_podcast_id, err
            )

    @use_cache(43200)  # Cache for 12 hours
    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """
        Get podcast episode details using direct API lookup.

        Uses the efficient episodes/byid endpoint for direct episode retrieval.
        """
        try:
            podcast_id, episode_id = prov_episode_id.split("|", 1)

            response = await self._api_request("episodes/byid", params={"id": episode_id})
            episode_data = response.get("episode")

            if episode_data:
                episode = parse_episode_from_data(
                    episode_data, podcast_id, 0, self.instance_id, self.domain
                )
                if episode:
                    return episode

        except (ProviderUnavailableError, InvalidDataError):
            # Re-raise these specific errors
            raise
        except ValueError as err:
            # Handle malformed episode ID
            raise InvalidDataError(f"Invalid episode ID format: {prov_episode_id}") from err
        except Exception as err:
            self.logger.warning("Unexpected error getting episode %s: %s", prov_episode_id, err)

        raise MediaNotFoundError(f"Episode {prov_episode_id} not found")

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """
        Get stream details for a podcast episode.

        Uses the Podcast Index episodes/byid endpoint for efficient direct lookup
        rather than fetching all episodes for a podcast.
        """
        if media_type != MediaType.PODCAST_EPISODE:
            raise MediaNotFoundError("Stream details only available for episodes")

        try:
            _, episode_id = item_id.split("|", 1)

            # Use direct episode lookup for efficiency
            response = await self._api_request("episodes/byid", params={"id": episode_id})
            episode_data = response.get("episode")

            if episode_data:
                stream_url = episode_data.get("enclosureUrl")
                if stream_url:
                    return StreamDetails(
                        provider=self.instance_id,
                        item_id=item_id,
                        audio_format=AudioFormat(
                            content_type=ContentType.try_parse(
                                episode_data.get("enclosureType") or "audio/mpeg"
                            ),
                        ),
                        media_type=MediaType.PODCAST_EPISODE,
                        stream_type=StreamType.HTTP,
                        path=stream_url,
                        allow_seek=True,
                    )

        except (ProviderUnavailableError, InvalidDataError):
            # Re-raise these specific errors
            raise
        except ValueError as err:
            # Handle malformed episode ID
            raise InvalidDataError(f"Invalid episode ID format: {item_id}") from err
        except Exception as err:
            self.logger.warning("Unexpected error getting stream for %s: %s", item_id, err)

        raise MediaNotFoundError(f"Stream not found for {item_id}")

    async def get_item(self, media_type: MediaType, prov_item_id: str) -> Podcast | PodcastEpisode:
        """Get single MediaItem from provider."""
        if media_type == MediaType.PODCAST:
            return await self.get_podcast(prov_item_id)
        elif media_type == MediaType.PODCAST_EPISODE:
            return await self.get_podcast_episode(prov_item_id)
        else:
            raise MediaNotFoundError(f"Media type {media_type} not supported by this provider")

    async def _fetch_podcasts(
        self, endpoint: str, params: dict[str, Any] | None = None
    ) -> list[Podcast]:
        """Fetch and parse podcasts from API endpoint."""
        response = await self._api_request(endpoint, params)
        podcasts = []
        for feed_data in response.get("feeds", []):
            podcast = parse_podcast_from_feed(feed_data, self.instance_id, self.domain)
            if podcast:
                podcasts.append(podcast)
        return podcasts

    async def _api_request(
        self, endpoint: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Make authenticated request to Podcast Index API."""
        self.logger.log(
            VERBOSE_LOG_LEVEL, "Making API request to %s with params: %s", endpoint, params
        )
        return await make_api_request(self.mass, self.api_key, self.api_secret, endpoint, params)

    async def _get_feed_url_for_podcast(self, podcast_id: str) -> str | None:
        """Get RSS feed URL for a podcast ID."""
        try:
            response = await self._api_request("podcasts/byfeedid", params={"id": podcast_id})
            feed_data: dict[str, Any] = response.get("feed", {})
            return feed_data.get("url")
        except (ProviderUnavailableError, InvalidDataError):
            # Re-raise these specific errors
            raise
        except Exception as err:
            self.logger.warning(
                "Unexpected error getting feed URL for podcast %s: %s",
                podcast_id,
                err,
                exc_info=True,
            )
            return None

    @use_cache(7200)  # Cache for 2 hours
    async def _browse_trending(self) -> list[Podcast]:
        """Browse trending podcasts."""
        try:
            return await self._fetch_podcasts("podcasts/trending", {"max": 50})
        except (ProviderUnavailableError, InvalidDataError):
            raise
        except Exception as err:
            self.logger.warning(
                "Unexpected error getting trending podcasts: %s", err, exc_info=True
            )
            return []

    @use_cache(14400)  # Cache for 4 hours
    async def _browse_recent_episodes(self) -> list[PodcastEpisode]:
        """Browse recent episodes."""
        try:
            response = await self._api_request("recent/episodes", params={"max": 50})

            episodes = []
            for idx, episode_data in enumerate(response.get("items", [])):
                # Extract podcast ID from episode data
                podcast_id = str(episode_data.get("feedId", ""))
                # Pass feedTitle to avoid unnecessary API calls
                podcast_name = episode_data.get("feedTitle")
                episode = parse_episode_from_data(
                    episode_data,
                    podcast_id,
                    idx,
                    self.instance_id,
                    self.domain,
                    podcast_name,
                )
                if episode:
                    episodes.append(episode)

            return episodes

        except (ProviderUnavailableError, InvalidDataError):
            # Re-raise these specific errors
            raise
        except Exception as err:
            self.logger.warning("Unexpected error getting recent episodes: %s", err, exc_info=True)
            return []

    @use_cache(86400)  # Cache for 24 hours
    async def _browse_categories(self) -> list[BrowseFolder]:
        """Browse podcast categories."""
        try:
            response = await self._api_request("categories/list")

            categories = []
            # Categories API returns feeds array with {id, name} objects
            categories_data = response.get("feeds", [])

            for category in categories_data:
                cat_name = category.get("name", "Unknown Category")

                categories.append(
                    BrowseFolder(
                        item_id=cat_name,  # Use name as ID
                        provider=self.domain,
                        path=f"{self.instance_id}://{BROWSE_CATEGORIES}/{cat_name}",
                        name=cat_name,
                    )
                )

            # Sort by name
            return sorted(categories, key=lambda x: x.name)

        except (ProviderUnavailableError, InvalidDataError):
            # Re-raise these specific errors
            raise
        except Exception as err:
            self.logger.warning("Unexpected error getting categories: %s", err, exc_info=True)
            return []

    @use_cache(43200)  # Cache for 12 hours
    async def _browse_category_podcasts(self, category_name: str) -> list[Podcast]:
        """Browse podcasts in a specific category using search."""
        try:
            # Search for podcasts using the category name directly
            search_response = await self._api_request(
                "search/byterm", params={"q": category_name, "max": 50}
            )

            podcasts = []
            for feed_data in search_response.get("feeds", []):
                podcast = parse_podcast_from_feed(feed_data, self.instance_id, self.domain)
                if podcast:
                    podcasts.append(podcast)

            return podcasts

        except (ProviderUnavailableError, InvalidDataError):
            raise
        except Exception as err:
            self.logger.warning(
                "Unexpected error getting category podcasts: %s", err, exc_info=True
            )
            return []
