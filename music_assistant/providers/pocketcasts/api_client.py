"""API client for the Pocket Casts service."""

from __future__ import annotations

import logging
from typing import Any, cast

import aiohttp
from music_assistant_models.errors import (
    LoginFailed,
    ProviderUnavailableError,
    ResourceTemporarilyUnavailable,
)

from music_assistant.helpers.json import json_loads
from music_assistant.helpers.throttle_retry import (
    ThrottlerManager,
    parse_retry_after,
    throttle_with_retries,
)

API_BASE_URL = "https://api.pocketcasts.com"
PODCAST_API_URL = "https://podcast-api.pocketcasts.com"


class PocketCastsClient:
    """Client for the Pocket Casts API."""

    throttler = ThrottlerManager(rate_limit=5, period=1)

    def __init__(self, session: aiohttp.ClientSession, logger: logging.Logger) -> None:
        """
        Initialize the client.

        :param session: The aiohttp session to use for requests (typically mass.http_session).
        :param logger: The provider logger, used for throttle/retry messages.
        """
        self.token: str | None = None
        self.user_uuid: str | None = None
        self.session = session
        self.logger = logger

    async def login(self, email: str, password: str) -> None:
        """
        Authenticate with Pocket Casts and store the session token.

        :param email: The account email address.
        :param password: The account password.
        """
        data = await self._request(
            "POST",
            f"{API_BASE_URL}/user/login",
            auth=False,
            data={"email": email, "password": password},
        )
        self.token = data.get("token")
        self.user_uuid = data.get("uuid")
        if not self.token:
            raise LoginFailed("No token in Pocket Casts login response")
        self.logger.info("Successfully logged in to Pocket Casts")

    async def get_subscribed_podcasts(self) -> list[dict[str, Any]]:
        """Return the user's subscribed podcasts."""
        data = await self._request("POST", f"{API_BASE_URL}/user/podcast/list")
        podcasts: list[dict[str, Any]] = data.get("podcasts", [])
        self.logger.debug("Retrieved %d subscribed podcasts", len(podcasts))
        return podcasts

    async def get_podcast(self, podcast_uuid: str) -> dict[str, Any]:
        """
        Return full details (including episodes) for a podcast by UUID.

        :param podcast_uuid: The podcast UUID.
        """
        data = await self._request(
            "GET",
            f"{PODCAST_API_URL}/podcast/full/{podcast_uuid}",
            auth=False,
            allow_redirects=True,
        )
        podcast: dict[str, Any] = data.get("podcast", {})
        return podcast

    async def get_podcast_episodes(self, podcast_uuid: str) -> tuple[str, list[dict[str, Any]]]:
        """
        Return a podcast's title and all of its episodes.

        :param podcast_uuid: The podcast UUID.
        """
        podcast = await self.get_podcast(podcast_uuid)
        # full-podcast episodes use snake_case keys: uuid, title, url, file_type, file_size,
        # duration (seconds), published, type, slug, has_generated_transcript. Note this is a
        # different (leaner) schema than the /user/episode endpoint - no playback status,
        # episode number, show notes or artwork.
        episodes: list[dict[str, Any]] = podcast.get("episodes", [])
        self.logger.debug("Retrieved %d episodes for podcast %s", len(episodes), podcast_uuid)
        return str(podcast.get("title", "")), episodes

    async def get_show_notes(self, podcast_uuid: str) -> dict[str, dict[str, Any]]:
        """
        Return the show notes and artwork, keyed by episode UUID, for a podcast.

        Episodes carrying neither are left out.

        :param podcast_uuid: The podcast UUID.
        """
        data = await self._request(
            "GET",
            f"{PODCAST_API_URL}/mobile/show_notes/full/{podcast_uuid}",
            auth=False,
            allow_redirects=True,
        )
        # one call covers every episode, and no other endpoint carries these two fields. The
        # rest of the response is dropped here to keep the cached entry small.
        show_notes: dict[str, dict[str, Any]] = {}
        for episode in data.get("podcast", {}).get("episodes", []):
            if not (uuid := episode.get("uuid")):
                continue
            details: dict[str, Any] = {}
            if description := episode.get("show_notes"):
                details["description"] = description
            if image := episode.get("image"):
                details["image"] = image
            if details:
                show_notes[uuid] = details
        self.logger.debug(
            "Retrieved show notes for %d episodes of podcast %s", len(show_notes), podcast_uuid
        )
        return show_notes

    async def get_in_progress_episodes(self) -> list[dict[str, Any]]:
        """Return episodes currently in progress."""
        data = await self._request("POST", f"{API_BASE_URL}/user/in_progress")
        episodes: list[dict[str, Any]] = data.get("episodes", [])
        self.logger.debug("Retrieved %d in-progress episodes", len(episodes))
        return episodes

    async def get_up_next_episodes(self) -> list[dict[str, Any]]:
        """Return the Up Next queue episodes."""
        data = await self._request("POST", f"{API_BASE_URL}/up_next/list")
        episodes = data.get("episodes", [])
        # the up_next endpoint returns a uuid-keyed map; normalise to a list carrying the uuid
        if isinstance(episodes, dict):
            return [{"uuid": uuid, **episode} for uuid, episode in episodes.items()]
        return cast("list[dict[str, Any]]", episodes)

    async def get_new_releases(self) -> list[dict[str, Any]]:
        """Return new release episodes from subscriptions."""
        data = await self._request("POST", f"{API_BASE_URL}/user/new_releases")
        episodes: list[dict[str, Any]] = data.get("episodes", [])
        self.logger.debug("Retrieved %d new release episodes", len(episodes))
        return episodes

    async def get_starred_episodes(self) -> list[dict[str, Any]]:
        """Return starred episodes."""
        data = await self._request("POST", f"{API_BASE_URL}/user/starred")
        episodes: list[dict[str, Any]] = data.get("episodes", [])
        self.logger.debug("Retrieved %d starred episodes", len(episodes))
        return episodes

    async def get_history(self) -> list[dict[str, Any]]:
        """Return listening history episodes."""
        data = await self._request("POST", f"{API_BASE_URL}/user/history")
        episodes: list[dict[str, Any]] = data.get("episodes", [])
        self.logger.debug("Retrieved %d history episodes", len(episodes))
        return episodes

    async def get_episode_details(self, episode_uuid: str) -> dict[str, Any]:
        """
        Return detailed episode info including correct duration and playback status.

        :param episode_uuid: The episode UUID.
        """
        # /user/episode returns camelCase keys: uuid, title, url, fileType, duration (seconds),
        # published, episodeNumber, playedUpTo (resume seconds), playingStatus (1=unplayed,
        # 2=in progress, 3=played), starred, podcastUuid. No show notes or episode artwork.
        data = await self._request(
            "POST", f"{API_BASE_URL}/user/episode", json={"uuid": episode_uuid}
        )
        self.logger.debug(
            "Episode %s: duration=%s, status=%s, playedUpTo=%s",
            episode_uuid,
            data.get("duration"),
            data.get("playingStatus"),
            data.get("playedUpTo"),
        )
        return data

    async def search_podcasts(self, query: str) -> list[dict[str, Any]]:
        """
        Search for podcasts.

        :param query: The search term.
        """
        data = await self._request("POST", f"{API_BASE_URL}/discover/search", json={"term": query})
        podcasts: list[dict[str, Any]] = data.get("podcasts", [])
        self.logger.debug("Found %d podcasts for query '%s'", len(podcasts), query)
        return podcasts

    async def update_episode_progress(
        self, podcast_uuid: str, episode_uuid: str, position_seconds: int
    ) -> None:
        """
        Update playback progress for an episode (marks it in progress).

        :param podcast_uuid: The podcast UUID.
        :param episode_uuid: The episode UUID.
        :param position_seconds: Current playback position in seconds.
        """
        await self._request(
            "POST",
            f"{API_BASE_URL}/sync/update_episode",
            json={
                "uuid": episode_uuid,
                "podcast": podcast_uuid,
                "status": 2,  # 2=in_progress
                "position": str(position_seconds),
            },
        )

    async def mark_episode_played(self, podcast_uuid: str, episode_uuid: str) -> None:
        """
        Mark an episode as played.

        :param podcast_uuid: The podcast UUID.
        :param episode_uuid: The episode UUID.
        """
        await self._request(
            "POST",
            f"{API_BASE_URL}/sync/update_episode",
            json={"uuid": episode_uuid, "podcast": podcast_uuid, "status": 3},  # 3=played
        )

    async def mark_episode_unplayed(self, podcast_uuid: str, episode_uuid: str) -> None:
        """
        Mark an episode as unplayed and reset its position.

        :param podcast_uuid: The podcast UUID.
        :param episode_uuid: The episode UUID.
        """
        await self._request(
            "POST",
            f"{API_BASE_URL}/sync/update_episode",
            json={
                "uuid": episode_uuid,
                "podcast": podcast_uuid,
                "status": 1,  # 1=unplayed
                "position": "0",
            },
        )

    async def archive_episode(
        self, podcast_uuid: str, episode_uuid: str, archive: bool = True
    ) -> None:
        """
        Archive or unarchive an episode.

        :param podcast_uuid: The podcast UUID.
        :param episode_uuid: The episode UUID.
        :param archive: True to archive, False to unarchive.
        """
        await self._request(
            "POST",
            f"{API_BASE_URL}/sync/update_episodes_archive",
            json={
                "episodes": [{"uuid": episode_uuid, "podcast": podcast_uuid}],
                "archive": archive,
            },
        )

    async def remove_from_up_next(self, episode_uuid: str) -> None:
        """
        Remove an episode from the Up Next queue.

        :param episode_uuid: The episode UUID to remove.
        """
        await self._request(
            "POST",
            f"{API_BASE_URL}/up_next/remove",
            json={"version": 2, "uuids": [episode_uuid]},
        )

    async def play_now(
        self,
        episode_uuid: str,
        podcast_uuid: str,
        title: str,
        url: str,
        published: str | None = None,
    ) -> None:
        """
        Add an episode to the top of the Up Next queue.

        :param episode_uuid: The episode UUID.
        :param podcast_uuid: The podcast UUID.
        :param title: The episode title.
        :param url: The episode audio URL.
        :param published: The episode publish date (ISO format), optional.
        """
        episode: dict[str, Any] = {
            "uuid": episode_uuid,
            "podcast": podcast_uuid,
            "title": title,
            "url": url,
        }
        if published:
            episode["published"] = published
        await self._request(
            "POST", f"{API_BASE_URL}/up_next/play_now", json={"version": 2, "episode": episode}
        )

    async def add_to_history(
        self,
        episode_uuid: str,
        podcast_uuid: str,
        title: str,
        url: str,
        published: str | None = None,
    ) -> None:
        """
        Record an episode in the listening history.

        :param episode_uuid: The episode UUID.
        :param podcast_uuid: The podcast UUID.
        :param title: The episode title.
        :param url: The episode audio URL.
        :param published: The episode publish date (ISO format), optional.
        """
        payload: dict[str, Any] = {
            "action": 1,
            "podcast": podcast_uuid,
            "episode": episode_uuid,
            "title": title,
            "url": url,
        }
        if published:
            payload["published"] = published
        await self._request("POST", f"{API_BASE_URL}/history/do", json=payload)

    async def subscribe_podcast(self, podcast_uuid: str) -> None:
        """
        Subscribe to a podcast.

        :param podcast_uuid: The UUID of the podcast to subscribe to.
        """
        await self._request(
            "POST", f"{API_BASE_URL}/user/podcast/subscribe", json={"uuid": podcast_uuid}
        )

    async def unsubscribe_podcast(self, podcast_uuid: str) -> None:
        """
        Unsubscribe from a podcast.

        :param podcast_uuid: The UUID of the podcast to unsubscribe from.
        """
        await self._request(
            "POST", f"{API_BASE_URL}/user/podcast/unsubscribe", json={"uuid": podcast_uuid}
        )

    def _headers(self) -> dict[str, str]:
        if not self.token:
            raise LoginFailed("Not logged in to Pocket Casts")
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    @throttle_with_retries
    async def _request(
        self, method: str, url: str, *, auth: bool = True, **kwargs: Any
    ) -> dict[str, Any]:
        """
        Perform a request against the Pocket Casts API and return the decoded JSON body.

        :param method: The HTTP method to use.
        :param url: The full request URL.
        :param auth: Whether to send the authorization header.
        """
        headers = self._headers() if auth else None
        try:
            async with self.session.request(method, url, headers=headers, **kwargs) as response:
                if response.status in (401, 403):
                    raise LoginFailed(f"Pocket Casts authentication failed ({response.status})")
                if response.status == 429 or response.status >= 500:
                    # transient: let the throttler back off and retry
                    raise ResourceTemporarilyUnavailable(
                        f"Pocket Casts temporarily unavailable ({response.status})",
                        backoff_time=parse_retry_after(response.headers.get("Retry-After")),
                    )
                if response.status != 200:
                    text = await response.text()
                    raise ProviderUnavailableError(
                        f"Pocket Casts request to {url} failed ({response.status}): {text}"
                    )
                return cast("dict[str, Any]", await response.json(loads=json_loads))
        except aiohttp.ClientError as err:
            raise ResourceTemporarilyUnavailable(
                f"Network error contacting Pocket Casts: {err}"
            ) from err
