"""Tests for Origin allowlist computation, matching, and bridge enforcement (C1+C2)."""
# mypy: disable-error-code="arg-type, no-untyped-def, type-arg, assignment, operator, misc"

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from music_assistant.providers.fastmcp_server.http_bridge import (
    build_protected_resource_metadata,
    mount_into_mass,
    mount_well_known,
)
from music_assistant.providers.fastmcp_server.origins import (
    _is_origin_allowed,
    _normalize_origin,
)
from music_assistant.providers.fastmcp_server.origins import (
    compute_origin_allowlist as _compute_origin_allowlist,
)
from music_assistant.providers.fastmcp_server.origins import (
    is_origin_allowed_for_request as _is_origin_allowed_for_request,
)

from .conftest import FakeWebserver, build_aiohttp_app


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://localhost:8095", "http://localhost:8095"),
        ("HTTP://Localhost:8095", "http://localhost:8095"),
        ("http://localhost:80", "http://localhost"),
        ("https://example.com:443", "https://example.com"),
        ("https://example.com/path", "https://example.com"),
        ("null", "null"),
        ("", None),
        ("not-a-url", None),
        ("http://", None),
        # IPv6 literals: brackets must round-trip so the normalized form
        # matches the allowlist entry the bridge synthesises.
        ("http://[::1]", "http://[::1]"),
        ("http://[::1]:8095", "http://[::1]:8095"),
        ("HTTP://[::1]:8095", "http://[::1]:8095"),
        ("http://[2001:db8::1]:80", "http://[2001:db8::1]"),
    ],
)
def test_normalize_origin(raw: str, expected: str | None) -> None:
    """Origin strings collapse to ``scheme://host[:port]`` lowercased, default ports stripped."""
    assert _normalize_origin(raw) == expected


def test_compute_allowlist_ipv6_publish_ip() -> None:
    """An IPv6 publish_ip is bracketed in the allowlist so browsers' Origin matches."""
    mass = SimpleNamespace(
        webserver=SimpleNamespace(base_url="http://localhost:8095", publish_ip="::1"),
    )
    allow = _compute_origin_allowlist(mass)
    assert "http://[::1]:8095" in allow
    assert _is_origin_allowed("http://[::1]:8095", allow) is True
    # And without an explicit port (still works because we bracket consistently).
    assert "http://[::1]" in allow


def _fake_mass(base_url: str = "http://localhost:8095", publish_ip: str = "127.0.0.1"):
    return SimpleNamespace(webserver=SimpleNamespace(base_url=base_url, publish_ip=publish_ip))


def test_compute_allowlist_default() -> None:
    """Default allowlist contains loopbacks, base_url host, and publish_ip."""
    allow = _compute_origin_allowlist(_fake_mass())
    # loopbacks always there
    assert "http://localhost" in allow
    assert "http://127.0.0.1" in allow
    assert "http://[::1]" in allow
    # base_url with port
    assert "http://localhost:8095" in allow
    # https-twin of base_url
    assert "https://localhost:8095" in allow
    # publish_ip with derived port
    assert "http://127.0.0.1:8095" in allow
    assert "https://127.0.0.1:8095" in allow


def test_compute_allowlist_with_extras() -> None:
    """CSV ``extra_origins`` get normalized; bogus / empty entries silently dropped."""
    allow = _compute_origin_allowlist(
        _fake_mass(),
        extra_origins_csv="https://ha.example.com, http://reverse.lan:8443 ,bogus,",
    )
    assert "https://ha.example.com" in allow
    assert "http://reverse.lan:8443" in allow
    # bogus + empty silently dropped
    assert all(o != "" for o in allow)


def test_compute_allowlist_with_https_base_url() -> None:
    """When base_url uses https on the default port, no port suffix is added."""
    allow = _compute_origin_allowlist(_fake_mass(base_url="https://mcp.example.com"))
    assert "https://mcp.example.com" in allow
    # publish_ip with no explicit port (https is scheme-default)
    assert "http://127.0.0.1" in allow
    assert "https://127.0.0.1" in allow


@pytest.mark.parametrize(
    ("origin", "allowed"),
    [
        (None, True),  # CLI / stdio-style — no Origin
        ("http://localhost:8095", True),
        ("http://LOCALHOST:8095", True),  # case-insensitive
        ("http://localhost:8095/", True),  # trailing slash tolerated
        ("http://evil.example", False),
        ("https://localhost:8095", True),  # https-twin allowed by default
        ("null", False),  # not in default allowlist
    ],
)
def test_is_origin_allowed(origin: str | None, allowed: bool) -> None:
    """Match Origin against the default allowlist (case-insensitive, trailing-slash tolerant)."""
    allow = _compute_origin_allowlist(_fake_mass())
    assert _is_origin_allowed(origin, allow) is allowed


