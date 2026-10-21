"""MusicBrainz API client."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from music_assistant_models.errors import RateLimited, ResourceTemporarilyUnavailable

from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.json import json_loads
from music_assistant.helpers.throttle_retry import (
    ThrottlerManager,
    parse_retry_after,
    throttle_with_retries,
)

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

MB_BASE_URL = "https://musicbrainz-mirror.music-assistant.io/ws/2"


class MusicBrainzAPIClient:
    """Thin HTTP client for the MusicBrainz API."""

    domain = "musicbrainz"
    throttler = ThrottlerManager(rate_limit=30, period=10)

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize the API client."""
        self.mass = mass
        self.logger = logging.getLogger(__name__)

    @use_cache(86400 * 30)  # Cache for 30 days
    @throttle_with_retries
    async def get_data(self, /, endpoint: str, **kwargs: str) -> Any:
        """
        Fetch data from the MusicBrainz API.

        Results are cached for 30 days.

        :param endpoint: API endpoint path (e.g. ``artist/123?inc=aliases``).
        :param kwargs: Additional query parameters forwarded as URL params.
        """
        return await self._request(endpoint, **kwargs)

    @use_cache(86400)  # an unknown URL is asked again after a day
    async def get_url_data(self, /, resource: str, inc: str) -> Any:
        """
        Fetch what MusicBrainz links to a URL.

        A known URL is cached for 30 days, an unknown one for a day, so a link a user adds on
        MusicBrainz is picked up the next day.

        :param resource: The URL to look up, e.g. ``https://www.deezer.com/artist/805``.
        :param inc: The relations to include, e.g. ``artist-rels``.
        """
        return await self._get_known_url_data(resource=resource, inc=inc)

    @use_cache(86400 * 7)  # Cache for 7 days
    @throttle_with_retries
    async def get_browse_data(self, /, endpoint: str, **kwargs: str) -> Any:
        """
        Fetch a listing from the MusicBrainz API that grows as the catalog does.

        Results are cached for 7 days, so a discography picks up new releases within a week.

        :param endpoint: API endpoint path (e.g. ``release-group``).
        :param kwargs: Additional query parameters forwarded as URL params.
        """
        return await self._request(endpoint, **kwargs)

    @use_cache(86400 * 30, cache_none=False)
    @throttle_with_retries
    async def _get_known_url_data(self, /, resource: str, inc: str) -> Any:
        """Fetch what MusicBrainz links to a URL, caching only a URL it knows."""
        return await self._request("url", resource=resource, inc=inc)

    async def _request(self, endpoint: str, **params: str) -> Any:
        """Perform one request against the MusicBrainz API and return its JSON body."""
        url = f"{MB_BASE_URL}/{endpoint}"
        headers = {
            "User-Agent": f"Music Assistant/{self.mass.version} (https://music-assistant.io)"
        }
        params["fmt"] = "json"
        async with self.mass.http_session.get(url, headers=headers, params=params) as response:
            # handle rate limiter
            if response.status == 429:
                backoff_time = parse_retry_after(response.headers.get("Retry-After"))
                raise RateLimited("Rate Limiter", backoff_time=backoff_time)
            # handle temporary server error
            if response.status in (502, 503):
                raise ResourceTemporarilyUnavailable(backoff_time=30)
            # handle 404 not found
            if response.status in (400, 401, 404):
                return None
            response.raise_for_status()
            return await response.json(loads=json_loads)
