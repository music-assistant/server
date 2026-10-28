"""API Client for YouSee Musik."""

from __future__ import annotations

from music_assistant.helpers.throttle_retry import ThrottlerManager
from music_assistant.providers.music247e.api_client import Music247eAPIClient


class YouSeeAPIClient(Music247eAPIClient):
    """Client for interacting with the YouSee Musik API."""

    GRAPHQL_ENDPOINT = "https://graphql-1458.api.247e.com/graphql"
    SERVICE_NAME = "YouSee"

    # Unsure if yousee enforces rate limiting, this is just a sane precaution
    throttler = ThrottlerManager(rate_limit=4, period=1)