def test_is_origin_allowed_with_explicit_null() -> None:
    """``Origin: null`` is accepted only when the operator opts in via ``extra_origins``."""
    allow = _compute_origin_allowlist(_fake_mass(), extra_origins_csv="null")
    assert "null" in allow
    assert _is_origin_allowed("null", allow) is True


def test_garbage_origin_rejected() -> None:
    """Malformed Origin values fail closed with 403."""
    allow = _compute_origin_allowlist(_fake_mass())
    assert _is_origin_allowed("not-a-url", allow) is False
    assert _is_origin_allowed("http://", allow) is False


# ── HA-ingress fallback in `_is_origin_allowed_for_request` ─────────────────


def _fake_request(
    headers: dict[str, str] | None = None,
    *,
    scheme: str = "http",
) -> Any:
    """Build a minimal stand-in for ``aiohttp.web.Request`` for origin checks."""
    return SimpleNamespace(
        headers=headers or {},
        remote="172.30.32.1",
        scheme=scheme,
    )


def _install_ingress_stub(monkeypatch: pytest.MonkeyPatch, *, is_ingress: bool) -> None:
    """Inject ``is_request_from_ingress`` lazily into ``sys.modules``."""
    import sys  # noqa: PLC0415
    import types  # noqa: PLC0415

    pkg = types.ModuleType("music_assistant")
    pkg.__path__ = []
    controllers = types.ModuleType("music_assistant.controllers")
    controllers.__path__ = []
    webserver_pkg = types.ModuleType("music_assistant.controllers.webserver")
    webserver_pkg.__path__ = []
    helpers_pkg = types.ModuleType("music_assistant.controllers.webserver.helpers")
    helpers_pkg.__path__ = []
    auth_mod = types.ModuleType("music_assistant.controllers.webserver.helpers.auth_middleware")
    auth_mod.is_request_from_ingress = lambda _req: is_ingress  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "music_assistant", pkg)
    monkeypatch.setitem(sys.modules, "music_assistant.controllers", controllers)
    monkeypatch.setitem(sys.modules, "music_assistant.controllers.webserver", webserver_pkg)
    monkeypatch.setitem(sys.modules, "music_assistant.controllers.webserver.helpers", helpers_pkg)
    monkeypatch.setitem(
        sys.modules,
        "music_assistant.controllers.webserver.helpers.auth_middleware",
        auth_mod,
    )


def test_request_origin_allowed_via_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    """Falls through to the legacy allowlist when the basic check accepts."""
    _install_ingress_stub(monkeypatch, is_ingress=False)
    allow = _compute_origin_allowlist(_fake_mass())
    req = _fake_request({"Origin": "http://localhost:8095"})
    assert _is_origin_allowed_for_request(req, allow) is True


def test_request_origin_accepts_ingress_forwarded_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Ingress request whose Origin matches X-Forwarded-Host is accepted.

    Reproduces the HA add-on case where the user opens the Connect Wizard at
    ``https://<ha>/<slug>/…`` and the browser sends ``Origin: https://<ha>``,
    which is never on the static allowlist.
    """
    _install_ingress_stub(monkeypatch, is_ingress=True)
    allow = _compute_origin_allowlist(_fake_mass())
    req = _fake_request(
        {
            "Origin": "https://ha.example.com",
            "X-Forwarded-Host": "ha.example.com",
            "X-Forwarded-Proto": "https",
        }
    )
    assert _is_origin_allowed_for_request(req, allow) is True


def test_request_origin_rejects_forwarded_host_without_ingress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the trusted-ingress signal, ``X-Forwarded-Host`` is not trusted."""
    _install_ingress_stub(monkeypatch, is_ingress=False)
    allow = _compute_origin_allowlist(_fake_mass())
    req = _fake_request(
        {
            "Origin": "https://attacker.example",
            "X-Forwarded-Host": "attacker.example",
            "X-Forwarded-Proto": "https",
        }
    )
    assert _is_origin_allowed_for_request(req, allow) is False


