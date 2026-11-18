"""API Client for Telmore Musik."""

from __future__ import annotations

from music_assistant.helpers.throttle_retry import ThrottlerManager
from music_assistant.providers.music247e.api_client import Music247eAPIClient


class TelmoreAPIClient(Music247eAPIClient):
    """Client for interacting with the Telmore Musik API."""

    GRAPHQL_ENDPOINT = "https://graphql-1387.api.247e.com/graphql"
    SERVICE_NAME = "Telmore"

    # Unsure if Telmore enforces rate limiting, this is just a sane precaution
    throttler = ThrottlerManager(rate_limit=4, period=1)
