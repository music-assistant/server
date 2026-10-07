"""API Client for Tidal."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    RateLimited,
    ResourceTemporarilyUnavailable,
)

from music_assistant.helpers.throttle_retry import ThrottlerManager, throttle_with_retries

from .constants import BASE_URL, JSONAPI_CONTENT_TYPE, OPEN_API_URL
from .jsonapi import JsonApiDocument

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence

    from aiohttp import ClientResponse

    from .provider import TidalProvider

# Safety ceiling for cursor pagination. The JSON:API relationship endpoints only
# expose page[cursor] (no page size control), so the server fixes a small page
# size and this cap is the only guard against a runaway cursor. It must stay high
# enough to walk an entire library/playlist without truncating; only a genuinely
# broken cursor should ever reach it.
MAX_PAGINATION_PAGES = 1000


class TidalAPIClient:
    """Client for interacting with Tidal API."""

    # Define throttler here for use by the client
    # Rate empirically verified (2026-07): a 10-minute soak at 4/s (2400 mixed
    # requests) plus bursts to 12/s completed without a single 429.
    throttler = ThrottlerManager(rate_limit=4, period=1)

    def __init__(self, provider: TidalProvider):
        """Initialize API client."""
        self.provider = provider
        self.auth = provider.auth
        self.logger = provider.logger
        self.mass = provider.mass

    async def get(self, endpoint: str, **kwargs: Any) -> dict[str, Any]:
        """Get data from Tidal API."""
        data, _ = await self._request("GET", endpoint, **kwargs)
        return data

    async def get_jsonapi(
        self,
        endpoint: str,
        include: Sequence[str] | None = None,
        params: dict[str, Any] | None = None,
        replace_media: str | None = None,
        **kwargs: Any,
    ) -> JsonApiDocument:
        """
        Get a JSON:API document from the official Tidal API.

        :param endpoint: Path below the API root.
        :param include: Relationship paths to side-load into `included`.
        :param params: Extra query parameters.
        :param replace_media: Relationship path(s) whose media identifiers Tidal should
            project onto their live replacements. Tidal churns tracks (deletes and
            re-adds them under new ids), and this makes it hand back the live id plus
            the original in `meta.replacement`, instead of an id that 404s.
        """
        query = dict(params or {})
        if include:
            query["include"] = ",".join(include)
        if replace_media:
            query["replaceMedia"] = replace_media
        headers = kwargs.pop("headers", {})
        headers["Accept"] = JSONAPI_CONTENT_TYPE
        data = await self.get(
            endpoint, base_url=OPEN_API_URL, params=query, headers=headers, **kwargs
        )
        # A valid JSON:API read always carries a top-level "data"; an empty body
        # ({"success": True}) or error document lacks it, so raise instead of
        # returning a silently empty result that would be cached as a no-match.
        if "data" not in data:
            raise ResourceTemporarilyUnavailable(f"Invalid JSON:API response for {endpoint}")
        return JsonApiDocument(data)

    async def write_jsonapi(
        self, method: str, endpoint: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """Send a JSON:API write (POST/DELETE) to the official Tidal API."""
        headers = {
            "Content-Type": JSONAPI_CONTENT_TYPE,
            "Accept": JSONAPI_CONTENT_TYPE,
            "Idempotency-Key": str(uuid4()),
        }
        result, _ = await self._request(
            method, endpoint, base_url=OPEN_API_URL, data=json.dumps(body), headers=headers
        )
        return result

    async def get_with_etag(self, endpoint: str, **kwargs: Any) -> tuple[dict[str, Any], str]:
        """Get data from the (unofficial) Tidal API, returning the response ETag as well."""
        return await self._request("GET", endpoint, **kwargs)

    async def post(
        self,
        endpoint: str,
        data: dict[str, Any] | None = None,
        as_form: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Send POST data to the (unofficial) Tidal API."""
        if as_form:
            kwargs.setdefault("headers", {})["Content-Type"] = "application/x-www-form-urlencoded"
            kwargs["data"] = data
        else:
            kwargs["json"] = data
        result, _ = await self._request("POST", endpoint, **kwargs)
        return result

    async def delete(
        self, endpoint: str, data: dict[str, Any] | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        """Delete data from the (unofficial) Tidal API."""
        kwargs["json"] = data
        result, _ = await self._request("DELETE", endpoint, **kwargs)
        return result

    async def paginate_jsonapi(
        self,
        endpoint: str,
        include: Sequence[str] | None = None,
        params: dict[str, Any] | None = None,
        max_pages: int = MAX_PAGINATION_PAGES,
        replace_media: str | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[JsonApiDocument]:
        """Yield successive JSON:API document pages, following the cursor links."""
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(max_pages):
            page_params = dict(params or {})
            if cursor:
                page_params["page[cursor]"] = cursor
            doc = await self.get_jsonapi(
                endpoint,
                include=include,
                params=page_params,
                replace_media=replace_media,
                **kwargs,
            )
            yield doc
            cursor = doc.next_cursor
            if not cursor:
                return
            # A server re-serving an already-followed cursor would loop all the way
            # to the page cap before warning; stop at the first repeat instead.
            if cursor in seen_cursors:
                self.logger.warning(
                    "Stopped paginating %s: server repeated cursor %s", endpoint, cursor
                )
                return
            seen_cursors.add(cursor)
        # Reached the page cap while more pages remained: surface the truncation.
        self.logger.warning(
            "Stopped paginating %s after %d pages; results may be truncated", endpoint, max_pages
        )

    @throttle_with_retries
    async def _request(
        self, method: str, endpoint: str, **kwargs: Any
    ) -> tuple[dict[str, Any], str]:
        """Handle API requests internally."""
        if not await self.auth.ensure_valid_token():
            raise LoginFailed("Failed to authenticate with Tidal")

        # Prepare URL
        base_url = kwargs.pop("base_url", BASE_URL)
        url = f"{base_url}/{endpoint}"

        # Prepare Headers
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self.auth.access_token}"

        locale = self.mass.metadata.locale.replace("_", "-")
        language = locale.split("-")[0]
        headers["Accept-Language"] = f"{locale}, {language};q=0.9, *;q=0.5"

        # Prepare Params
        params = kwargs.pop("params", {}) or {}
        # sessionId is an unofficial-API concept; don't send it to the official API.
        if self.auth.session_id and base_url != OPEN_API_URL:
            params["sessionId"] = self.auth.session_id
        if self.auth.country_code:
            params["countryCode"] = self.auth.country_code

        self.logger.debug("Making %s request to Tidal API: %s", method, endpoint)

        async with self.mass.http_session.request(
            method, url, headers=headers, params=params, **kwargs
        ) as response:
            if response.status != 401:
                return await self._handle_response(response)

        # The token was rejected before its known expiry (e.g. invalidated
        # server-side): force a refresh and retry the request once.
        self.logger.debug("Got 401 from Tidal API, forcing token refresh and retrying")
        if not await self.auth.refresh_token():
            raise LoginFailed("Authentication failed")
        headers["Authorization"] = f"Bearer {self.auth.access_token}"

        async with self.mass.http_session.request(
            method, url, headers=headers, params=params, **kwargs
        ) as response:
            return await self._handle_response(response)

    async def _handle_response(self, response: ClientResponse) -> tuple[dict[str, Any], str]:
        """Handle API response and common error conditions."""
        if response.status == 401:
            raise LoginFailed("Authentication failed")
        if response.status == 404:
            raise MediaNotFoundError(f"Item not found: {response.url}")
        if response.status == 429:
            retry_after = int(response.headers.get("Retry-After", 30))
            raise RateLimited("Tidal Rate limit reached", backoff_time=retry_after)
        if response.status >= 400:
            text = await response.text()
            self.logger.error("API error: %s - %s", response.status, text)
            raise ResourceTemporarilyUnavailable("API error")

        try:
            if response.status == 204 or response.content_length == 0:
                data = {"success": True}
            else:
                data = await response.json()
        except json.JSONDecodeError as err:
            raise ResourceTemporarilyUnavailable("Failed to parse response") from err

        return data, response.headers.get("ETag", "")
