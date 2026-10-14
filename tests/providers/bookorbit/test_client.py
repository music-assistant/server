"""Tests for the BookOrbit HTTP client."""

import asyncio
import json
from typing import Any

import pytest
from aiohttp import web

from music_assistant.providers.bookorbit.client import BookOrbitClient
from music_assistant.providers.bookorbit.helpers import is_audio_format, normalize_base_url


def _make(client: Any) -> BookOrbitClient:
    return BookOrbitClient(
        base_url=str(client.make_url("/")).rstrip("/"),
        username="u",
        password="p",
        verify_ssl=True,
        session=client.session,
    )


@pytest.mark.asyncio
async def test_login_stores_access_token_and_refresh_cookie(aiohttp_client: Any) -> None:
    """Login stores access token and refresh cookie."""

    async def login(request: web.Request) -> web.Response:
        assert (await request.json()) == {"username": "u", "password": "p"}
        resp = web.json_response({"accessToken": "access-1", "user": {"id": 1}})
        resp.set_cookie("refresh_token", "refresh-1", path="/")
        return resp

    app = web.Application()
    app.router.add_post("/api/v1/auth/login", login)
    client = await aiohttp_client(app)

    bo = _make(client)
    await bo.login()
    assert bo.access_token == "access-1"
    assert bo.refresh_token == "refresh-1"


@pytest.mark.asyncio
async def test_request_refreshes_on_401_then_retries(aiohttp_client: Any) -> None:
    """Request refreshes on 401 then retries."""
    state = {"calls": 0}

    async def libraries(request: web.Request) -> web.Response:
        state["calls"] += 1
        auth = request.headers.get("Authorization")
        if auth != "Bearer access-2":
            return web.Response(status=401)
        return web.json_response([])

    async def refresh(request: web.Request) -> web.Response:
        assert request.cookies.get("refresh_token") == "refresh-1"
        return web.json_response({"accessToken": "access-2"})

    app = web.Application()
    app.router.add_get("/api/v1/libraries", libraries)
    app.router.add_post("/api/v1/auth/refresh", refresh)
    client = await aiohttp_client(app)

    bo = _make(client)
    bo.access_token = "access-1"
    bo.refresh_token = "refresh-1"
    result = await bo.list_libraries()
    assert result == []
    assert bo.access_token == "access-2"
    assert state["calls"] == 2


@pytest.mark.asyncio
async def test_request_falls_back_to_login_when_refresh_fails(aiohttp_client: Any) -> None:
    """Request falls back to login when refresh fails."""

    async def libraries(request: web.Request) -> web.Response:
        if request.headers.get("Authorization") != "Bearer access-3":
            return web.Response(status=401)
        return web.json_response([{"id": 1, "name": "Audiobooks"}])

    async def refresh(_: web.Request) -> web.Response:
        return web.Response(status=401)

    async def login(_: web.Request) -> web.Response:
        resp = web.json_response({"accessToken": "access-3"})
        resp.set_cookie("refresh_token", "refresh-3", path="/")
        return resp

    app = web.Application()
    app.router.add_get("/api/v1/libraries", libraries)
    app.router.add_post("/api/v1/auth/refresh", refresh)
    app.router.add_post("/api/v1/auth/login", login)
    client = await aiohttp_client(app)

    bo = _make(client)
    bo.access_token = "stale"
    bo.refresh_token = "stale-refresh"
    result = await bo.list_libraries()
    assert result == [{"id": 1, "name": "Audiobooks"}]
    assert bo.access_token == "access-3"
    assert bo.refresh_token == "refresh-3"


@pytest.mark.asyncio
async def test_request_raises_when_still_unauthorized(aiohttp_client: Any) -> None:
    """Request raises when still unauthorized."""

    async def always_401(_: web.Request) -> web.Response:
        return web.Response(status=401)

    async def login(_: web.Request) -> web.Response:
        return web.json_response({"accessToken": "access-x"})

    app = web.Application()
    app.router.add_get("/api/v1/libraries", always_401)
    app.router.add_post("/api/v1/auth/refresh", always_401)
    app.router.add_post("/api/v1/auth/login", login)
    client = await aiohttp_client(app)

    bo = _make(client)
    bo.access_token = "stale"
    with pytest.raises(Exception, match="401"):
        await bo.list_libraries()


