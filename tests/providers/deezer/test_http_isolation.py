"""Test Deezer account isolation over a shared aiohttp session."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from aiohttp import ClientSession, CookieJar, web
from aiohttp.test_utils import TestServer
from deezer_python_gql import DeezerGQLClient

from music_assistant.providers.deezer import gw_client


@pytest.fixture
async def deezer_server(gw_user_data: dict[str, Any]) -> AsyncGenerator[TestServer]:
    """Serve account-aware Deezer responses using synthetic credentials."""

    async def auth(request: web.Request) -> web.Response:
        cookies = dict(request.cookies)
        # Model the reported first-account-wins behavior when a refresh token leaks.
        account = cookies.get("refresh-token") or cookies["arl"]
        payload = (
            base64.urlsafe_b64encode(json.dumps({"sub": account, "exp": 4102444800}).encode())
            .decode()
            .rstrip("=")
        )
        response = web.json_response({"jwt": f"header.{payload}.signature"})
        response.set_cookie("refresh-token", account)
        return response

    async def pipe(request: web.Request) -> web.Response:
        cookies = dict(request.cookies)
        assert not cookies.get("arl")
        jwt = request.headers["Authorization"].removeprefix("Bearer ")
        payload = jwt.split(".")[1]
        account = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["sub"]
        account = cookies.get("refresh-token") or account
        return web.json_response({"data": {"me": {"id": account}}})

    async def gateway(request: web.Request) -> web.Response:
        cookies = dict(request.cookies)
        account = cookies["arl"]
        if any(value and value != account for value in cookies.values()):
            return web.json_response({"error": {"VALID_TOKEN_REQUIRED": "Foreign cookie"}})
        if request.query["method"] == "deezer.getUserData":
            data = gw_user_data
            data["results"]["USER"]["USER_ID"] = account if cookies.get("sid") else 0
            data["results"]["USER"]["OPTIONS"]["license_token"] = account
        else:
            data = {"error": [], "results": {"SNG_ID": "1", "TRACK_TOKEN": account}}
        response = web.json_response(data)
        response.set_cookie("sid", account)
        response.set_cookie("dzr_uniq_id", account)
        return response

    async def media(request: web.Request) -> web.Response:
        cookies = dict(request.cookies)
        data = await request.json()
        account = data["license_token"]
        assert data["track_tokens"] == [account]
        assert cookies["arl"] == account
        assert all(not value or value == account for value in cookies.values())
        return web.json_response({"data": [{"media": [{"account": account}]}]})

    app = web.Application()
    app.router.add_post("/auth", auth)
    app.router.add_post("/pipe", pipe)
    app.router.add_post("/gw", gateway)
    app.router.add_post("/media", media)
    server = TestServer(app)
    async with server:
        yield server


@pytest.mark.parametrize("account_ids", [("123", "456"), ("456", "123")])
@pytest.mark.parametrize("concurrent", [False, True])
async def test_shared_session_isolates_accounts(
    monkeypatch: pytest.MonkeyPatch,
    deezer_server: TestServer,
    account_ids: tuple[str, str],
    concurrent: bool,
) -> None:
    """Keep authentication, metadata and media requests tied to their own accounts."""
    server = deezer_server
    async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as session:
        monkeypatch.setattr(DeezerGQLClient, "AUTH_URL", str(server.make_url("/auth")))
        monkeypatch.setattr(gw_client, "GW_LIGHT_URL", str(server.make_url("/gw")))
        monkeypatch.setattr(gw_client, "MEDIA_GET_URL", str(server.make_url("/media")))
        gql_clients = [
            DeezerGQLClient(account, url=str(server.make_url("/pipe")), session=session)
            for account in account_ids
        ]
        gw_clients = [gw_client.GWClient(session, account) for account in account_ids]

        async def check_account(index: int) -> None:
            me = await gql_clients[index].get_me()
            assert me is not None
            assert me.id == account_ids[index]
            await gw_clients[index].setup()
            assert gw_clients[index]._user_id == int(account_ids[index])
            result, _ = await gw_clients[index].get_deezer_track_urls("1")
            assert result["account"] == account_ids[index]

        if concurrent:
            await asyncio.gather(check_account(0), check_account(1))
        else:
            await check_account(0)
            await check_account(1)

        # Force JWT and license refreshes after both accounts populated the shared jar.
        for index in (1, 0):
            gql_clients[index]._jwt_expires_at = 0
            gw_clients[index]._license_expiration_timestamp = 0
            me = await gql_clients[index].get_me()
            assert me is not None
            assert me.id == account_ids[index]
            result, _ = await gw_clients[index].get_deezer_track_urls("1")
            assert result["account"] == account_ids[index]

        # A reloaded provider must not inherit the account left in the shared session.
        replacement = DeezerGQLClient(
            account_ids[1], url=str(server.make_url("/pipe")), session=session
        )
        me = await replacement.get_me()
        assert me is not None
        assert me.id == account_ids[1]
        assert not session.closed
