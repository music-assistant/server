"""Apple Music API client."""

from __future__ import annotations

import functools
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import TYPE_CHECKING, Any, Concatenate, cast

from aiohttp import ClientConnectionError, ClientPayloadError, ClientTimeout
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    RateLimited,
    ResourceTemporarilyUnavailable,
)

from music_assistant.helpers.json import json_loads
from music_assistant.helpers.throttle_retry import ThrottlerManager, throttle_with_retries

from .helpers.utils import is_library_id, translate_media_type_to_apple_type

if TYPE_CHECKING:
    from .provider import AppleMusicProvider

_APPLE_API_BASE = "https://api.music.apple.com/v1"

_LIBRARY_PAGE_SIZE = 50

_PAGE_TRUNCATION_RETRIES = 3


def _retry_transient_transport_errors[ClientT, **P, R](
    func: Callable[Concatenate[ClientT, P], Awaitable[R]],
) -> Callable[Concatenate[ClientT, P], Awaitable[R]]:
    """
    Convert transient aiohttp transport errors into the retryable error type.

    A dropped connection or truncated body raises an ``aiohttp.ClientError`` (or
    ``TimeoutError``) rather than an HTTP status, so it would otherwise bypass the
    status-based retry handling. ``ClientResponseError`` (raised by
    ``raise_for_status`` for genuine 4xx/5xx) is deliberately not caught here.
    """

    @functools.wraps(func)
    async def wrapper(self: ClientT, *args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await func(self, *args, **kwargs)
        except (ClientConnectionError, ClientPayloadError, TimeoutError) as err:
            raise ResourceTemporarilyUnavailable(
                f"Transient transport error calling Apple Music: {type(err).__name__}: {err}"
            ) from err

    return wrapper


def _raise_on_auth_error(status: int, endpoint: str) -> None:
    """
    Raise LoginFailed when Apple rejected our credentials.

    Apple answers a revoked or expired music user token with 401/403 on every endpoint,
    so this surfaces as an auth error the user can act on instead of a bare HTTP error.

    :param status: The HTTP status code returned by Apple.
    :param endpoint: The endpoint that was called, used in the error message.
    """
    if status in (401, 403):
        raise LoginFailed(
            f"Apple Music denied access to {endpoint}: the account needs to be signed in again"
        )


class AppleMusicAPIClient:
    """Handles all HTTP communication with the Apple Music API."""

    # period=0.25 -> 4 req/s. Apple throttles per developer account, so 429s follow the
    # fleet-wide load on the bundled token, not our rate - and they clear within a second.
    throttler = ThrottlerManager(rate_limit=1, period=0.25, initial_backoff=1)

    def __init__(self, provider: AppleMusicProvider) -> None:
        """Initialize the API client."""
        self.provider = provider
        self.logger = provider.logger

    @property
    def _headers(self) -> dict[str, str]:
        """Return standard auth headers."""
        return {
            "Authorization": f"Bearer {self.provider._music_app_token}",
            "Music-User-Token": cast("str", self.provider._music_user_token),
        }

    @throttle_with_retries
    @_retry_transient_transport_errors
    async def get_data(self, endpoint: str, **kwargs: Any) -> dict[str, Any]:
        """GET data from the Apple Music API."""
        url = f"{_APPLE_API_BASE}/{endpoint}"
        async with (
            self.provider.mass.http_session.get(
                url,
                headers=self._headers,
                params=kwargs,
                ssl=True,
                timeout=ClientTimeout(total=120),
            ) as response,
        ):
            _raise_on_auth_error(response.status, endpoint)
            if response.status == 404 and "limit" in kwargs and "offset" in kwargs:
                return {}
            if response.status == 404:
                raise MediaNotFoundError(f"{endpoint} not found")
            if response.status == 504:
                self.provider.logger.debug(
                    "Apple Music API Timeout: url=%s, params=%s, response_headers=%s",
                    url,
                    kwargs,
                    response.headers,
                )
                raise ResourceTemporarilyUnavailable("Apple Music API Timeout")
            if response.status == 429:
                self.provider.logger.debug(
                    "Apple Music Rate Limiter. Headers: %s", response.headers
                )
                raise RateLimited("Apple Music Rate Limiter")
            if response.status == 500:
                # Apple 500s are typically transient; retry rather than abort the whole sync.
                raise ResourceTemporarilyUnavailable(
                    "Unexpected server error when calling Apple Music"
                )
            response.raise_for_status()
            return cast("dict[str, Any]", await response.json(loads=json_loads))

    @throttle_with_retries
    async def delete_data(self, endpoint: str, data: Any = None, **kwargs: Any) -> None:
        """DELETE data from the Apple Music API."""
        url = f"{_APPLE_API_BASE}/{endpoint}"
        async with (
            self.provider.mass.http_session.delete(
                url,
                headers=self._headers,
                params=kwargs,
                json=data,
                ssl=True,
                timeout=ClientTimeout(total=120),
            ) as response,
        ):
            _raise_on_auth_error(response.status, endpoint)
            if response.status == 404:
                raise MediaNotFoundError(f"{endpoint} not found")
            if response.status == 429:
                self.provider.logger.debug(
                    "Apple Music Rate Limiter. Headers: %s", response.headers
                )
                raise RateLimited("Apple Music Rate Limiter")
            response.raise_for_status()

    @throttle_with_retries
    async def put_data(self, endpoint: str, data: Any = None, **kwargs: Any) -> dict[str, Any]:
        """PUT data to the Apple Music API."""
        url = f"{_APPLE_API_BASE}/{endpoint}"
        async with (
            self.provider.mass.http_session.put(
                url,
                headers=self._headers,
                params=kwargs,
                json=data,
                ssl=True,
                timeout=ClientTimeout(total=120),
            ) as response,
        ):
            _raise_on_auth_error(response.status, endpoint)
            if response.status == 404:
                raise MediaNotFoundError(f"{endpoint} not found")
            if response.status == 429:
                self.provider.logger.debug(
                    "Apple Music Rate Limiter. Headers: %s", response.headers
                )
                raise RateLimited("Apple Music Rate Limiter")
            response.raise_for_status()
            if response.content_length:
                return cast("dict[str, Any]", await response.json(loads=json_loads))
            return {}

    @throttle_with_retries
    async def post_data(self, endpoint: str, data: Any = None, **kwargs: Any) -> dict[str, Any]:
        """POST data to the Apple Music API."""
        url = f"{_APPLE_API_BASE}/{endpoint}"
        async with (
            self.provider.mass.http_session.post(
                url,
                headers=self._headers,
                params=kwargs,
                json=data,
                ssl=True,
                timeout=ClientTimeout(total=120),
            ) as response,
        ):
            _raise_on_auth_error(response.status, endpoint)
            if response.status == 404:
                raise MediaNotFoundError(f"{endpoint} not found")
            if response.status == 429:
                self.provider.logger.debug(
                    "Apple Music Rate Limiter. Headers: %s", response.headers
                )
                raise RateLimited("Apple Music Rate Limiter")
            response.raise_for_status()
            return cast("dict[str, Any]", await response.json(loads=json_loads))

    async def iter_all_items(
        self, endpoint: str, key: str = "data", page_size: int = _LIBRARY_PAGE_SIZE, **kwargs: Any
    ) -> AsyncGenerator[dict[str, Any]]:
        """
        Yield items from a paged list one page at a time.

        Unlike :meth:`get_all_items`, this never holds the full result set in memory, so it is
        safe for very large listings (e.g. a 100k-track Apple Music library).
        """
        offset = 0
        while True:
            kwargs["limit"] = page_size
            kwargs["offset"] = offset
            result = await self._get_page(endpoint, key, offset, kwargs)
            if key not in result:
                # offset 0 only: empty collection or 404. _get_page raises on mid-list truncation.
                break
            for item in result[key]:
                yield item
            if not result.get("next"):
                break
            offset += page_size

    async def get_all_items(
        self, endpoint: str, key: str = "data", **kwargs: Any
    ) -> list[dict[str, Any]]:
        """Get all items from a paged list."""
        return [item async for item in self.iter_all_items(endpoint, key, **kwargs)]

    async def get_user_storefront(self) -> str:
        """Return the user's storefront identifier."""
        locale = self.provider.mass.metadata.locale.replace("_", "-")
        language = locale.split("-")[0]
        result = await self.get_data("me/storefront", l=language)
        return cast("str", result["data"][0]["id"])

    async def get_ratings(self, item_ids: list[str], media_type: MediaType) -> dict[str, bool]:
        """Return a mapping of item_id → is_favourite for a list of IDs."""
        if media_type == MediaType.ARTIST:
            raise NotImplementedError(
                "Ratings are not available for artist in the Apple Music API."
            )
        if not item_ids:
            return {}
        apple_type = translate_media_type_to_apple_type(media_type)
        endpoint = apple_type if not is_library_id(item_ids[0]) else f"library-{apple_type}"
        max_ids_per_request = 200
        results: dict[str, bool] = {}
        for i in range(0, len(item_ids), max_ids_per_request):
            batch_ids = item_ids[i : i + max_ids_per_request]
            response = await self.get_data(
                f"me/ratings/{endpoint}",
                ids=",".join(batch_ids),
            )
            results.update(
                {
                    item["id"]: bool(item["attributes"].get("value", False) == 1)
                    for item in response.get("data", [])
                }
            )
        return results

    async def _get_page(
        self, endpoint: str, key: str, offset: int, kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Fetch a single page of a paged listing, recovering from transient truncation.

        Apple returns ``{}`` (HTTP 404) for a paged request that yields no payload. On the
        first page (offset 0) that legitimately means an empty collection. Mid-pagination
        (offset > 0, reached only after a prior page promised more via ``next``) it means a
        transient truncation; accepting it would drop still-present items and trigger
        spurious deletions, so re-fetch the same page a bounded number of times before
        surfacing the failure.
        """
        result = await self.get_data(endpoint, **kwargs)
        if key in result or offset == 0:
            return result
        for _ in range(_PAGE_TRUNCATION_RETRIES):
            result = await self.get_data(endpoint, **kwargs)
            if key in result:
                return result
        raise ResourceTemporarilyUnavailable(
            f"Incomplete paged listing for {endpoint} at offset {offset}"
        )
