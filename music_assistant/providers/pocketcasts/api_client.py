"""Simple Pocket Casts API client built from scratch."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

LOGGER = logging.getLogger(__name__)


class PocketCastsAPIError(Exception):
    """Base exception for Pocket Casts API errors."""


class LoginError(PocketCastsAPIError):
    """Login failed."""


class PocketCastsClient:
    """Direct API client for Pocket Casts - no external library needed."""

    BASE_URL = "https://api.pocketcasts.com"
    PLAY_URL = "https://play.pocketcasts.com"

    def __init__(self) -> None:
        """Initialize the client."""
        self.token: str | None = None
        self.user_uuid: str | None = None
        self.session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> PocketCastsClient:
        """Async context manager entry."""
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
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
                allow_redirects=True,  # Explicitly enable redirects
            ) as response:
                LOGGER.debug("Response status: %d", response.status)
                LOGGER.debug("Response URL: %s", response.url)
                LOGGER.debug("Response headers: %s", dict(response.headers))

                if response.status == 200:
                    text = await response.text()
                    LOGGER.debug("Response text length: %d", len(text))
                    LOGGER.debug("First 500 chars: %s", text[:500])

                    data = await response.json()
                    LOGGER.debug("JSON keys: %s", list(data.keys()))

                    # Episodes are at root level, not nested
                    episodes: list[dict[str, Any]] = data.get("episodes", [])
                    LOGGER.info("Retrieved %d episodes for podcast %s", len(episodes), podcast_uuid)
                    return episodes
                else:
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

    async def update_episode_progress(
        self, podcast_uuid: str, episode_uuid: str, position_seconds: int, duration_seconds: int
    ) -> bool:
        """Update playback progress for an episode."""
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
                    "podcast": podcast_uuid,
                    "episode": episode_uuid,
                    "position": position_seconds,
                    "duration": duration_seconds,
                    "status": 2
                    if position_seconds >= duration_seconds
                    else 1,  # 2=played, 1=in_progress
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
                    LOGGER.debug("Search response: %s", data)
                    podcasts: list[dict[str, Any]] = data.get("podcasts", [])
                    LOGGER.info("Found %d podcasts for query '%s'", len(podcasts), query)
                    return podcasts
                else:
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
                        LOGGER.debug("Got podcast details: %s", result)
                        return result.get("podcast")

            LOGGER.debug("All podcast detail endpoints returned 404")
            return None

        except Exception as err:
            LOGGER.error("Error fetching podcast details: %s", err)
            return None
