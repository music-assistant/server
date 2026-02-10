"""Simple Pocket Casts API client built from scratch."""

from __future__ import annotations

import logging
import types
from typing import Any, cast

import aiohttp

LOGGER = logging.getLogger(__name__)


class PocketCastsAPIError(Exception):
    """Base exception for Pocket Casts API errors."""


class LoginError(PocketCastsAPIError):
    """Login failed."""


class PocketCastsClient:
    """Direct API client for Pocket Casts - no external library needed."""

    BASE_URL = "https://api.pocketcasts.com"

    def __init__(self) -> None:
        """Initialize the client."""
        self.token: str | None = None
        self.user_uuid: str | None = None
        self.session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> PocketCastsClient:
        """Async context manager entry."""
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: types.TracebackType | None,
    ) -> None:
        """Async context manager exit."""
        if self.session:
            await self.session.close()

    async def login(self, email: str, password: str) -> bool:
        """Login and get JWT token."""
        if not self.session:
            self.session = aiohttp.ClientSession()

        try:
            LOGGER.info("Attempting login to Pocket Casts API")

            async with self.session.post(
                f"{self.BASE_URL}/user/login", data={"email": email, "password": password}
            ) as response:
                if response.status != 200:
                    text = await response.text()
                    LOGGER.error("Login failed with status %d: %s", response.status, text)
                    raise LoginError(f"Login failed with status {response.status}")

                data = await response.json()
                self.token = data.get("token")
                self.user_uuid = data.get("uuid")

                if not self.token:
                    raise LoginError("No token in login response")

                LOGGER.info("Successfully logged in to Pocket Casts")
                return True

        except aiohttp.ClientError as err:
            LOGGER.error("Network error during login: %s", err)
            raise LoginError(f"Network error: {err}") from err

    def _headers(self) -> dict[str, str]:
        """Get headers with auth token."""
        if not self.token:
            raise PocketCastsAPIError("Not logged in")
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    async def get_subscribed_podcasts(self) -> list[dict[str, Any]]:
        """Get user's subscribed podcasts."""
        if not self.session:
            raise PocketCastsAPIError("Session not initialized")

        try:
            LOGGER.debug("Fetching subscribed podcasts")

            async with self.session.post(
                f"{self.BASE_URL}/user/podcast/list", headers=self._headers()
            ) as response:
                if response.status != 200:
                    text = await response.text()
                    LOGGER.error("Failed to get podcasts: %d - %s", response.status, text)
                    return []

                data = await response.json()
                podcasts: list[dict[str, Any]] = data.get("podcasts", [])
                LOGGER.info("Retrieved %d subscribed podcasts", len(podcasts))
                return podcasts

        except Exception as err:
            LOGGER.error("Error fetching podcasts: %s", err)
            return []

    async def get_podcast_episodes(self, podcast_uuid: str) -> list[dict[str, Any]]:
        """Get episodes for a specific podcast via API redirect."""
        if not self.session:
            raise PocketCastsAPIError("Session not initialized")

        try:
            LOGGER.debug("Fetching episodes via API redirect for podcast %s", podcast_uuid)

            async with self.session.get(
                f"https://podcast-api.pocketcasts.com/podcast/full/{podcast_uuid}",
                allow_redirects=True,
            ) as response:
                if response.status == 200:
                    data = await response.json()

                    # Episodes are nested inside the podcast object
                    podcast_obj = data.get("podcast", {})
                    episodes: list[dict[str, Any]] = podcast_obj.get("episodes", [])

                    LOGGER.info("Retrieved %d episodes for podcast %s", len(episodes), podcast_uuid)
                    return episodes
                LOGGER.error("Failed to get episodes: %d", response.status)
                return []

        except Exception as err:
            LOGGER.error("Error fetching episodes: %s", err)
            return []

    async def get_in_progress_episodes(self) -> list[dict[str, Any]]:
        """Get episodes currently in progress."""
        if not self.session:
            raise PocketCastsAPIError("Session not initialized")

        try:
            LOGGER.debug("Fetching in-progress episodes")

            async with self.session.post(
                f"{self.BASE_URL}/user/in_progress", headers=self._headers()
            ) as response:
                if response.status != 200:
                    text = await response.text()
                    LOGGER.error("Failed to get in-progress: %d - %s", response.status, text)
                    return []

                data = await response.json()
                episodes: list[dict[str, Any]] = data.get("episodes", [])
                LOGGER.debug("Retrieved %d in-progress episodes", len(episodes))
                return episodes

        except Exception as err:
            LOGGER.error("Error fetching in-progress: %s", err)
            return []

    async def get_up_next_episodes(self) -> dict[str, dict[str, Any]] | list[dict[str, Any]]:
        """Get Up Next queue episodes.

        Note: Returns dict with episode UUIDs as keys, unlike other endpoints that return lists.
        """
        if not self.session:
            raise PocketCastsAPIError("Session not initialized")

        try:
            LOGGER.debug("Fetching Up Next episodes")

            async with self.session.post(
                f"{self.BASE_URL}/up_next/list", headers=self._headers()
            ) as response:
                if response.status != 200:
                    text = await response.text()
                    LOGGER.error("Failed to get up next: %d - %s", response.status, text)
                    return {}

                data = await response.json()
                episodes: dict[str, dict[str, Any]] | list[dict[str, Any]] = data.get(
                    "episodes", {}
                )
                LOGGER.debug("Retrieved %d up next episodes", len(episodes))
                return episodes

        except Exception as err:
            LOGGER.error("Error fetching up next: %s", err)
            return {}

    async def get_new_releases(self) -> list[dict[str, Any]]:
        """Get new release episodes from subscriptions."""
        if not self.session:
            raise PocketCastsAPIError("Session not initialized")

        try:
            LOGGER.debug("Fetching new releases")

            async with self.session.post(
                f"{self.BASE_URL}/user/new_releases", headers=self._headers()
            ) as response:
                if response.status != 200:
                    text = await response.text()
                    LOGGER.error("Failed to get new releases: %d - %s", response.status, text)
                    return []

                data = await response.json()
                episodes: list[dict[str, Any]] = data.get("episodes", [])
                LOGGER.debug("Retrieved %d new release episodes", len(episodes))
                return episodes

        except Exception as err:
            LOGGER.error("Error fetching new releases: %s", err)
            return []

    async def get_starred_episodes(self) -> list[dict[str, Any]]:
        """Get starred/favorited episodes."""
        if not self.session:
            raise PocketCastsAPIError("Session not initialized")

        try:
            LOGGER.debug("Fetching starred episodes")

            async with self.session.post(
                f"{self.BASE_URL}/user/starred", headers=self._headers()
            ) as response:
                if response.status != 200:
                    text = await response.text()
                    LOGGER.error("Failed to get starred: %d - %s", response.status, text)
                    return []

                data = await response.json()
                episodes: list[dict[str, Any]] = data.get("episodes", [])
                LOGGER.debug("Retrieved %d starred episodes", len(episodes))
                return episodes

        except Exception as err:
            LOGGER.error("Error fetching starred: %s", err)
            return []

    async def get_history(self) -> list[dict[str, Any]]:
        """Get listening history episodes."""
        if not self.session:
            raise PocketCastsAPIError("Session not initialized")

        try:
            LOGGER.debug("Fetching history")

            async with self.session.post(
                f"{self.BASE_URL}/user/history", headers=self._headers()
            ) as response:
                if response.status != 200:
                    text = await response.text()
                    LOGGER.error("Failed to get history: %d - %s", response.status, text)
                    return []

                data = await response.json()
                episodes: list[dict[str, Any]] = data.get("episodes", [])
                LOGGER.debug("Retrieved %d history episodes", len(episodes))
                return episodes

        except Exception as err:
            LOGGER.error("Error fetching history: %s", err)
            return []

    async def update_episode_progress(
        self, podcast_uuid: str, episode_uuid: str, position_seconds: int
    ) -> bool:
        """Update playback progress for an episode (status=2, in progress).

        :param podcast_uuid: The podcast UUID.
        :param episode_uuid: The episode UUID.
        :param position_seconds: Current playback position in seconds.
        """
        if not self.session:
            raise PocketCastsAPIError("Session not initialized")

        try:
            LOGGER.debug(
                "Updating progress for episode %s to %d seconds", episode_uuid, position_seconds
            )

            async with self.session.post(
                f"{self.BASE_URL}/sync/update_episode",
                headers=self._headers(),
                json={
                    "uuid": episode_uuid,
                    "podcast": podcast_uuid,
                    "status": 2,  # 2=in_progress
                    "position": str(position_seconds),
                },
            ) as response:
                success = response.status == 200
                if not success:
                    text = await response.text()
                    LOGGER.error("Failed to update progress: %d - %s", response.status, text)

                return success

        except Exception as err:
            LOGGER.error("Error updating progress: %s", err)
            return False

    async def mark_episode_played(self, podcast_uuid: str, episode_uuid: str) -> bool:
        """Mark an episode as played (status=3).

        :param podcast_uuid: The podcast UUID.
        :param episode_uuid: The episode UUID.
        """
        if not self.session:
            raise PocketCastsAPIError("Session not initialized")

        try:
            LOGGER.debug("Marking episode %s as played", episode_uuid)

            async with self.session.post(
                f"{self.BASE_URL}/sync/update_episode",
                headers=self._headers(),
                json={
                    "uuid": episode_uuid,
                    "podcast": podcast_uuid,
                    "status": 3,  # 3=played
                },
            ) as response:
                success = response.status == 200
                if not success:
                    text = await response.text()
                    LOGGER.error("Failed to mark as played: %d - %s", response.status, text)

                return success

        except Exception as err:
            LOGGER.error("Error marking episode as played: %s", err)
            return False

    async def mark_episode_unplayed(self, podcast_uuid: str, episode_uuid: str) -> bool:
        """Mark an episode as unplayed (status=1, position=0).

        :param podcast_uuid: The podcast UUID.
        :param episode_uuid: The episode UUID.
        """
        if not self.session:
            raise PocketCastsAPIError("Session not initialized")

        try:
            LOGGER.debug("Marking episode %s as unplayed", episode_uuid)

            async with self.session.post(
                f"{self.BASE_URL}/sync/update_episode",
                headers=self._headers(),
                json={
                    "uuid": episode_uuid,
                    "podcast": podcast_uuid,
                    "status": 1,  # 1=unplayed
                    "position": "0",
                },
            ) as response:
                success = response.status == 200
                if not success:
                    text = await response.text()
                    LOGGER.error("Failed to mark as unplayed: %d - %s", response.status, text)

                return success

        except Exception as err:
            LOGGER.error("Error marking episode as unplayed: %s", err)
            return False

    async def archive_episode(
        self, podcast_uuid: str, episode_uuid: str, archive: bool = True
    ) -> bool:
        """Archive or unarchive an episode.

        :param podcast_uuid: The podcast UUID.
        :param episode_uuid: The episode UUID.
        :param archive: True to archive, False to unarchive.
        """
        if not self.session:
            raise PocketCastsAPIError("Session not initialized")

        try:
            LOGGER.debug("Setting archive=%s for episode %s", archive, episode_uuid)

            async with self.session.post(
                f"{self.BASE_URL}/sync/update_episodes_archive",
                headers=self._headers(),
                json={
                    "episodes": [{"uuid": episode_uuid, "podcast": podcast_uuid}],
                    "archive": archive,
                },
            ) as response:
                success = response.status == 200
                if not success:
                    text = await response.text()
                    LOGGER.error("Failed to archive episode: %d - %s", response.status, text)

                return success

        except Exception as err:
            LOGGER.error("Error archiving episode: %s", err)
            return False

    async def remove_from_up_next(self, episode_uuid: str) -> bool:
        """Remove an episode from the Up Next queue.

        :param episode_uuid: The episode UUID to remove.
        """
        if not self.session:
            raise PocketCastsAPIError("Session not initialized")

        try:
            LOGGER.debug("Removing episode %s from Up Next", episode_uuid)

            async with self.session.post(
                f"{self.BASE_URL}/up_next/remove",
                headers=self._headers(),
                json={
                    "version": 2,
                    "uuids": [episode_uuid],
                },
            ) as response:
                success = response.status == 200
                if not success:
                    text = await response.text()
                    LOGGER.error("Failed to remove from Up Next: %d - %s", response.status, text)

                return success

        except Exception as err:
            LOGGER.error("Error removing from Up Next: %s", err)
            return False

    async def play_now(
        self,
        episode_uuid: str,
        podcast_uuid: str,
        title: str,
        url: str,
        published: str | None = None,
    ) -> bool:
        """Add episode to Up Next at "play now" position (top of queue).

        This is called when starting playback of an episode to sync with Pocketcasts.

        :param episode_uuid: The episode UUID.
        :param podcast_uuid: The podcast UUID.
        :param title: The episode title.
        :param url: The episode audio URL.
        :param published: The episode publish date (ISO format), optional.
        """
        if not self.session:
            raise PocketCastsAPIError("Session not initialized")

        try:
            LOGGER.debug("Adding episode %s to Up Next (play now)", episode_uuid)

            episode_data: dict[str, Any] = {
                "uuid": episode_uuid,
                "podcast": podcast_uuid,
                "title": title,
                "url": url,
            }
            if published:
                episode_data["published"] = published

            async with self.session.post(
                f"{self.BASE_URL}/up_next/play_now",
                headers=self._headers(),
                json={
                    "version": 2,
                    "episode": episode_data,
                },
            ) as response:
                success = response.status == 200
                if not success:
                    text = await response.text()
                    LOGGER.error("Failed to add to Up Next: %d - %s", response.status, text)

                return success

        except Exception as err:
            LOGGER.error("Error adding to Up Next: %s", err)
            return False

    async def get_episode_details(self, episode_uuid: str) -> dict[str, Any] | None:
        """Get detailed episode info including correct duration and playback status.

        This endpoint returns accurate data including:
        - duration: Correct episode length in seconds
        - playingStatus: 1=unplayed, 2=in_progress, 3=played
        - playedUpTo: Resume position in seconds
        - starred: Whether episode is starred

        :param episode_uuid: The episode UUID.
        """
        if not self.session:
            raise PocketCastsAPIError("Session not initialized")

        try:
            LOGGER.debug("Fetching episode details for %s", episode_uuid)

            async with self.session.post(
                f"{self.BASE_URL}/user/episode",
                headers=self._headers(),
                json={"uuid": episode_uuid},
            ) as response:
                if response.status == 200:
                    data: dict[str, Any] = await response.json()
                    LOGGER.debug(
                        "Episode %s: duration=%s, status=%s, playedUpTo=%s",
                        episode_uuid,
                        data.get("duration"),
                        data.get("playingStatus"),
                        data.get("playedUpTo"),
                    )
                    return data

                text = await response.text()
                LOGGER.error("Failed to get episode details: %d - %s", response.status, text)
                return None

        except Exception as err:
            LOGGER.error("Error fetching episode details: %s", err)
            return None

    async def search_podcasts(self, query: str) -> list[dict[str, Any]]:
        """Search for podcasts."""
        if not self.session:
            raise PocketCastsAPIError("Session not initialized")

        try:
            LOGGER.debug("Searching for podcasts: %s", query)

            # Try api domain first
            async with self.session.post(
                f"{self.BASE_URL}/discover/search", headers=self._headers(), json={"term": query}
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    podcasts: list[dict[str, Any]] = data.get("podcasts", [])
                    LOGGER.info("Found %d podcasts for query '%s'", len(podcasts), query)
                    return podcasts
                text = await response.text()
                LOGGER.error("Search failed: %d - %s", response.status, text)
                return []

        except Exception as err:
            LOGGER.error("Error searching: %s", err)
            return []

    async def get_podcast_details(self, podcast_uuid: str) -> dict[str, Any] | None:
        """Get details for any podcast by UUID (not just subscribed)."""
        if not self.session:
            raise PocketCastsAPIError("Session not initialized")

        try:
            LOGGER.debug("Fetching podcast details for %s", podcast_uuid)

            # Try multiple possible endpoints
            endpoints = [
                (f"{self.BASE_URL}/discover/podcast", {"uuid": podcast_uuid}),
                (f"{self.BASE_URL}/podcast/full/{podcast_uuid}", {}),
            ]

            for endpoint, data in endpoints:
                async with self.session.post(
                    endpoint, headers=self._headers(), json=data if data else None
                ) as response:
                    LOGGER.debug("Trying %s: status %d", endpoint, response.status)

                    if response.status == 200:
                        result = await response.json()
                        podcast_data = result.get("podcast")
                        if podcast_data is not None:
                            return cast("dict[str, Any]", podcast_data)
                        return None

            LOGGER.debug("All podcast detail endpoints returned 404")
            return None

        except Exception as err:
            LOGGER.error("Error fetching podcast details: %s", err)
            return None

    async def subscribe_podcast(self, podcast_uuid: str) -> bool:
        """Subscribe to a podcast.

        :param podcast_uuid: The UUID of the podcast to subscribe to.
        """
        if not self.session:
            raise PocketCastsAPIError("Session not initialized")

        try:
            LOGGER.debug("Subscribing to podcast %s", podcast_uuid)

            async with self.session.post(
                f"{self.BASE_URL}/user/podcast/subscribe",
                headers=self._headers(),
                json={"uuid": podcast_uuid},
            ) as response:
                success = response.status == 200
                if success:
                    LOGGER.info("Successfully subscribed to podcast %s", podcast_uuid)
                else:
                    text = await response.text()
                    LOGGER.error(
                        "Failed to subscribe: %d - %s - Response: %s",
                        response.status,
                        response.reason,
                        text,
                    )

                return success

        except Exception as err:
            LOGGER.error("Error subscribing to podcast: %s", err)
            return False

    async def unsubscribe_podcast(self, podcast_uuid: str) -> bool:
        """Unsubscribe from a podcast.

        :param podcast_uuid: The UUID of the podcast to unsubscribe from.
        """
        if not self.session:
            raise PocketCastsAPIError("Session not initialized")

        try:
            LOGGER.debug("Unsubscribing from podcast %s", podcast_uuid)

            async with self.session.post(
                f"{self.BASE_URL}/user/podcast/unsubscribe",
                headers=self._headers(),
                json={"uuid": podcast_uuid},
            ) as response:
                success = response.status == 200
                if success:
                    LOGGER.info("Successfully unsubscribed from podcast %s", podcast_uuid)
                else:
                    text = await response.text()
                    LOGGER.error("Failed to unsubscribe: %d - %s", response.status, text)

                return success

        except Exception as err:
            LOGGER.error("Error unsubscribing from podcast: %s", err)
            return False