@pytest.mark.asyncio
async def test_query_audiobooks_filters_formats_and_paginates(aiohttp_client: Any) -> None:
    """Query audiobooks filters formats and paginates."""
    bodies: list[dict[str, Any]] = []

    async def query(request: web.Request) -> web.Response:
        body = json.loads(await request.text())
        bodies.append(body)
        page = body["pagination"]["page"]
        if page == 0:
            return web.json_response(
                {"items": [{"id": 1}, {"id": 2}], "total": 3, "page": 0, "size": 100}
            )
        return web.json_response({"items": [{"id": 3}], "total": 3, "page": 1, "size": 100})

    app = web.Application()
    app.router.add_post("/api/v1/libraries/{library_id}/books", query)
    client = await aiohttp_client(app)

    bo = _make(client)
    bo.access_token = "t"
    books = await bo.query_audiobooks(library_id=7)
    assert [b["id"] for b in books] == [1, 2, 3]
    assert len(bodies) == 2
    assert bodies[0] == {
        "filter": {
            "type": "group",
            "join": "AND",
            "rules": [
                {
                    "type": "rule",
                    "field": "format",
                    "operator": "includesAny",
                    "value": ["flac", "m4a", "m4b", "mp3", "ogg", "opus"],
                },
            ],
        },
        "sort": [{"field": "title", "dir": "asc"}],
        "pagination": {"page": 0, "size": 100},
    }
    assert bodies[1]["pagination"] == {"page": 1, "size": 100}


@pytest.mark.asyncio
async def test_query_audiobooks_omits_library_filter_when_unset(aiohttp_client: Any) -> None:
    """Query audiobooks omits library filter when library_id is None."""
    bodies: list[dict[str, Any]] = []

    async def query(request: web.Request) -> web.Response:
        bodies.append(json.loads(await request.text()))
        return web.json_response({"items": [], "total": 0, "page": 0, "size": 100})

    app = web.Application()
    app.router.add_post("/api/v1/books/query", query)
    client = await aiohttp_client(app)

    bo = _make(client)
    bo.access_token = "t"
    assert await bo.query_audiobooks() == []
    rules = bodies[0]["filter"]["rules"]
    assert len(rules) == 1
    assert rules[0]["field"] == "format"


@pytest.mark.asyncio
async def test_get_book_and_progress(aiohttp_client: Any) -> None:
    """Get book and progress."""
    saved: dict[str, Any] = {}

    async def book(request: web.Request) -> web.Response:
        return web.json_response({"id": int(request.match_info["id"])})

    async def get_progress(request: web.Request) -> web.Response:
        if request.match_info["id"] == "404":
            return web.Response(status=404)
        return web.json_response({"percentage": 12.5})

    async def put_progress(request: web.Request) -> web.Response:
        saved.update(await request.json())
        return web.json_response({})

    app = web.Application()
    app.router.add_get("/api/v1/books/{id}", book)
    app.router.add_get("/api/v1/books/{id}/audio-progress", get_progress)
    app.router.add_patch("/api/v1/books/{id}/audio-progress", put_progress)
    client = await aiohttp_client(app)

    bo = _make(client)
    bo.access_token = "t"
    assert (await bo.get_book(5)) == {"id": 5}
    assert (await bo.get_audio_progress(5)) == {"percentage": 12.5}
    assert (await bo.get_audio_progress(404)) is None
    await bo.upsert_audio_progress(5, percentage=50.0, current_file_id=9, position_seconds=12.0)
    assert saved["percentage"] == 50.0
    assert saved["currentFileId"] == 9
    assert saved["positionSeconds"] == 12.0


def test_stream_url_and_helpers() -> None:
    """Stream url and helpers."""
    bo = BookOrbitClient(
        base_url="http://host:3000/",
        username="u",
        password="p",
        verify_ssl=True,
    )
    assert bo.stream_url(42, "aud_11111111-1111-4111-8111-111111111111") == (
        "http://host:3000/api/v1/audiobooks/42/assets/"
        "aud_11111111-1111-4111-8111-111111111111/content"
    )
    assert normalize_base_url("http://x/") == "http://x"
    assert is_audio_format("M4B")
    assert not is_audio_format("epub")
    assert not is_audio_format(None)


@pytest.mark.asyncio
async def test_open_file_sends_bearer_and_range_and_retries_on_401(aiohttp_client: Any) -> None:
    """Assets are fetched with the bearer token; a 401 refreshes the token and retries."""
    seen: list[tuple[str | None, str | None]] = []
    asset_id = "aud_11111111-1111-4111-8111-111111111111"

    async def login(_request: web.Request) -> web.Response:
        resp = web.json_response({"accessToken": "stale"})
        resp.set_cookie("refresh_token", "refresh-1", path="/")
        return resp

    async def refresh(_request: web.Request) -> web.Response:
        return web.json_response({"accessToken": "fresh"})

    async def serve(request: web.Request) -> web.Response:
        auth = request.headers.get("Authorization")
        seen.append((auth, request.headers.get("Range")))
        if auth != "Bearer fresh":
            return web.Response(status=401)
        return web.Response(status=206, body=b"abc")

    app = web.Application()
    app.router.add_post("/api/v1/auth/login", login)
    app.router.add_post("/api/v1/auth/refresh", refresh)
    app.router.add_get("/api/v1/audiobooks/{book_id}/assets/{asset_id}/content", serve)
    client = await aiohttp_client(app)

    bo = _make(client)
    resp = await bo.open_file(7, asset_id, range_header="bytes=0-2")
    async with resp:
        assert resp.status == 206
        assert await resp.read() == b"abc"
    assert seen == [("Bearer stale", "bytes=0-2"), ("Bearer fresh", "bytes=0-2")]


