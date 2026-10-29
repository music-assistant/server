"""ListenBrainz API client for playlist data."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    import logging

    from aiohttp import ClientSession

    from music_assistant.providers.listenbrainz import ListenBrainzProvider


_SAMPLE_RESPONSE: dict[str, Any] = json.loads(
    Path(__file__).with_name("sample_playlist.json").read_text(encoding="utf-8")
)


class ListenBrainzAPIClient:
    """Client for the ListenBrainz playlist API."""

    BASE_URL = "https://api.listenbrainz.org"

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

    async def get_recommendation_playlists(self, username: str) -> list[dict[str, Any]]:
        """
        Return recommendation-playlist metadata for a user.

        :param username: ListenBrainz username.
        """
        data = await self._get_data(f"/1/user/{username}/playlists/recommendations")
        return cast("list[dict[str, Any]]", data["playlists"])

    async def get_playlist(self, playlist_mbid: str) -> dict[str, Any]:
        """
        Return one playlist, including its recordings.

        :param playlist_mbid: ListenBrainz playlist MusicBrainz ID.
        """
        data = await self._get_data(f"/1/playlist/{playlist_mbid}")
        return cast("dict[str, Any]", data["playlist"])

    async def _get_data(self, path: str) -> dict[str, Any]:
        """
        Return a mocked ListenBrainz response for the requested API path.

        This is the transport seam: replace the fixture return with a request through
        ``self.http_session`` when live API access is introduced.

        :param path: API path relative to :attr:`BASE_URL`.
        """
        self.logger.debug("Fetching ListenBrainz API path %s (mock)", path)
        data = copy.deepcopy(_SAMPLE_RESPONSE)
        if path.endswith("/playlists/recommendations"):
            playlist = data["playlist"]
            playlist.pop("track", None)
            return {"playlists": [playlist]}
        return data
