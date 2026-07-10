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
    throttler = ThrottlerManager(rate_limit=10, period=10)

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize the API client."""
        self.mass = mass
        self.logger = logging.getLogger(__name__)

    @use_cache(86400 * 30)  # Cache for 30 days
    @throttle_with_retries
    async def get_data(self, endpoint: str, **kwargs: str) -> Any:
        """
        Fetch data from the MusicBrainz API.

        Results are cached for 30 days.

        :param endpoint: API endpoint path (e.g. ``artist/123?inc=aliases``).
        :param kwargs: Additional query parameters forwarded as URL params.
        """
        url = f"{MB_BASE_URL}/{endpoint}"
        headers = {
            "User-Agent": f"Music Assistant/{self.mass.version} (https://music-assistant.io)"
        }
        kwargs["fmt"] = "json"
        async with self.mass.http_session.get(url, headers=headers, params=kwargs) as response:
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