@pytest.mark.asyncio
async def test_clients_on_same_host_keep_separate_tokens(aiohttp_server: Any) -> None:
    """Two clients of one server never share or overwrite each other's refresh cookie."""
    refresh_cookies: list[str] = []
    library_cookies: list[str | None] = []

    async def login(request: web.Request) -> web.Response:
        user = (await request.json())["username"]
        resp = web.json_response({"accessToken": f"access-{user}"})
        resp.set_cookie("refresh_token", f"refresh-{user}", path="/")
        return resp

    async def refresh(request: web.Request) -> web.Response:
        refresh_cookies.append(request.headers.get("Cookie", ""))
        user = request.cookies["refresh_token"].removeprefix("refresh-")
        return web.json_response({"accessToken": f"fresh-{user}"})

    async def libraries(request: web.Request) -> web.Response:
        library_cookies.append(request.headers.get("Cookie"))
        if not request.headers.get("Authorization", "").startswith("Bearer fresh-"):
            return web.Response(status=401)
        return web.json_response([])

    app = web.Application()
    app.router.add_post("/api/v1/auth/login", login)
    app.router.add_post("/api/v1/auth/refresh", refresh)
    app.router.add_get("/api/v1/libraries", libraries)
    server = await aiohttp_server(app)
    base = str(server.make_url("/")).rstrip("/")

    alice = BookOrbitClient(base_url=base, username="alice", password="p")
    bob = BookOrbitClient(base_url=base, username="bob", password="p")
    try:
        await alice.login()
        await bob.login()
        await alice.list_libraries()
        await bob.list_libraries()
    finally:
        await alice.close()
        await bob.close()
    assert refresh_cookies == ["refresh_token=refresh-alice", "refresh_token=refresh-bob"]
    assert (alice.refresh_token, bob.refresh_token) == ("refresh-alice", "refresh-bob")
    assert (alice.access_token, bob.access_token) == ("fresh-alice", "fresh-bob")
    # the refresh cookie is only sent to the refresh endpoint
    assert library_cookies == [None, None, None, None]
    assert alice.session.closed is False  # a new session is created on demand
    await alice.close()


@pytest.mark.asyncio
async def test_concurrent_401s_share_one_refresh(aiohttp_client: Any) -> None:
    """Requests rejected with the same token wait for a single refresh."""
    state = {"refreshes": 0}

    async def libraries(request: web.Request) -> web.Response:
        if request.headers.get("Authorization") != "Bearer fresh":
            return web.Response(status=401)
        return web.json_response([])

    async def refresh(_request: web.Request) -> web.Response:
        state["refreshes"] += 1
        await asyncio.sleep(0.05)
        return web.json_response({"accessToken": "fresh"})

    app = web.Application()
    app.router.add_get("/api/v1/libraries", libraries)
    app.router.add_post("/api/v1/auth/refresh", refresh)
    client = await aiohttp_client(app)

    bo = _make(client)
    bo.access_token = "stale"
    bo.refresh_token = "refresh-1"
    assert await asyncio.gather(*(bo.list_libraries() for _ in range(5))) == [[]] * 5
    assert state["refreshes"] == 1


@pytest.mark.asyncio
async def test_get_cover_returns_bytes_or_none(aiohttp_client: Any) -> None:
    """Covers are fetched with the bearer token; a missing cover is None."""

    async def cover(request: web.Request) -> web.Response:
        if request.match_info["id"] == "404":
            return web.Response(status=404)
        assert request.headers.get("Authorization") == "Bearer t"
        return web.Response(body=b"\x89PNG", content_type="image/png")

    app = web.Application()
    app.router.add_get("/api/v1/books/{id}/cover", cover)
    client = await aiohttp_client(app)

    bo = _make(client)
    bo.access_token = "t"
    assert await bo.get_cover(f"{bo.base_url}/api/v1/books/5/cover?t=1") == b"\x89PNG"
    assert await bo.get_cover(f"{bo.base_url}/api/v1/books/404/cover") is None
