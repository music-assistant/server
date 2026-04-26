"""API Client for YouSee Musik."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from music_assistant_models.errors import (
    LoginFailed,
)

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.helpers.json import json_dumps
from music_assistant.helpers.throttle_retry import ThrottlerManager, throttle_with_retries
from music_assistant.providers.yousee.constants import MAX_PAGES_PAGINATED, PAGE_SIZE

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant.providers.yousee.provider import YouSeeMusikProvider


JsonLike = dict[str, Any]


class YouSeeGraphQLError(Exception):
    """YouSee Musik GraphQL error."""

    def __init__(self, data: JsonLike) -> None:
        """Initialize YouSeeGraphQLError."""
        super().__init__(json_dumps(data))


class YouSeeAPIClient:
    """Client for interacting with YouSee API."""

    YOUSEE_GRAPHQL_ENDPOINT = "https://graphql-1458.api.247e.com/graphql"

    # Unsure if yousee enforces rate limiting, this is just a sane precaution
    throttler = ThrottlerManager(rate_limit=4, period=1)

    def __init__(self, provider: YouSeeMusikProvider):
        """Initialize API client."""
        self.provider = provider
        self.auth = provider.auth
        self.logger = provider.logger
        self.mass = provider.mass

    @throttle_with_retries  # type: ignore[type-var]
    async def post_graphql(
        self, query: str, variables: JsonLike, _headers: JsonLike | None = None
    ) -> JsonLike:
        """Post GraphQL query to YouSee endpoint with authorization."""
        locale = self.mass.metadata.locale.split("_")[0]

        async with self.mass.http_session.post(
            self.YOUSEE_GRAPHQL_ENDPOINT,
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
                raise LoginFailed("Authentication with YouSee failed")

            resp.raise_for_status()

            result = await resp.json()
            if len(result.get("errors", [])) > 0:
                raise YouSeeGraphQLError(result)

            return dict(result)

    async def paginate_graphql(
        self,
        query: str,
        variables: JsonLike,
        page_path: list[str],
        variables_first_key: str = "first",
        variables_after_key: str = "after",
    ) -> AsyncGenerator[JsonLike, None]:
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
