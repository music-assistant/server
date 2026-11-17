"""MusicBrainz API client."""

from __future__ import annotations

import functools
import logging
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from music_assistant_models.errors import RateLimited, ResourceTemporarilyUnavailable

from music_assistant.constants import MASS_USER_AGENT
from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.json import json_loads
from music_assistant.helpers.throttle_retry import (
    ThrottlerManager,
    parse_retry_after,
    throttle_with_retries,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable

    from music_assistant.mass import MusicAssistant

MB_BASE_URL = "https://musicbrainz-mirror.music-assistant.io/ws/2"

_REFRESH_MB: ContextVar[bool] = ContextVar("refresh_musicbrainz", default=False)


@asynccontextmanager
async def refresh_musicbrainz() -> AsyncGenerator[None]:
    """Bypass the MusicBrainz cache for every lookup made inside this block."""
    token = _REFRESH_MB.set(True)
    try:
        yield
    finally:
        _REFRESH_MB.reset(token)


def _refresh_when_asked(func: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """
    Bypass the cache of a MusicBrainz lookup when inside :func:`refresh_musicbrainz`.

    handle_refresh sets BYPASS_CACHE for the duration of the wrapped call. This is safe
    because the wrapped function only does a plain HTTP request, with no calls to other
    providers' cached methods on the stack. The fresh answer replaces the stored entry.
    """

    @functools.wraps(func)
    async def wrapper(self: MusicBrainzAPIClient, /, endpoint: str, **kwargs: str) -> Any:
        if not _REFRESH_MB.get():
            return await func(self, endpoint, **kwargs)
        async with self.mass.cache.handle_refresh(True):
            return await func(self, endpoint, **kwargs)

    return wrapper


class MusicBrainzAPIClient:
    """Thin HTTP client for the MusicBrainz API."""

    domain = "musicbrainz"
    throttler = ThrottlerManager(rate_limit=30, period=10)

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize the API client."""
        self.mass = mass
        self.logger = logging.getLogger(__name__)

    @_refresh_when_asked
    @use_cache(86400 * 30)  # Cache for 30 days
    @throttle_with_retries
    async def get_data(self, /, endpoint: str, **kwargs: str) -> Any:
        """
        Fetch data from the MusicBrainz API.

        Results are cached for 30 days, and read past the cache inside
        :func:`refresh_musicbrainz`.

        :param endpoint: API endpoint path (e.g. ``artist/123?inc=aliases``).
        :param kwargs: Additional query parameters forwarded as URL params.
        """
        return await self._request(endpoint, **kwargs)

    @use_cache(86400)  # a URL not linked to exactly one entity is asked again after a day
    async def get_url_data(self, /, resource: str, inc: str) -> Any:
        """
        Fetch what MusicBrainz links to a URL.

        A URL linked to exactly one entity is cached for 30 days; an unknown URL, or one linked
        to several entities, only for a day, so a link a user adds or removes on MusicBrainz
        is picked up the next day.

        :param resource: The URL to look up, e.g. ``https://www.deezer.com/artist/805``.
        :param inc: The relations to include, e.g. ``artist-rels``.
        """
        settled_key = f"settled_url_data.{inc}.{resource}"
        if (data := await self.mass.cache.get(settled_key, provider=self.domain)) is not None:
            return data
        data = await self._get_url_data(resource=resource, inc=inc)
        if len(url_relation_ids(data, inc.removesuffix("-rels"))) == 1:
            await self.mass.cache.set(
                key=settled_key, data=data, expiration=86400 * 30, provider=self.domain
            )
        else:
            await self.mass.cache.delete(settled_key, provider=self.domain)
        return data

    @use_cache(86400 * 7)  # Cache for 7 days
    @throttle_with_retries
    async def get_browse_data(self, /, endpoint: str, **kwargs: str) -> Any:
        """
        Fetch a listing from the MusicBrainz API that grows as the catalog does.

        Results are cached for 7 days, so a discography picks up new releases within a week.

        :param endpoint: API endpoint path (e.g. ``release-group``).
        :param kwargs: Additional query parameters forwarded as URL params.
        """
        return await self._request(endpoint, **kwargs)

    @throttle_with_retries
    async def _get_url_data(self, /, resource: str, inc: str) -> Any:
        """Fetch what MusicBrainz links to a URL, without caching."""
        return await self._request("url", resource=resource, inc=inc)

    async def _request(self, endpoint: str, **params: str) -> Any:
        """Perform one request against the MusicBrainz API and return its JSON body."""
        url = f"{MB_BASE_URL}/{endpoint}"
        headers = {"User-Agent": MASS_USER_AGENT}
        params["fmt"] = "json"
        async with self.mass.http_session.get(url, headers=headers, params=params) as response:
            # handle rate limiter
            if response.status == 429:
                backoff_time = parse_retry_after(response.headers.get("Retry-After"))
                raise RateLimited("Rate Limiter", backoff_time=backoff_time)
            # handle temporary server error
            if response.status in (502, 503):
                raise ResourceTemporarilyUnavailable(backoff_time=30)
            # handle 404 not found
            if response.status in (400, 401, 404):
                return None
            response.raise_for_status()
            return await response.json(loads=json_loads)


def url_relation_ids(url_data: Any, entity: str) -> set[str]:
    """
    Return the ids of the entities of one kind a MusicBrainz URL lookup links the URL to.

    :param url_data: The answer of a URL lookup, None for a URL MusicBrainz does not know.
    :param entity: The kind of entity, e.g. ``artist``, ``release`` or ``recording``.
    """
    if not url_data:
        return set()
    return {
        target["id"]
        for relation in url_data.get("relations", [])
        if not relation.get("ended") and (target := relation.get(entity)) and target.get("id")
    }
