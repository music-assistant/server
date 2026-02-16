"""Tests for webserver dynamic routing with and without base path (subpath) support."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Callable, Coroutine
from typing import Any

import pytest
from aiohttp import ClientSession, web

from music_assistant.helpers.webserver import Webserver


def _get_bound_port(ws: Webserver) -> int:
    """Extract the actual TCP port from the running server's socket.

    :param ws: A Webserver instance that has been set up and is listening.
    """
    assert ws._tcp_site is not None
    assert ws._tcp_site._server is not None
    sockets = ws._tcp_site._server.sockets  # type: ignore[attr-defined]
    assert sockets
    return int(sockets[0].getsockname()[1])


def _make_handler(
    body: str, status: int = 200
) -> Callable[[web.Request], Coroutine[Any, Any, web.Response]]:
    """Create a simple dynamic route handler returning a fixed text response.

    :param body: The response body text.
    :param status: The HTTP status code to return.
    """

    async def handler(_request: web.Request) -> web.Response:
        return web.Response(text=body, status=status)

    return handler


@pytest.fixture
async def webserver_no_base_path() -> AsyncGenerator[tuple[Webserver, str], None]:
    """Create a Webserver with dynamic routes and no base path, listening on a random port.

    Yields a tuple of (Webserver, base_url_with_port) for making HTTP requests.
    """
    logger = logging.getLogger("test_webserver_routing.no_base")
    ws = Webserver(logger, enable_dynamic_routes=True)
    await ws.setup(
        bind_ip="127.0.0.1",
        bind_port=0,
        base_url="http://127.0.0.1",
    )
    port = _get_bound_port(ws)
    try:
        yield ws, f"http://127.0.0.1:{port}"
    finally:
        await ws.close()


@pytest.fixture
async def webserver_with_base_path() -> AsyncGenerator[tuple[Webserver, str], None]:
    """Create a Webserver with dynamic routes and base path '/music', on a random port.

    Yields a tuple of (Webserver, base_url_with_port) for making HTTP requests.
    """
    logger = logging.getLogger("test_webserver_routing.with_base")
    ws = Webserver(logger, enable_dynamic_routes=True)
    await ws.setup(
        bind_ip="127.0.0.1",
        bind_port=0,
        base_url="http://127.0.0.1/music",
    )
    port = _get_bound_port(ws)
    try:
        yield ws, f"http://127.0.0.1:{port}"
    finally:
        await ws.close()


@pytest.fixture
async def http_session() -> AsyncGenerator[ClientSession, None]:
    """Create an aiohttp ClientSession for making test HTTP requests."""
    async with ClientSession() as session:
        yield session


# ============================================================================
# No base path (standard setup — existing behaviour must stay correct)
# ============================================================================


async def test_exact_get_route(
    webserver_no_base_path: tuple[Webserver, str],
    http_session: ClientSession,
) -> None:
    """Test that a registered GET dynamic route is matched exactly.

    :param webserver_no_base_path: Webserver fixture with no base path.
    :param http_session: aiohttp client session.
    """
    ws, url = webserver_no_base_path
    ws.register_dynamic_route("/callback", _make_handler("get_callback"), method="GET")

    async with http_session.get(f"{url}/callback") as resp:
        assert resp.status == 200
        assert await resp.text() == "get_callback"


async def test_exact_post_route(
    webserver_no_base_path: tuple[Webserver, str],
    http_session: ClientSession,
) -> None:
    """Test that a registered POST dynamic route is matched exactly.

    :param webserver_no_base_path: Webserver fixture with no base path.
    :param http_session: aiohttp client session.
    """
    ws, url = webserver_no_base_path
    ws.register_dynamic_route("/submit", _make_handler("post_submit"), method="POST")

    async with http_session.post(f"{url}/submit") as resp:
        assert resp.status == 200
        assert await resp.text() == "post_submit"


async def test_wildcard_method_route(
    webserver_no_base_path: tuple[Webserver, str],
    http_session: ClientSession,
) -> None:
    """Test that a route registered with method='*' matches GET, POST, and PUT.

    :param webserver_no_base_path: Webserver fixture with no base path.
    :param http_session: aiohttp client session.
    """
    ws, url = webserver_no_base_path
    ws.register_dynamic_route("/any", _make_handler("any_method"))

    for method_func in (http_session.get, http_session.post, http_session.put):
        async with method_func(f"{url}/any") as resp:
            assert resp.status == 200
            assert await resp.text() == "any_method"


async def test_prefix_wildcard_route(
    webserver_no_base_path: tuple[Webserver, str],
    http_session: ClientSession,
) -> None:
    """Test that a route registered as '/api/*' matches '/api/users' and '/api/items/1'.

    :param webserver_no_base_path: Webserver fixture with no base path.
    :param http_session: aiohttp client session.
    """
    ws, url = webserver_no_base_path
    ws.register_dynamic_route("/api/*", _make_handler("api_wildcard"))

    async with http_session.get(f"{url}/api/users") as resp:
        assert resp.status == 200
        assert await resp.text() == "api_wildcard"

    async with http_session.get(f"{url}/api/items/1") as resp:
        assert resp.status == 200
        assert await resp.text() == "api_wildcard"


async def test_unregistered_path_returns_404(
    webserver_no_base_path: tuple[Webserver, str],
    http_session: ClientSession,
) -> None:
    """Test that a request to an unregistered path returns 404.

    :param webserver_no_base_path: Webserver fixture with no base path.
    :param http_session: aiohttp client session.
    """
    _ws, url = webserver_no_base_path

    async with http_session.get(f"{url}/nonexistent") as resp:
        assert resp.status == 404


async def test_unregister_callable(
    webserver_no_base_path: tuple[Webserver, str],
    http_session: ClientSession,
) -> None:
    """Test that the callable returned by register_dynamic_route removes the route.

    :param webserver_no_base_path: Webserver fixture with no base path.
    :param http_session: aiohttp client session.
    """
    ws, url = webserver_no_base_path
    unregister = ws.register_dynamic_route("/temp", _make_handler("temporary"))

    async with http_session.get(f"{url}/temp") as resp:
        assert resp.status == 200

    unregister()

    async with http_session.get(f"{url}/temp") as resp:
        assert resp.status == 404


async def test_unregister_dynamic_route(
    webserver_no_base_path: tuple[Webserver, str],
    http_session: ClientSession,
) -> None:
    """Test that unregister_dynamic_route removes a previously registered route.

    :param webserver_no_base_path: Webserver fixture with no base path.
    :param http_session: aiohttp client session.
    """
    ws, url = webserver_no_base_path
    ws.register_dynamic_route("/removeme", _make_handler("remove_me"))

    async with http_session.get(f"{url}/removeme") as resp:
        assert resp.status == 200

    ws.unregister_dynamic_route("/removeme")

    async with http_session.get(f"{url}/removeme") as resp:
        assert resp.status == 404


async def test_duplicate_route_raises_error(
    webserver_no_base_path: tuple[Webserver, str],
) -> None:
    """Test that registering the same route twice raises RuntimeError.

    :param webserver_no_base_path: Webserver fixture with no base path.
    """
    ws, _url = webserver_no_base_path
    ws.register_dynamic_route("/dup", _make_handler("first"))

    with pytest.raises(RuntimeError, match="already registered"):
        ws.register_dynamic_route("/dup", _make_handler("second"))


async def test_dynamic_routes_disabled_raises_error() -> None:
    """Test that registering a route when dynamic routes are disabled raises RuntimeError."""
    logger = logging.getLogger("test_webserver_routing.disabled")
    ws = Webserver(logger, enable_dynamic_routes=False)

    with pytest.raises(RuntimeError, match="not enabled"):
        ws.register_dynamic_route("/nope", _make_handler("nope"))

    with pytest.raises(RuntimeError, match="not enabled"):
        ws.unregister_dynamic_route("/nope")


# ============================================================================
# With base path /music (the PR fix — subpath behind reverse proxy)
# ============================================================================


async def test_prefixed_request_strips_prefix(
    webserver_with_base_path: tuple[Webserver, str],
    http_session: ClientSession,
) -> None:
    """Test that /music/callback strips /music prefix and matches /callback handler.

    :param webserver_with_base_path: Webserver fixture with '/music' base path.
    :param http_session: aiohttp client session.
    """
    ws, url = webserver_with_base_path
    ws.register_dynamic_route("/callback", _make_handler("callback_ok"))

    async with http_session.get(f"{url}/music/callback") as resp:
        assert resp.status == 200
        assert await resp.text() == "callback_ok"


async def test_root_request_still_works_with_base_path(
    webserver_with_base_path: tuple[Webserver, str],
    http_session: ClientSession,
) -> None:
    """Test that /callback still works via root catch-all when base path is set.

    :param webserver_with_base_path: Webserver fixture with '/music' base path.
    :param http_session: aiohttp client session.
    """
    ws, url = webserver_with_base_path
    ws.register_dynamic_route("/callback", _make_handler("callback_direct"))

    async with http_session.get(f"{url}/callback") as resp:
        assert resp.status == 200
        assert await resp.text() == "callback_direct"


async def test_bare_base_path_redirects(
    webserver_with_base_path: tuple[Webserver, str],
    http_session: ClientSession,
) -> None:
    """Test that GET /music redirects (302) to /music/.

    :param webserver_with_base_path: Webserver fixture with '/music' base path.
    :param http_session: aiohttp client session.
    """
    _ws, url = webserver_with_base_path

    async with http_session.get(f"{url}/music", allow_redirects=False) as resp:
        assert resp.status == 302
        assert resp.headers["Location"] == "/music/"


async def test_prefixed_wildcard_route(
    webserver_with_base_path: tuple[Webserver, str],
    http_session: ClientSession,
) -> None:
    """Test that /music/api/something matches the /api/* handler via prefix stripping.

    :param webserver_with_base_path: Webserver fixture with '/music' base path.
    :param http_session: aiohttp client session.
    """
    ws, url = webserver_with_base_path
    ws.register_dynamic_route("/api/*", _make_handler("api_prefixed"))

    async with http_session.get(f"{url}/music/api/something") as resp:
        assert resp.status == 200
        assert await resp.text() == "api_prefixed"


async def test_root_wildcard_route_with_base_path(
    webserver_with_base_path: tuple[Webserver, str],
    http_session: ClientSession,
) -> None:
    """Test that /api/something still matches /api/* handler via root catch-all.

    :param webserver_with_base_path: Webserver fixture with '/music' base path.
    :param http_session: aiohttp client session.
    """
    ws, url = webserver_with_base_path
    ws.register_dynamic_route("/api/*", _make_handler("api_direct"))

    async with http_session.get(f"{url}/api/something") as resp:
        assert resp.status == 200
        assert await resp.text() == "api_direct"


async def test_unregistered_prefixed_path_returns_404(
    webserver_with_base_path: tuple[Webserver, str],
    http_session: ClientSession,
) -> None:
    """Test that /music/nonexistent returns 404 when no handler is registered.

    :param webserver_with_base_path: Webserver fixture with '/music' base path.
    :param http_session: aiohttp client session.
    """
    _ws, url = webserver_with_base_path

    async with http_session.get(f"{url}/music/nonexistent") as resp:
        assert resp.status == 404


async def test_base_path_property(
    webserver_with_base_path: tuple[Webserver, str],
) -> None:
    """Test that the base_path property returns '/music' when configured with a subpath.

    :param webserver_with_base_path: Webserver fixture with '/music' base path.
    """
    ws, _url = webserver_with_base_path
    assert ws.base_path == "/music"


# ============================================================================
# Edge cases
# ============================================================================


async def test_prefix_strip_to_root(
    webserver_with_base_path: tuple[Webserver, str],
    http_session: ClientSession,
) -> None:
    """Test that a request to /music/ resolves to '/' after prefix stripping.

    :param webserver_with_base_path: Webserver fixture with '/music' base path.
    :param http_session: aiohttp client session.
    """
    ws, url = webserver_with_base_path
    ws.register_dynamic_route("/", _make_handler("root_handler"))

    async with http_session.get(f"{url}/music/") as resp:
        assert resp.status == 200
        assert await resp.text() == "root_handler"


async def test_multi_level_base_path(http_session: ClientSession) -> None:
    """Test that a multi-level base path like '/ha/music' works correctly.

    :param http_session: aiohttp client session.
    """
    logger = logging.getLogger("test_webserver_routing.multi_level")
    ws = Webserver(logger, enable_dynamic_routes=True)
    await ws.setup(
        bind_ip="127.0.0.1",
        bind_port=0,
        base_url="http://127.0.0.1/ha/music",
    )
    port = _get_bound_port(ws)
    url = f"http://127.0.0.1:{port}"

    try:
        ws.register_dynamic_route("/callback", _make_handler("multi_level_ok"))
        assert ws.base_path == "/ha/music"

        # Prefixed path should work
        async with http_session.get(f"{url}/ha/music/callback") as resp:
            assert resp.status == 200
            assert await resp.text() == "multi_level_ok"

        # Root path should also work
        async with http_session.get(f"{url}/callback") as resp:
            assert resp.status == 200
            assert await resp.text() == "multi_level_ok"

        # Bare base path should redirect
        async with http_session.get(f"{url}/ha/music", allow_redirects=False) as resp:
            assert resp.status == 302
            assert resp.headers["Location"] == "/ha/music/"
    finally:
        await ws.close()


async def test_base_url_trailing_slash_normalized() -> None:
    """Test that a base_url with trailing slash produces a clean base_path without it."""
    logger = logging.getLogger("test_webserver_routing.trailing_slash")
    ws = Webserver(logger, enable_dynamic_routes=True)
    await ws.setup(
        bind_ip="127.0.0.1",
        bind_port=0,
        base_url="http://127.0.0.1/music/",
    )

    try:
        assert ws.base_path == "/music"
        assert ws.base_url == "http://127.0.0.1/music"
    finally:
        await ws.close()


async def test_no_path_in_base_url(
    webserver_no_base_path: tuple[Webserver, str],
) -> None:
    """Test that a plain base_url without a path results in empty base_path.

    :param webserver_no_base_path: Webserver fixture with no base path.
    """
    ws, _url = webserver_no_base_path
    assert ws.base_path == ""
