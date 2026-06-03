"""Apple Music API client."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import MediaType
from music_assistant_models.errors import (
    MediaNotFoundError,
    MusicAssistantError,
    RateLimited,
    ResourceTemporarilyUnavailable,
)

from music_assistant.helpers.json import json_loads
from music_assistant.helpers.throttle_retry import ThrottlerManager, throttle_with_retries

from .helpers.utils import is_library_id, translate_media_type_to_apple_type

if TYPE_CHECKING:
    from .provider import AppleMusicProvider

_APPLE_API_BASE = "https://api.music.apple.com/v1"


class AppleMusicAPIClient:
    """Handles all HTTP communication with the Apple Music API."""

    throttler = ThrottlerManager(rate_limit=1, period=2, initial_backoff=15)

    def __init__(self, provider: AppleMusicProvider) -> None:
        """Initialize the API client."""
        self.provider = provider
        self.logger = provider.logger

    @property
    def _headers(self) -> dict[str, str]:
        """Return standard auth headers."""
        return {
            "Authorization": f"Bearer {self.provider._music_app_token}",
            "Music-User-Token": self.provider._music_user_token,
        }

    @throttle_with_retries
    async def get_data(self, endpoint: str, **kwargs: Any) -> dict[str, Any]:
        """GET data from the Apple Music API."""
        url = f"{_APPLE_API_BASE}/{endpoint}"
        async with (
            self.provider.mass.http_session.get(
                url, headers=self._headers, params=kwargs, ssl=True, timeout=120
            ) as response,
        ):
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
                raise MusicAssistantError("Unexpected server error when calling Apple Music")
            response.raise_for_status()
            return await response.json(loads=json_loads)

    @throttle_with_retries
    async def delete_data(self, endpoint: str, data: Any = None, **kwargs: Any) -> None:
        """DELETE data from the Apple Music API."""
        url = f"{_APPLE_API_BASE}/{endpoint}"
        async with (
            self.provider.mass.http_session.delete(
                url, headers=self._headers, params=kwargs, json=data, ssl=True, timeout=120
            ) as response,
        ):
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
                url, headers=self._headers, params=kwargs, json=data, ssl=True, timeout=120
            ) as response,
        ):
            if response.status == 404:
                raise MediaNotFoundError(f"{endpoint} not found")
            if response.status == 429:
                self.provider.logger.debug(
                    "Apple Music Rate Limiter. Headers: %s", response.headers
                )
                raise RateLimited("Apple Music Rate Limiter")
            response.raise_for_status()
            if response.content_length:
                return await response.json(loads=json_loads)
            return {}

    @throttle_with_retries
    async def post_data(self, endpoint: str, data: Any = None, **kwargs: Any) -> dict[str, Any]:
        """POST data to the Apple Music API."""
        url = f"{_APPLE_API_BASE}/{endpoint}"
        async with (
            self.provider.mass.http_session.post(
                url, headers=self._headers, params=kwargs, json=data, ssl=True, timeout=120
            ) as response,
        ):
            if response.status == 404:
                raise MediaNotFoundError(f"{endpoint} not found")
            if response.status == 429:
                self.provider.logger.debug(
                    "Apple Music Rate Limiter. Headers: %s", response.headers
                )
                raise RateLimited("Apple Music Rate Limiter")
            response.raise_for_status()
            return await response.json(loads=json_loads)

    async def get_all_items(self, endpoint: str, key: str = "data", **kwargs: Any) -> list[dict]:
        """Get all items from a paged list."""
        limit = 50
        offset = 0
        all_items: list[dict] = []
        while True:
            kwargs["limit"] = limit
            kwargs["offset"] = offset
            result = await self.get_data(endpoint, **kwargs)
            if key not in result:
                break
            all_items += result[key]
            if not result.get("next"):
                break
            offset += limit
        return all_items

    async def get_user_storefront(self) -> str:
        """Return the user's storefront identifier."""
        locale = self.provider.mass.metadata.locale.replace("_", "-")
        language = locale.split("-")[0]
        result = await self.get_data("me/storefront", l=language)
        return result["data"][0]["id"]

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
