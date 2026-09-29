"""Tests for the aiohttp client session helper."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web

from music_assistant.helpers import aiohttp_client


@pytest.fixture
async def proxy_server() -> AsyncGenerator[tuple[str, list[str]]]:
    """Run a local forward proxy that records the URLs it is asked to fetch."""
    requested: list[str] = []

    async def _handler(request: web.Request) -> web.Response:
        requested.append(request.raw_path)
        return web.Response(text="via proxy")

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", _handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    yield f"http://127.0.0.1:{port}", requested
    await runner.cleanup()


async def test_clientsession_honors_proxy_env(
    proxy_server: tuple[str, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test requests are routed through the proxy set in the environment."""
    proxy_url, requested = proxy_server
    monkeypatch.setenv("HTTP_PROXY", proxy_url)
    monkeypatch.setenv("http_proxy", proxy_url)
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    mass = MagicMock()
    mass.version = "test"
    # the hostname is unresolvable, so the request can only succeed via the proxy
    with patch.object(aiohttp_client, "_get_resolver", return_value=None):
        session = aiohttp_client.create_clientsession(mass)
        async with session.get("http://origin.invalid/hello") as response:
            assert await response.text() == "via proxy"
        await session.close()
    assert requested == ["http://origin.invalid/hello"]