def test_request_origin_rejects_mismatched_forwarded_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Origin must equal the forwarded host — a mismatch is rejected even via ingress."""
    _install_ingress_stub(monkeypatch, is_ingress=True)
    allow = _compute_origin_allowlist(_fake_mass())
    req = _fake_request(
        {
            "Origin": "https://attacker.example",
            "X-Forwarded-Host": "ha.example.com",
            "X-Forwarded-Proto": "https",
        }
    )
    assert _is_origin_allowed_for_request(req, allow) is False


def test_request_origin_no_forward_header_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ingress without ``X-Forwarded-Host`` falls through to the strict allowlist."""
    _install_ingress_stub(monkeypatch, is_ingress=True)
    allow = _compute_origin_allowlist(_fake_mass())
    req = _fake_request({"Origin": "https://ha.example.com"})
    assert _is_origin_allowed_for_request(req, allow) is False


def test_request_origin_missing_proto_falls_back_to_transport_scheme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    When ``X-Forwarded-Proto`` is absent, the aiohttp ``scheme`` fills in.

    Inside an HA add-on the transport scheme is plain ``http`` because the
    container is reached over the docker network. A proxy that forwards the
    host header but omits the proto header should still validate against the
    actual transport's scheme rather than guess ``https``.
    """
    _install_ingress_stub(monkeypatch, is_ingress=True)
    allow = _compute_origin_allowlist(_fake_mass())
    req = _fake_request(
        {
            "Origin": "http://ha.local:8123",
            "X-Forwarded-Host": "ha.local:8123",
        },
        scheme="http",
    )
    assert _is_origin_allowed_for_request(req, allow) is True


def test_request_origin_accepts_case_insensitive_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Origin`` matching is case-insensitive on host (RFC 3986)."""
    _install_ingress_stub(monkeypatch, is_ingress=True)
    allow = _compute_origin_allowlist(_fake_mass())
    req = _fake_request(
        {
            "Origin": "HTTPS://Ha.Example.COM",
            "X-Forwarded-Host": "ha.example.com",
            "X-Forwarded-Proto": "https",
        }
    )
    assert _is_origin_allowed_for_request(req, allow) is True


def test_request_origin_handles_missing_ma_module() -> None:
    """When ``music_assistant`` is unavailable, never auto-accept (fail closed)."""
    allow = _compute_origin_allowlist(_fake_mass())
    req = _fake_request(
        {
            "Origin": "https://ha.example.com",
            "X-Forwarded-Host": "ha.example.com",
        }
    )
    # No stub installed; the import inside the helper raises, caught → reject.
    assert _is_origin_allowed_for_request(req, allow) is False


# ── End-to-end bridge enforcement (C2) ──────────────────────────────────────


class _FakeMcp:
    """Stand-in for FastMCP exposing an ASGI app via ``http_app(...)``."""

    def __init__(self, asgi_app: Any) -> None:
        self._app = asgi_app

    def http_app(self, transport: str = "streamable-http", path: str = "/mcp") -> Any:
        return self._app


async def _echo_asgi(scope: dict, receive: Any, send: Any) -> None:
    """Minimal ASGI app: handles lifespan events + returns 200 'OK' on http."""
    if scope.get("type") == "lifespan":
        while True:
            msg = await receive()
            if msg["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif msg["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"OK"})


@pytest.fixture
async def bridge_client() -> Any:
    """Build the bridge handler against a fake MA + fake ASGI, expose via TestClient."""
    fake_ws = FakeWebserver()
    mass = SimpleNamespace(webserver=fake_ws)
    mcp = _FakeMcp(_echo_asgi)
    await mount_into_mass(mass, mcp, mount_path="/mcp/v1", extra_origins_csv="")

    async with TestClient(TestServer(build_aiohttp_app(fake_ws))) as client:
        yield client


async def test_bridge_rejects_evil_origin(bridge_client: TestClient) -> None:
    """A non-allow-listed Origin is blocked with 403, ASGI never invoked."""
    resp = await bridge_client.post("/mcp/v1/", headers={"Origin": "http://evil.example"})
    assert resp.status == 403


async def test_bridge_allows_localhost(bridge_client: TestClient) -> None:
    """Origin that matches base_url is forwarded to ASGI (200 from echo app)."""
    resp = await bridge_client.post("/mcp/v1/", headers={"Origin": "http://localhost:8095"})
    assert resp.status == 200
    assert (await resp.read()) == b"OK"


