"""End-to-end tests through the real ASGI bridge loop (C11).

These tests exercise ``mount_into_mass`` against an aiohttp ``TestServer``
hosting a hand-rolled ASGI app. They cover the bits that pure-helper unit
tests can't reach: streaming chunk pass-through, DELETE / non-GET methods,
the well-known endpoint living next to the MCP mount.
"""
# mypy: disable-error-code="arg-type, no-untyped-def, type-arg, assignment, operator, misc"

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from music_assistant.providers.fastmcp_server.http_bridge import mount_into_mass, mount_well_known

from .conftest import FakeWebserver, build_aiohttp_app


async def _streaming_asgi(scope: dict, receive: Any, send: Any) -> None:  # noqa: ARG001
    """ASGI app that emits three SSE-style chunks before closing the body."""
    # Drain the request body so the client write side can complete.
    while True:
        msg = await receive()
        if msg.get("type") == "http.request" and not msg.get("more_body"):
            break
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/event-stream")],
        }
    )
    for i in range(3):
        await send(
            {"type": "http.response.body", "body": f"event{i}\n".encode(), "more_body": True}
        )
    await send({"type": "http.response.body", "body": b"", "more_body": False})


async def _method_echo_asgi(scope: dict, receive: Any, send: Any) -> None:
    """ASGI app that echoes the HTTP method in the body."""
    while True:
        msg = await receive()
        if msg.get("type") == "http.request" and not msg.get("more_body"):
            break
    method = scope["method"].encode()
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": method})


class _Mcp:
    def __init__(self, asgi: Any) -> None:
        self._asgi = asgi

    def http_app(self, transport: str = "streamable-http") -> Any:
        return self._asgi


@pytest.fixture
async def streaming_client() -> Any:
    """Bridge an SSE-streaming ASGI app through mount_into_mass."""
    ws = FakeWebserver()
    mass = SimpleNamespace(webserver=ws)
    await mount_into_mass(mass, _Mcp(_streaming_asgi), mount_path="/mcp/v1")
    async with TestClient(TestServer(build_aiohttp_app(ws))) as client:
        yield client


@pytest.fixture
async def method_echo_client() -> Any:
    """Bridge a method-echo ASGI app to verify DELETE / arbitrary verbs work."""
    ws = FakeWebserver()
    mass = SimpleNamespace(webserver=ws)
    await mount_into_mass(mass, _Mcp(_method_echo_asgi), mount_path="/mcp/v1")
    async with TestClient(TestServer(build_aiohttp_app(ws))) as client:
        yield client


async def test_streaming_chunks_passed_through(streaming_client: TestClient) -> None:
    """Three ASGI body chunks reach the aiohttp client unbuffered."""
    resp = await streaming_client.post("/mcp/v1/", headers={"Origin": "http://localhost:8095"})
    assert resp.status == 200
    body = await resp.read()
    assert body == b"event0\nevent1\nevent2\n"


async def test_delete_method_reaches_asgi(method_echo_client: TestClient) -> None:
    """Streamable-HTTP DELETE (session terminate) is forwarded — bridge does not 405."""
    resp = await method_echo_client.delete("/mcp/v1/", headers={"Origin": "http://localhost:8095"})
    assert resp.status == 200
    assert (await resp.read()) == b"DELETE"


async def test_get_method_reaches_asgi(method_echo_client: TestClient) -> None:
    """GET is forwarded — required so FastMCP can open server-initiated SSE."""
    resp = await method_echo_client.get("/mcp/v1/", headers={"Origin": "http://localhost:8095"})
    assert resp.status == 200
    assert (await resp.read()) == b"GET"


async def test_well_known_alongside_mcp_mount() -> None:
    """Both /mcp/v1/* and /.well-known/oauth-protected-resource are reachable."""
    ws = FakeWebserver()
    mass = SimpleNamespace(webserver=ws)
    await mount_into_mass(mass, _Mcp(_method_echo_asgi), mount_path="/mcp/v1")
    await mount_well_known(
        mass,
        mount_path="/mcp/v1",
        resource_uri="http://localhost:8095/mcp/v1",
        authorization_servers=["http://localhost:8095"],
        scopes_supported=["query:library"],
        resource_name="Music Assistant MCP",
    )
    async with TestClient(TestServer(build_aiohttp_app(ws))) as client:
        # MCP endpoint reachable
        resp = await client.post("/mcp/v1/", headers={"Origin": "http://localhost:8095"})
        assert resp.status == 200
        # well-known sub-path returns RFC 9728 metadata
        meta = await client.get("/.well-known/oauth-protected-resource/mcp/v1")
        assert meta.status == 200
        doc = await meta.json()
        assert doc["resource"] == "http://localhost:8095/mcp/v1"
        assert doc["authorization_servers"] == ["http://localhost:8095"]
        # Well-known root form also works (RFC 9728 §3.1 fallback).
        meta_root = await client.get("/.well-known/oauth-protected-resource")
        assert meta_root.status == 200
