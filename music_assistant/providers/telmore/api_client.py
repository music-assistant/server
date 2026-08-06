"""API Client for Telmore Musik."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from music_assistant_models.errors import (
    LoginFailed,
)

from music_assistant.helpers.json import json_dumps
from music_assistant.helpers.throttle_retry import (
    ThrottlerManager,
    throttle_with_retries,
)
from music_assistant.providers.yousee.api_client import YouSeeAPIClient

if TYPE_CHECKING:
    from music_assistant.providers.telmore.provider import TelmoreMusikProvider


JsonLike = dict[str, Any]


class TelmoreGraphQLError(Exception):
    """Telmore Musik GraphQL error."""

    def __init__(self, data: JsonLike) -> None:
        """Initialize TelmoreGraphQLError."""
        super().__init__(json_dumps(data))


class TelmoreAPIClient(YouSeeAPIClient):
    """Client for interacting with Telmore API."""

    TELMORE_GRAPHQL_ENDPOINT = "https://graphql-1387.api.247e.com/graphql"

    # Unsure if Telmore enforces rate limiting, this is just a sane precaution
    throttler = ThrottlerManager(rate_limit=4, period=1)

    def __init__(self, provider: TelmoreMusikProvider):
        """Initialize API client."""
        self.provider = provider  # type: ignore[assignment]
        self.auth = provider.auth  # type: ignore[assignment]
        self.logger = provider.logger
        self.mass = provider.mass

    @throttle_with_retries
    async def post_graphql(
        self, query: str, variables: JsonLike, _headers: JsonLike | None = None
    ) -> JsonLike:
        """Post GraphQL query to Telmore endpoint with authorization."""
        locale = self.mass.metadata.locale.split("_")[0]

        async with self.mass.http_session.post(
            self.TELMORE_GRAPHQL_ENDPOINT,
            json={"query": query, "variables": variables},
            headers={
                "Authorization": f"Bearer {await self.auth.auth_token()}",
                "Accept-Language": locale,
            }
            | (_headers or {}),
        ) as resp:
            if resp.status in {401, 403}:
                # Invalidate token
                self.auth.invalidate()
                raise LoginFailed("Authentication with Telmore failed")

            resp.raise_for_status()

            result = await resp.json()
            if len(result.get("errors", [])) > 0:
                raise TelmoreGraphQLError(result)

            return dict(result)
