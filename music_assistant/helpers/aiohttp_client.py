"""Helpers for setting up a aiohttp session (and related)."""

from __future__ import annotations

import asyncio
import socket
import sys
from contextlib import suppress
from functools import cache
from ssl import SSLContext
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Self

import aiohttp
from aiohttp import web
from aiohttp.hdrs import USER_AGENT
from aiohttp_asyncmdnsresolver.api import AsyncDualMDNSResolver
from music_assistant_models.enums import EventType

from music_assistant.constants import APPLICATION_NAME

from . import ssl as ssl_util
from .json import json_dumps, json_loads

if TYPE_CHECKING:
    from aiohttp.typedefs import JSONDecoder
    from music_assistant_models.event import MassEvent

    from music_assistant.mass import MusicAssistant


MAXIMUM_CONNECTIONS = 4096
MAXIMUM_CONNECTIONS_PER_HOST = 100


def create_clientsession(
    mass: MusicAssistant,
    verify_ssl: bool = True,
    **kwargs: Any,
) -> aiohttp.ClientSession:
    """Create a new ClientSession with kwargs, i.e. for cookies."""
    clientsession = aiohttp.ClientSession(
        connector=_get_connector(mass, verify_ssl),
        json_serialize=json_dumps,
        response_class=MassClientResponse,
        **kwargs,
    )
    # Prevent packages accidentally overriding our default headers
    # It's important that we identify as Music Assistant
    # If a package requires a different user agent, override it by passing a headers
    # dictionary to the request method.
    user_agent = (
        f"{APPLICATION_NAME}/{mass.version} "
        f"aiohttp/{aiohttp.__version__} Python/{sys.version_info[0]}.{sys.version_info[1]}"
    )
    clientsession._default_headers = MappingProxyType(  # type: ignore[assignment]
        {USER_AGENT: user_agent},
    )
    return clientsession


async def async_aiohttp_proxy_stream(
    mass: MusicAssistant,
    request: web.BaseRequest,
    stream: aiohttp.StreamReader,
    content_type: str | None,
    buffer_size: int = 102400,
    timeout: int = 10,
) -> web.StreamResponse:
    """Stream a stream to aiohttp web response."""
    response = web.StreamResponse()
    if content_type is not None:
        response.content_type = content_type
    await response.prepare(request)

    # Suppressing something went wrong fetching data, closed connection
    with suppress(TimeoutError, aiohttp.ClientError):
        while not mass.closing:
            async with asyncio.timeout(timeout):
                data = await stream.read(buffer_size)

            if not data:
                break
            await response.write(data)

    return response


class MassAsyncDNSResolver(AsyncDualMDNSResolver):
    """Music Assistant AsyncDNSResolver.

    This is a wrapper around the AsyncDualMDNSResolver to only
    close the resolver when the Music Assistant instance is closed.
    """

    async def real_close(self) -> None:
        """Close the resolver."""
        await super().close()

    async def close(self) -> None:
        """Close the resolver."""


class MassClientResponse(aiohttp.ClientResponse):
    """aiohttp.ClientResponse with a json method that uses json_loads by default."""

    async def json(
        self,
        *args: Any,
        loads: JSONDecoder = json_loads,
        **kwargs: Any,
    ) -> Any:
        """Send a json request and parse the json response."""
        return await super().json(*args, loads=loads, **kwargs)


class ChunkAsyncStreamIterator:
    """
    Async iterator for chunked streams.

    Based on aiohttp.streams.ChunkTupleAsyncStreamIterator, but yields
    bytes instead of tuple[bytes, bool].
    """

    __slots__ = ("_stream",)

    def __init__(self, stream: aiohttp.StreamReader) -> None:
        """Initialize."""
        self._stream = stream

    def __aiter__(self) -> Self:
        """Iterate."""
        return self

    async def __anext__(self) -> bytes:
        """Yield next chunk."""
        rv = await self._stream.readchunk()
        if rv == (b"", False):
            raise StopAsyncIteration
        return rv[0]


class MusicAssistantTCPConnector(aiohttp.TCPConnector):
    """Music Assistant TCP Connector.

    Same as aiohttp.TCPConnector but with a longer cleanup_closed timeout.

    By default the cleanup_closed timeout is 2 seconds. This is too short
    for Music Assistant since we churn through a lot of connections. We set
    it to 60 seconds to reduce the overhead of aborting TLS connections
    that are likely already closed.
    """

    # abort transport after 60 seconds (cleanup broken connections)
    _cleanup_closed_period = 60.0


def _get_connector(
    mass: MusicAssistant,
    verify_ssl: bool = True,
    family: socket.AddressFamily = socket.AF_UNSPEC,
    ssl_cipher: ssl_util.SSLCipherList = ssl_util.SSLCipherList.PYTHON_DEFAULT,
) -> aiohttp.BaseConnector:
    """
    Return the connector pool for aiohttp.

    This method must be run in the event loop.
    """
    if verify_ssl:
        ssl_context: SSLContext = ssl_util.client_context(ssl_cipher)
    else:
        ssl_context = ssl_util.client_context_no_verify(ssl_cipher)

    return MusicAssistantTCPConnector(
        family=family,
        # Cleanup closed is no longer needed after https://github.com/python/cpython/pull/118960
        # which first appeared in Python 3.12.7 and 3.13.1
        enable_cleanup_closed=False,
        ssl=ssl_context,
        limit=MAXIMUM_CONNECTIONS,
        limit_per_host=MAXIMUM_CONNECTIONS_PER_HOST,
        resolver=_get_resolver(mass),
    )


@cache
def _get_resolver(mass: MusicAssistant) -> MassAsyncDNSResolver:
    """Return the MassAsyncDNSResolver."""
    resolver = MassAsyncDNSResolver(async_zeroconf=mass.aiozc)

    async def _close_resolver(event: MassEvent) -> None:  # noqa: ARG001
        await resolver.real_close()

    mass.subscribe(_close_resolver, EventType.SHUTDOWN)
    return resolver
