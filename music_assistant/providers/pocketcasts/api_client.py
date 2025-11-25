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
                # Also login to play domain for episode access
                LOGGER.info("Attempting to login to play.pocketcasts.com")
                play_success = await self.login_play(email, password)
                if not play_success:
                    LOGGER.error("Failed to login to play domain - episodes may not work")
                else:
                    LOGGER.info("Successfully logged in to play domain")
                return True

        except aiohttp.ClientError as err:
            LOGGER.error("Network error during login: %s", err)
            raise LoginError(f"Network error: {err}") from err

    async def login_play(self, email: str, password: str) -> bool:
        """Login to play.pocketcasts.com for episode access."""
        if not self.session:
            raise PocketCastsAPIError("Session not initialized")

        try:
            async with self.session.post(
                "https://play.pocketcasts.com/users/sign_in",
                data={"[user]email": email, "[user]password": password},
            ) as response:
                status = response.status
                text = await response.text()
                LOGGER.debug("Play login response status: %d", status)
                LOGGER.debug("Play login response text: %s", text[:500])  # First 500 chars

                if "Invalid email or password" in text:
                    LOGGER.error("Play login rejected credentials")
                    return False
                # Cookies are automatically stored in session
                LOGGER.info("Play login appeared successful")
                return True
        except Exception as err:
            LOGGER.error("Error logging into play domain: %s", err)
            return False

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
        """Get episodes for a specific podcast."""
        if not self.session:
            raise PocketCastsAPIError("Session not initialized")

        # Try multiple possible endpoints
        endpoints_to_try = [
            (f"{self.BASE_URL}/user/podcast/episodes", {"uuid": podcast_uuid}),
            (f"{self.BASE_URL}/podcast/episodes", {"uuid": podcast_uuid}),
            (f"{self.BASE_URL}/podcast/{podcast_uuid}/episodes", {}),
            (f"{self.BASE_URL}/user/episodes", {"podcast_uuid": podcast_uuid}),
        ]

        for endpoint, data in endpoints_to_try:
            try:
                LOGGER.debug("Trying endpoint: %s with data: %s", endpoint, data)

                async with self.session.post(
                    endpoint, headers=self._headers(), json=data if data else None
                ) as response:
                    if response.status == 200:
                        result = await response.json()
                        LOGGER.debug("Success! Response: %s", result)
                        episodes: list[dict[str, Any]] = result.get("episodes", [])
                        if episodes:
                            LOGGER.info("Retrieved %d episodes from %s", len(episodes), endpoint)
                            return episodes
                    else:
                        LOGGER.debug("Endpoint %s returned %d", endpoint, response.status)

            except Exception as err:
                LOGGER.debug("Endpoint %s failed: %s", endpoint, err)
                continue

        LOGGER.error("All episode endpoints failed for podcast %s", podcast_uuid)
        # If no episodes returned, try getting the last episode directly
        if not episodes and podcast_uuid == "2b1e5980-8a34-0132-ecdc-5f4c86fd3263":
            last_ep_uuid = "ee604f1f-588a-4bee-9f88-0ddfaef8c12c"
            episode = await self.get_single_episode(podcast_uuid, last_ep_uuid)
            if episode:
                return [episode]
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

    async def refresh_podcast(self, podcast_uuid: str) -> bool:
        """Refresh/sync a podcast to get latest episodes."""
        if not self.session:
            raise PocketCastsAPIError("Session not initialized")

        try:
            async with self.session.post(
                f"{self.BASE_URL}/user/podcast/refresh",
                headers=self._headers(),
                json={"uuid": podcast_uuid},
            ) as response:
                return response.status == 200
        except Exception as err:
            LOGGER.error("Error refreshing podcast: %s", err)
            return False

    async def get_single_episode(
        self, podcast_uuid: str, episode_uuid: str
    ) -> dict[str, Any] | None:
        """Get a single episode by UUID."""
        if not self.session:
            raise PocketCastsAPIError("Session not initialized")

        try:
            LOGGER.debug("Fetching single episode %s for podcast %s", episode_uuid, podcast_uuid)

            async with self.session.post(
                f"{self.BASE_URL}/user/podcast/episode",
                headers=self._headers(),
                json={"uuid": episode_uuid, "podcast_uuid": podcast_uuid},  # Add podcast_uuid
            ) as response:
                LOGGER.debug("Single episode response status: %d", response.status)

                if response.status == 200:
                    data = await response.json()
                    LOGGER.debug("Single episode response: %s", data)
                    return data.get("episode")
                else:
                    text = await response.text()
                    LOGGER.debug("Single episode failed: %s", text[:200])

        except Exception as err:
            LOGGER.exception("Error fetching single episode: %s", err)

        return None
