"""ListenBrainz API client for playlist data."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    import logging

    from aiohttp import ClientSession

    from music_assistant.providers.listenbrainz import ListenBrainzProvider
from music_assistant.helpers.throttle_retry import ThrottlerManager


class ListenBrainzAPIClient:
    """Client for the ListenBrainz playlist API."""

    BASE_URL = "https://api.listenbrainz.org"
    throttler = ThrottlerManager(rate_limit=1, period=1)  # 1 request per second

    def __init__(self, provider: ListenBrainzProvider) -> None:
        """
        Initialize the ListenBrainz API client.

        :param provider: The ListenBrainz provider instance.
        """
        self.provider = provider
        self.http_session: ClientSession = provider.mass.http_session

    @property
    def logger(self) -> logging.Logger:
        """Return the provider's active logger."""
        return self.provider.logger

    async def get_createdfor_playlists(self, username: str) -> list[dict[str, Any]]:
        """
        Get all system-generated playlists for a user.

        :param username: ListenBrainz username.
        """
        self.logger.debug("Fetching ListenBrainz playlists for user %s", username)
        path = f"/1/user/{username}/playlists/createdfor"
        playlists: list[dict[str, Any]] = []
        offset = 0
        try:
            while True:
                data = await self._get_data(path if offset == 0 else f"{path}?offset={offset}")
                page_playlists = data.get("playlists", [])
                playlists.extend(item["playlist"] for item in page_playlists)

                page_count = cast("int", data["count"])
                page_offset = cast("int", data["offset"])
                playlist_count = cast("int", data["playlist_count"])
                offset = page_offset + page_count

                self.logger.debug(
                    "Fetched %d ListenBrainz playlists at offset %d (%d of %d)",
                    len(page_playlists),
                    page_offset,
                    len(playlists),
                    playlist_count,
                )

                if page_count == 0 or offset >= playlist_count:
                    return playlists
        except Exception as err:
            self.logger.error(
                "Error fetching ListenBrainz playlists for user %s: %s", username, err
            )
            raise

    async def get_playlist(self, playlist_mbid: str) -> dict[str, Any]:
        """
        Return one playlist, including its recordings.

        :param playlist_mbid: ListenBrainz playlist MusicBrainz ID.
        """
        data = await self._get_data(f"/1/playlist/{playlist_mbid}")
        return cast("dict[str, Any]", data["playlist"])

    async def _get_data(self, path: str) -> dict[str, Any]:
        """
        Make a request to the ListenBrainz API and return the response data.

        :param path: API path relative to :attr:`BASE_URL`.
        """
        async with (
            self.throttler.acquire(),
            self.http_session.get(f"{self.BASE_URL}{path}") as response,
        ):
            response.raise_for_status()
            return cast("dict[str, Any]", await response.json())
