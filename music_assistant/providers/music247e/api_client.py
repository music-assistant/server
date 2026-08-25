"""API Client for the 24-7 (247e) music backend."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from music_assistant_models.errors import (
    LoginFailed,
    RateLimited,
    ResourceTemporarilyUnavailable,
)

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.helpers.json import json_dumps
from music_assistant.helpers.throttle_retry import (
    ThrottlerManager,
    parse_retry_after,
    throttle_with_retries,
)
from music_assistant.providers.music247e.constants import MAX_PAGES_PAGINATED, PAGE_SIZE

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant.providers.music247e.provider import Music247eProvider


JsonLike = dict[str, Any]


class InvalidDataError(Exception):
    """24-7 (247e) GraphQL error."""

    def __init__(self, data: JsonLike) -> None:
        """Initialize InvalidDataError."""
        super().__init__(json_dumps(data))


class Music247eAPIClient:
    """Client for interacting with a 24-7 (247e) GraphQL API."""

    # concrete providers set the tenant-specific GraphQL endpoint and display name
    GRAPHQL_ENDPOINT: str
    SERVICE_NAME: str

    # Unsure if the backend enforces rate limiting, this is just a sane precaution
    throttler = ThrottlerManager(rate_limit=4, period=1)

    def __init__(self, provider: Music247eProvider):
        """Initialize API client."""
        self.provider = provider
        self.auth = provider.auth
        self.logger = provider.logger
        self.mass = provider.mass

    @throttle_with_retries
    async def post_graphql(
        self, query: str, variables: JsonLike, _headers: JsonLike | None = None
    ) -> JsonLike:
        """Post GraphQL query to the endpoint with authorization."""
        locale = self.mass.metadata.locale.split("_")[0]

        async with self.mass.http_session.post(
            self.GRAPHQL_ENDPOINT,
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
                raise LoginFailed(f"Authentication with {self.SERVICE_NAME} failed")
            # handle rate limiter
            if resp.status == 429:
                backoff_time = parse_retry_after(resp.headers.get("Retry-After"))
                raise RateLimited("Rate Limiter", backoff_time=backoff_time)
            # handle temporary server error
            if resp.status in (502, 503):
                raise ResourceTemporarilyUnavailable(backoff_time=30)

            resp.raise_for_status()

            result = await resp.json()
            if len(result.get("errors", [])) > 0:
                raise InvalidDataError(result)

            return dict(result)

    async def paginate_graphql(
        self,
        query: str,
        variables: JsonLike,
        page_path: list[str],
        variables_first_key: str = "first",
        variables_after_key: str = "after",
    ) -> AsyncGenerator[JsonLike]:
        """Paginate GraphQL results."""
        after = None
        has_more = True
        i = 0
        while has_more and (i < MAX_PAGES_PAGINATED):
            self.logger.log(VERBOSE_LOG_LEVEL, "Paginating GraphQL query, page %s", i + 1)
            vars_with_pagination = variables | {
                variables_first_key: PAGE_SIZE,
                variables_after_key: after,
            }
            result = await self.post_graphql(query, vars_with_pagination)

            # Navigate to the page containing items and pageInfo
            page_data = result
            for key in page_path:
                page_data = page_data.get(key, {})

            for item in page_data.get("items", []):
                yield item

            page_info = page_data.get("pageInfo", {})
            has_more = page_info.get("hasNextPage", False)
            after = page_info.get("endCursor", None)
            i += 1
