"""Tests for requests to the Alexa skill's API."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from unittest.mock import MagicMock

import aiohttp
import pytest

from music_assistant.providers.alexa import api_request

IDLE_TIMEOUT = 0.1


async def _skill_server(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Answer requests, but close an idle connection as the next request arrives."""
    loop = asyncio.get_running_loop()
    served = False
    try:
        while True:
            idle_since = loop.time()
            head = await reader.readuntil(b"\r\n\r\n")
            if served and loop.time() - idle_since > IDLE_TIMEOUT:
                break
            served = True
            length = next(
                int(line.split(":")[1])
                for line in head.decode().split("\r\n")
                if line.lower().startswith("content-length")
            )
            await reader.readexactly(length)
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b"Content-Length: 2\r\nConnection: keep-alive\r\n\r\n{}"
            )
            await writer.drain()
    except asyncio.IncompleteReadError:
        pass
    writer.close()


@pytest.fixture
async def provider() -> AsyncIterator[MagicMock]:
    """Return a provider whose skill API URL points at a local skill server."""
    server = await asyncio.start_server(_skill_server, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    setup_values = {"api_url": f"http://127.0.0.1:{port}"}
    async with aiohttp.ClientSession() as session:
        provider = MagicMock()
        provider.mass.http_session = session
        provider.get_setup_value.side_effect = setup_values.get
        yield provider
    server.close()
    await server.wait_closed()


async def test_push_after_idle_gap_survives_closed_keepalive(provider: MagicMock) -> None:
    """A push on a pooled connection the skill server closed while idle still succeeds."""
    assert await api_request(provider, "/ma/push-url", json_data={"a": 1}) == "{}"
    await asyncio.sleep(IDLE_TIMEOUT * 3)
    assert await api_request(provider, "/ma/push-url", json_data={"a": 1}) == "{}"
