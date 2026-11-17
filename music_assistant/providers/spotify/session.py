"""Spotify Web API session with its own client id, token and rate limit."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import TYPE_CHECKING, Any

import aiohttp
from music_assistant_models.errors import (
    MediaNotFoundError,
    RateLimited,
    ResourceTemporarilyUnavailable,
    RetriesExhausted,
)
from orjson import JSONDecodeError

from music_assistant.helpers.datetime import utc
from music_assistant.helpers.json import json_loads
from music_assistant.helpers.throttle_retry import (
    MAX_BACKOFF,
    RequestPriority,
    ThrottlerManager,
    current_priority,
    parse_retry_after,
    throttle_with_retries,
)

if TYPE_CHECKING:
    import logging

    from music_assistant.mass import MusicAssistant

API_URL = "https://api.spotify.com/v1"

# reason Spotify gives in the body of a 429 when the quota of the app is used up
QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
# seconds to wait for the body of a 429, which only adds the reason to what its headers say
RATE_LIMIT_BODY_TIMEOUT = 2


class SpotifySession:
    """
    Requests to the Spotify Web API on behalf of one client id.

    Spotify rate limits per client id, so each session has a throttler of its own.
    """

    def __init__(
        self,
        mass: MusicAssistant,
        logger: logging.Logger,
        name: str,
        label: str,
        throttler: ThrottlerManager,
        get_auth: Callable[[], Awaitable[dict[str, Any]]],
        on_unauthorized: Callable[[], None],
        has_fallback: bool = False,
    ) -> None:
        """
        Initialize the session.

        :param mass: The Music Assistant instance.
        :param logger: Logger for the requests of this session.
        :param name: Name of the session.
        :param label: Readable name of the Spotify app of the session, for logging.
        :param throttler: Throttler that paces the requests of this session.
        :param get_auth: Returns a valid access token of this session.
        :param on_unauthorized: Called when Spotify rejects the access token of this session.
        :param has_fallback: Whether another session serves user actions and playback while this
            one is rate limited, so such a request gives up on a limit at once.
        """
        self.mass = mass
        self.logger = logger
        self.name = name
        self.label = label
        self.throttler = throttler
        self._get_auth = get_auth
        self._on_unauthorized = on_unauthorized
        self._has_fallback = has_fallback
        self._last_rate_limit_reason: str | None = None
        self._last_rate_limit_at: datetime | None = None
        # quota answers in a row, without an answered request in between
        self._quota_answers = 0

    def get_diagnostics(self) -> dict[str, Any]:
        """Return the rate limit state of this session for a diagnostics report."""
        return {
            "cooldown_remaining_sec": round(self.throttler.cooldown_remaining),
            "last_rate_limit_reason": self._last_rate_limit_reason,
            "last_rate_limit_at": (
                self._last_rate_limit_at.isoformat() if self._last_rate_limit_at else None
            ),
        }

    @throttle_with_retries
    async def get(
        self, /, endpoint: str, *, auth_info: dict[str, Any] | None = None, **params: Any
    ) -> dict[str, Any]:
        """
        Get data from the api.

        :param endpoint: API endpoint to call.
        :param auth_info: Access token to use instead of the one of this session.
        :param params: Query parameters of the request.
        :raises MediaNotFoundError: When Spotify does not have, or does not share, the item.
        """
        params["market"] = "from_token"
        params["country"] = "from_token"
        locale = self.mass.metadata.locale.replace("_", "-")
        language = locale.split("-")[0]
        headers = {"Accept-Language": f"{locale}, {language};q=0.9, *;q=0.5"}
        self.logger.debug("handling get data %s/%s with kwargs %s", API_URL, endpoint, params)
        async with self._request(
            "GET",
            endpoint,
            auth_info=auth_info,
            headers=headers,
            params=params,
            timeout=aiohttp.ClientTimeout(total=120),
        ) as response:
            if response.status in (400, 403, 404):
                try:
                    error = await response.json(loads=json_loads)
                    message = error.get("error", {}).get("message") or response.reason
                except aiohttp.ContentTypeError, JSONDecodeError:
                    message = (await response.text()) or response.reason

                self.logger.debug(
                    "Spotify API error: endpoint=%s, status=%s, reason=%s, message=%s",
                    endpoint,
                    response.status,
                    response.reason,
                    message,
                )

                raise MediaNotFoundError(f"{endpoint} not found")

            response.raise_for_status()
            result: dict[str, Any] = await response.json(loads=json_loads)
            if etag := response.headers.get("ETag"):
                result["etag"] = etag
            return result

    @throttle_with_retries
    async def delete(
        self,
        /,
        endpoint: str,
        data: Any = None,
        *,
        auth_info: dict[str, Any] | None = None,
        **params: Any,
    ) -> None:
        """
        Delete data from the api.

        :param endpoint: API endpoint to call.
        :param data: JSON body of the request.
        :param auth_info: Token to use, the session's own token when omitted.
        :param params: Query parameters of the request.
        """
        async with self._request(
            "DELETE", endpoint, auth_info=auth_info, params=params, json=data, ssl=True
        ) as response:
            response.raise_for_status()

    @throttle_with_retries
    async def put(
        self,
        /,
        endpoint: str,
        data: Any = None,
        *,
        auth_info: dict[str, Any] | None = None,
        **params: Any,
    ) -> None:
        """
        Put data on the api.

        :param endpoint: API endpoint to call.
        :param data: JSON body of the request.
        :param auth_info: Token to use, the session's own token when omitted.
        :param params: Query parameters of the request.
        """
        async with self._request(
            "PUT", endpoint, auth_info=auth_info, params=params, json=data, ssl=True
        ) as response:
            response.raise_for_status()

    @throttle_with_retries
    async def post(
        self,
        /,
        endpoint: str,
        data: Any = None,
        want_result: bool = True,
        *,
        auth_info: dict[str, Any] | None = None,
        **params: Any,
    ) -> dict[str, Any]:
        """
        Post data on the api.

        :param endpoint: API endpoint to call.
        :param data: JSON body of the request.
        :param want_result: Return the response body, an empty dict when False.
        :param auth_info: Token to use, the session's own token when omitted.
        :param params: Query parameters of the request.
        """
        async with self._request(
            "POST", endpoint, auth_info=auth_info, params=params, json=data, ssl=True
        ) as response:
            response.raise_for_status()
            if not want_result:
                return {}
            result: dict[str, Any] = await response.json(loads=json_loads)
            return result

    @asynccontextmanager
    async def _request(
        self,
        method: str,
        endpoint: str,
        auth_info: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[aiohttp.ClientResponse]:
        """
        Send a request and yield its response, raising for the statuses every method shares.

        :param method: HTTP method of the request.
        :param endpoint: API endpoint to call.
        :param auth_info: Access token to use instead of the one of this session.
        :param headers: Extra headers of the request.
        :param kwargs: Extra arguments for the aiohttp request.
        """
        if not auth_info:
            auth_info = await self._get_auth()
        headers = {**(headers or {}), "Authorization": f"Bearer {auth_info['access_token']}"}
        if (
            self._has_fallback
            and current_priority() is not RequestPriority.LOW
            and self.throttler.cooldown_remaining > 0
        ):
            # the client was told to wait while this request stood in line for its turn
            raise RetriesExhausted(
                f"Spotify rate limited the {self.label}",
                translation_key=RateLimited.translation_key,
            )
        async with self.mass.http_session.request(
            method, f"{API_URL}/{endpoint}", headers=headers, **kwargs
        ) as response:
            # handle spotify rate limiter
            if response.status == 429:
                backoff_time = parse_retry_after(response.headers.get("Retry-After"))
                # another session serves user actions and playback meanwhile
                moves_on = self._has_fallback and current_priority() is not RequestPriority.LOW
                cooldown = max(backoff_time, self.throttler.initial_backoff)
                # the gate closes before the body is read, so nothing follows this
                # request to a client that just asked to wait
                self.throttler.set_cooldown(cooldown)
                reason = await _rate_limit_reason(response)
                self._last_rate_limit_reason = reason
                self._last_rate_limit_at = utc()
                message = f"Spotify rate limited the {self.label}"
                if reason:
                    message += f" ({reason})"
                quota_exceeded = reason == QUOTA_EXCEEDED
                warned = False
                if quota_exceeded:
                    self._quota_answers += 1
                    if warned := self._quota_answers == 1:
                        self._warn_quota_exceeded(backoff_time)
                if moves_on or (self._has_fallback and quota_exceeded):
                    # a used up quota does not come back within a retry either: the gate stays
                    # closed for the time asked and the request gives up right away
                    if quota_exceeded:
                        # a block that goes on is asked about less and less
                        doublings = min(self._quota_answers - 1, 10)
                        cooldown = max(
                            cooldown,
                            min(self.throttler.initial_backoff * 2**doublings, MAX_BACKOFF),
                        )
                    self.throttler.set_cooldown(cooldown)
                    if not warned:
                        # the caller moves on to the other session, so nothing else logs this
                        self.logger.info(message)
                    raise RetriesExhausted(message, translation_key=RateLimited.translation_key)
                raise RateLimited(message, backoff_time=backoff_time)
            self._quota_answers = 0
            # handle token expired, raise ResourceTemporarilyUnavailable
            # so it will be retried (and the token refreshed)
            if response.status == 401:
                self._on_unauthorized()
                raise ResourceTemporarilyUnavailable("Token expired", backoff_time=1)
            # handle temporary server error
            if response.status in (502, 503):
                raise ResourceTemporarilyUnavailable(backoff_time=30)
            yield response

    def _warn_quota_exceeded(self, asked_wait: float) -> None:
        """
        Log that the quota of this app is used up.

        :param asked_wait: Seconds Spotify asks to wait, 0 when it named no wait.
        """
        how_long = (
            f"Spotify asks to wait {asked_wait:.0f} seconds"
            if asked_wait
            else "Spotify did not say for how long"
        )
        self.logger.warning("The %s has used up its Spotify quota, %s", self.label, how_long)


async def _rate_limit_reason(response: aiohttp.ClientResponse) -> str | None:
    """Return the reason Spotify gives in the body of a 429 answer, None when it gives none."""
    try:
        async with asyncio.timeout(RATE_LIMIT_BODY_TIMEOUT):
            body = await response.json(loads=json_loads, content_type=None)
    except TimeoutError, aiohttp.ClientError, ValueError:
        return None
    if not isinstance(body, dict):
        return None
    # Spotify nests the details of an error under "error", accept a top level reason as well
    error = body.get("error")
    reason = (error.get("reason") if isinstance(error, dict) else None) or body.get("reason")
    return str(reason) if reason else None
