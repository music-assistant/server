"""API Client for Tidal."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    ResourceTemporarilyUnavailable,
)

from music_assistant.helpers.throttle_retry import ThrottlerManager, throttle_with_retries

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from aiohttp import ClientResponse

    from .provider import TidalProvider


class TidalAPIClient:
    """Client for interacting with Tidal API."""

    BASE_URL: str = "https://api.tidal.com/v1"
    BASE_URL_V2: str = "https://api.tidal.com/v2"
    OPEN_API_URL: str = "https://openapi.tidal.com/v2"

    # Define throttler here for use by the client
    throttler = ThrottlerManager(rate_limit=1, period=2)

    def __init__(self, provider: TidalProvider):
        """Initialize API client."""
        self.provider = provider
        self.auth = provider.auth
        self.logger = provider.logger
        self.mass = provider.mass

    async def get(
        self, endpoint: str, **kwargs: Any
    ) -> dict[str, Any] | tuple[dict[str, Any], str]:
        """Get data from Tidal API."""
        return await self._request("GET", endpoint, **kwargs)

    async def get_data(self, endpoint: str, **kwargs: Any) -> dict[str, Any]:
        """Get data from Tidal API, discarding headers/ETags."""
        result = await self.get(endpoint, **kwargs)
        return result[0] if isinstance(result, tuple) else result

    async def post(
        self,
        endpoint: str,
        data: dict[str, Any] | None = None,
        as_form: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Send POST data to Tidal API."""
        if as_form:
            kwargs.setdefault("headers", {})["Content-Type"] = "application/x-www-form-urlencoded"
            kwargs["data"] = data
        else:
            kwargs["json"] = data

        return cast("dict[str, Any]", await self._request("POST", endpoint, **kwargs))

    async def put(
        self,
        endpoint: str,
        data: dict[str, Any] | None = None,
        as_form: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Send PUT data to Tidal API."""
        # Special handling for mixes which use V2
        if "mixes" in endpoint and "base_url" not in kwargs:
            kwargs["base_url"] = self.BASE_URL_V2

        if as_form:
            kwargs.setdefault("headers", {})["Content-Type"] = "application/x-www-form-urlencoded"
            kwargs["data"] = data
        else:
            kwargs["json"] = data

        return cast("dict[str, Any]", await self._request("PUT", endpoint, **kwargs))

    async def delete(
        self, endpoint: str, data: dict[str, Any] | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        """Delete data from Tidal API."""
        kwargs["json"] = data
        return cast("dict[str, Any]", await self._request("DELETE", endpoint, **kwargs))

    @throttle_with_retries  # type: ignore[type-var]
    async def _request(
        self, method: str, endpoint: str, **kwargs: Any
    ) -> dict[str, Any] | tuple[dict[str, Any], str]:
        """Handle API requests internally."""
        if not await self.auth.ensure_valid_token():
            raise LoginFailed("Failed to authenticate with Tidal")

        # Prepare URL
        base_url = kwargs.pop("base_url", self.BASE_URL)
        url = f"{base_url}/{endpoint}"

        # Prepare Headers
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self.auth.access_token}"

        locale = self.mass.metadata.locale.replace("_", "-")
        language = locale.split("-")[0]
        headers["Accept-Language"] = f"{locale}, {language};q=0.9, *;q=0.5"

        # Prepare Params
        params = kwargs.pop("params", {}) or {}
        if self.auth.session_id:
            params["sessionId"] = self.auth.session_id
        if self.auth.country_code:
            params["countryCode"] = self.auth.country_code

        # Extract special handling flags
        return_etag = kwargs.pop("return_etag", False)

        self.logger.debug("Making %s request to Tidal API: %s", method, endpoint)

        async with self.mass.http_session.request(
            method, url, headers=headers, params=params, **kwargs
        ) as response:
            return await self._handle_response(response, return_etag)

    async def _handle_response(
        self, response: ClientResponse, return_etag: bool = False
    ) -> dict[str, Any] | tuple[dict[str, Any], str]:
        """Handle API response and common error conditions."""
        if response.status == 401:
            raise LoginFailed("Authentication failed")
        if response.status == 404:
            raise MediaNotFoundError(f"Item not found: {response.url}")
        if response.status == 429:
            retry_after = int(response.headers.get("Retry-After", 30))
            raise ResourceTemporarilyUnavailable(
                "Tidal Rate limit reached", backoff_time=retry_after
            )
        if response.status >= 400:
            text = await response.text()
            self.logger.error("API error: %s - %s", response.status, text)
            raise ResourceTemporarilyUnavailable("API error")

        try:
            if response.status == 204 or response.content_length == 0:
                data = {"success": True}
            else:
                data = await response.json()

            if return_etag:
                etag = response.headers.get("ETag", "")
                return data, etag
            return data
        except json.JSONDecodeError as err:
            raise ResourceTemporarilyUnavailable("Failed to parse response") from err

    async def paginate(
        self,
        endpoint: str,
        item_key: str = "items",
        nested_key: str | None = None,
        limit: int = 50,
        cursor_based: bool = False,
        **kwargs: Any,
    ) -> AsyncGenerator[Any, None]:
        """Paginate through all items from a Tidal API endpoint."""
        offset = 0
        cursor = None

        while True:
            params = {"limit": limit}
            if cursor_based:
                if cursor:
                    params["cursor"] = cursor
            else:
                params["offset"] = offset

            if "params" in kwargs:
                params.update(kwargs.pop("params"))

            api_result = await self.get(endpoint, params=params, **kwargs)
            response = api_result[0] if isinstance(api_result, tuple) else api_result

            items = response.get(item_key, [])
            if not items:
                break

            for item in items:
                if nested_key and nested_key in item and item[nested_key]:
                    yield item[nested_key]
                else:
                    yield item

            if cursor_based:
                cursor = response.get("cursor")
                if not cursor:
                    break
            else:
                offset += len(items)
