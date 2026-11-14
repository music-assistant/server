"""Test that Deezer's GraphQL and gw-light requests respect the account's rate limit."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncGenerator
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
from aiohttp import ClientSession, CookieJar, web
from aiohttp.test_utils import TestServer
from music_assistant_models.errors import RetriesExhausted

from music_assistant.helpers.throttle_retry import ThrottlerManager
from music_assistant.providers.deezer import gw_client
from music_assistant.providers.deezer.gql_client import ThrottledGQLClient
from music_assistant.providers.deezer.provider import DeezerProvider

JWT_PAYLOAD = base64.urlsafe_b64encode(json.dumps({"exp": 4102444800}).encode()).decode()


@pytest.fixture
def rate_limits() -> dict[str, str | None]:
    """Return the paths whose next request gets a 429, with the Retry-After it carries."""
    return {}


@pytest.fixture
async def deezer_server(
    gw_user_data: dict[str, Any], rate_limits: dict[str, str | None]
) -> AsyncGenerator[TestServer]:
    """Serve Deezer responses, answering a 429 to the requests of `rate_limits`."""

    def limited(request: web.Request) -> web.Response | None:
        if request.path not in rate_limits:
            return None
        retry_after = rate_limits.pop(request.path)
        return web.Response(status=429, headers={"Retry-After": retry_after} if retry_after else {})

    async def auth(request: web.Request) -> web.Response:
        return limited(request) or web.json_response({"jwt": f"header.{JWT_PAYLOAD}.sig"})

    async def pipe(request: web.Request) -> web.Response:
        return limited(request) or web.json_response({"data": {"me": {"id": "123"}}})

    async def gateway(request: web.Request) -> web.Response:
        if request.query["method"] == "deezer.getUserData":
            data = gw_user_data
        else:
            data = {"error": [], "results": {"SNG_ID": "1", "TRACK_TOKEN": "token"}}
        response = limited(request) or web.json_response(data)
        response.set_cookie("sid", "session")
        return response

    async def media(request: web.Request) -> web.Response:
        return limited(request) or web.json_response({"data": [{"media": [{"url": "x"}]}]})

    app = web.Application()
    app.router.add_post("/auth", auth)
    app.router.add_post("/pipe", pipe)
    app.router.add_post("/gw", gateway)
    app.router.add_post("/media", media)
    server = TestServer(app)
    async with server:
        yield server


@pytest.fixture
async def session() -> AsyncGenerator[ClientSession]:
    """Return a session with a jar of its own."""
    async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as client_session:
        yield client_session


@pytest.fixture
def gw_urls(monkeypatch: pytest.MonkeyPatch, deezer_server: TestServer) -> None:
    """Point the gw-light client at the test server."""
    monkeypatch.setattr(gw_client, "GW_LIGHT_URL", str(deezer_server.make_url("/gw")))
    monkeypatch.setattr(gw_client, "MEDIA_GET_URL", str(deezer_server.make_url("/media")))


async def test_clients_share_one_throttler(provider: DeezerProvider) -> None:
    """The GraphQL and gw-light clients of a provider instance share one rate limit."""
    cast("Mock", provider.mass).config.get.return_value = {}
    with (
        patch("music_assistant.providers.deezer.provider.ThrottledGQLClient") as gql,
        patch("music_assistant.providers.deezer.provider.GWClient") as gw,
    ):
        gql.return_value.get_me = AsyncMock(return_value=Mock(id="123"))
        gw.return_value.setup = AsyncMock()
        await provider.handle_async_init()

    throttler = gql.call_args.kwargs["throttler"]
    assert isinstance(throttler, ThrottlerManager)
    assert gw.call_args.kwargs["throttler"] is throttler


@pytest.mark.usefixtures("gw_urls")
async def test_requests_take_a_slot(
    deezer_server: TestServer, session: ClientSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every GraphQL, gw-light and stream url request takes a slot of the shared throttler."""
    throttler = ThrottlerManager(rate_limit=100, period=60)
    acquire = Mock(wraps=throttler.acquire)
    monkeypatch.setattr(throttler, "acquire", acquire)
    gql = _gql_client(deezer_server, session, throttler)
    gw = gw_client.GWClient(session, "arl", throttler=throttler)

    # the token refresh shares the slot of the query it precedes
    assert await gql.get_me() is not None
    assert acquire.call_count == 1
    await gw.setup()
    assert acquire.call_count == 2
    # song.getData and the stream url request
    await gw.get_deezer_track_urls("1")
    assert acquire.call_count == 4


@pytest.mark.parametrize("path", ["/auth", "/pipe"])
async def test_gql_rate_limit_holds_back_the_account(
    deezer_server: TestServer,
    session: ClientSession,
    rate_limits: dict[str, str | None],
    path: str,
) -> None:
    """A 429 of the GraphQL API or its token refresh arms the cooldown of the throttler."""
    throttler = ThrottlerManager(rate_limit=100, period=60, retry_attempts=1, initial_backoff=30)
    gql = _gql_client(deezer_server, session, throttler)
    rate_limits[path] = None

    with pytest.raises(RetriesExhausted):
        await gql.get_me()
    assert throttler.cooldown_remaining > 25


async def test_gql_retries_after_rate_limit(
    deezer_server: TestServer, session: ClientSession, rate_limits: dict[str, str | None]
) -> None:
    """A rate limited GraphQL request is sent again once the backoff passed."""
    throttler = ThrottlerManager(rate_limit=100, period=60, initial_backoff=0)
    gql = _gql_client(deezer_server, session, throttler)
    rate_limits["/pipe"] = None

    me = await gql.get_me()

    assert me is not None
    assert me.id == "123"


@pytest.mark.usefixtures("gw_urls")
@pytest.mark.parametrize("path", ["/gw", "/media"])
async def test_gw_rate_limit_honors_retry_after(
    session: ClientSession, rate_limits: dict[str, str | None], path: str
) -> None:
    """A 429 of the gw-light or stream url API holds back the account for its Retry-After."""
    throttler = ThrottlerManager(rate_limit=100, period=60, retry_attempts=1, initial_backoff=0)
    gw = gw_client.GWClient(session, "arl", throttler=throttler)
    await gw.setup()
    rate_limits[path] = "30"

    with pytest.raises(RetriesExhausted):
        await gw.get_deezer_track_urls("1")
    assert throttler.cooldown_remaining > 25


@pytest.mark.usefixtures("gw_urls")
async def test_gw_retries_after_rate_limit(
    session: ClientSession, rate_limits: dict[str, str | None]
) -> None:
    """A rate limited gw-light request is sent again once the backoff passed."""
    throttler = ThrottlerManager(rate_limit=100, period=60, initial_backoff=0)
    gw = gw_client.GWClient(session, "arl", throttler=throttler)
    rate_limits["/gw"] = None

    await gw.setup()

    assert gw.user_country == "DE"


def _gql_client(
    server: TestServer, session: ClientSession, throttler: ThrottlerManager
) -> ThrottledGQLClient:
    """Return a GraphQL client that talks to the test server."""
    client = ThrottledGQLClient("arl", session, throttler)
    client.url = str(server.make_url("/pipe"))
    client.AUTH_URL = str(server.make_url("/auth"))
    return client