async def test_bridge_allows_no_origin(bridge_client: TestClient) -> None:
    """Requests without ``Origin`` header (curl/CLI) pass through unchanged."""
    resp = await bridge_client.post("/mcp/v1/")
    assert resp.status == 200


def test_build_protected_resource_metadata_minimal() -> None:
    """RFC 9728 metadata always carries resource + authorization_servers + bearer methods."""
    meta = build_protected_resource_metadata(
        resource_uri="http://localhost:8095/mcp/v1",
        authorization_servers=["http://localhost:8095"],
    )
    assert meta == {
        "resource": "http://localhost:8095/mcp/v1",
        "authorization_servers": ["http://localhost:8095"],
        "bearer_methods_supported": ["header"],
    }


def test_build_protected_resource_metadata_full() -> None:
    """Optional fields scopes_supported / resource_name appear when provided."""
    meta = build_protected_resource_metadata(
        resource_uri="http://localhost:8095/mcp/v1",
        authorization_servers=["http://localhost:8095"],
        scopes_supported=["query:library", "control:playback"],
        resource_name="Music Assistant MCP",
    )
    assert meta["scopes_supported"] == ["query:library", "control:playback"]
    assert meta["resource_name"] == "Music Assistant MCP"


async def test_mount_well_known_with_dynamic_scopes_refreshes() -> None:
    """
    When scopes_supported is a callable, the body refreshes on each request.

    Permission hot-swap mutates the closed-over set in MCPServerRuntime;
    the well-known endpoint must reflect the change without a runtime rebuild.
    """
    fake_ws = FakeWebserver()
    mass = SimpleNamespace(webserver=fake_ws)
    scopes: list[str] = ["query:library"]
    await mount_well_known(
        mass,
        mount_path="/mcp/v1",
        resource_uri="http://localhost:8095/mcp/v1",
        authorization_servers=["http://localhost:8095"],
        scopes_supported=lambda: list(scopes),
        resource_name="MA MCP",
    )

    async with TestClient(TestServer(build_aiohttp_app(fake_ws))) as client:
        before = await (await client.get("/.well-known/oauth-protected-resource")).json()
        assert before["scopes_supported"] == ["query:library"]
        # Mutate the underlying set (simulates hot-swap of permission flags).
        scopes.append("control:playback")
        after = await (await client.get("/.well-known/oauth-protected-resource")).json()
        assert "control:playback" in after["scopes_supported"]


async def test_mount_well_known_serves_metadata() -> None:
    """The well-known route returns the RFC 9728 JSON document for both URI forms."""
    fake_ws = FakeWebserver()
    mass = SimpleNamespace(webserver=fake_ws)
    unmount = await mount_well_known(
        mass,
        mount_path="/mcp/v1",
        resource_uri="http://localhost:8095/mcp/v1",
        authorization_servers=["http://localhost:8095"],
        scopes_supported=["query:library"],
        resource_name="Music Assistant MCP",
    )

    paths = [r[0] for r in fake_ws.routes]
    assert "/.well-known/oauth-protected-resource/mcp/v1" in paths
    assert "/.well-known/oauth-protected-resource" in paths

    async with TestClient(TestServer(build_aiohttp_app(fake_ws))) as client:
        for path in paths:
            resp = await client.get(path)
            assert resp.status == 200
            assert resp.headers["content-type"].startswith("application/json")
            doc = await resp.json()
            assert doc["resource"] == "http://localhost:8095/mcp/v1"
            assert doc["authorization_servers"] == ["http://localhost:8095"]
            assert doc["bearer_methods_supported"] == ["header"]
            assert doc["scopes_supported"] == ["query:library"]
            assert doc["resource_name"] == "Music Assistant MCP"

    unmount()


async def test_bridge_with_extra_origins() -> None:
    """``extra_origins_csv`` widens the allowlist for reverse-proxy / HA ingress."""
    fake_ws = FakeWebserver()
    mass = SimpleNamespace(webserver=fake_ws)
    mcp = _FakeMcp(_echo_asgi)
    await mount_into_mass(
        mass, mcp, mount_path="/mcp/v1", extra_origins_csv="https://ha.example.com"
    )

    async with TestClient(TestServer(build_aiohttp_app(fake_ws))) as client:
        resp = await client.post("/mcp/v1/", headers={"Origin": "https://ha.example.com"})
        assert resp.status == 200
        # An origin not in the extras stays rejected.
        resp = await client.post("/mcp/v1/", headers={"Origin": "http://evil.example"})
        assert resp.status == 403
