"""
Tests for the Connect Wizard — endpoints, action handler, client templates.

These tests run against the real :func:`mount_connect_wizard` flow on a
``FakeWebserver`` (no real MA stack required); ``mass.webserver.auth`` is
stubbed with ``AsyncMock`` / ``MagicMock`` so we can assert exactly which auth
calls the wizard fires for each user-facing operation.
"""
# ruff: noqa: D401
#   D401: pytest fixture/test docstrings describe *what is returned*.
#   S101: ``assert`` is the pytest convention.
#   PLR2004: small magic numbers (12 client specs, 5 routes) are obvious in context.
# mypy: disable-error-code="type-arg"

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml
from aiohttp.test_utils import TestClient, TestServer

from music_assistant.providers.fastmcp_server._init_helpers import (
    _detect_external_base_url,
    _dispatch_open_connect,
    _sanitize_external_base_url,
)
from music_assistant.providers.fastmcp_server.connect.actions import handle_open_connect_action
from music_assistant.providers.fastmcp_server.connect.clients import CLIENTS, lookup_client
from music_assistant.providers.fastmcp_server.connect.mount import mount_connect_wizard
from music_assistant.providers.fastmcp_server.connect.page import HTML
from music_assistant.providers.fastmcp_server.constants import CONF_CONNECT_EXTERNAL_URL

from .conftest import FakeWebserver, build_aiohttp_app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@pytest.fixture
def wizard_mass(mock_user: MagicMock) -> MagicMock:
    """A ``mass`` stub with ``FakeWebserver`` + the auth surface the wizard touches."""
    fake_ws = FakeWebserver()
    fake_ws.auth = SimpleNamespace(  # type: ignore[attr-defined]
        login=AsyncMock(
            return_value={
                "success": True,
                "access_token": "sess-1",
                "user": {
                    "user_id": mock_user.user_id,
                    "username": mock_user.username,
                    "role": "admin",
                },
            }
        ),
        create_token=AsyncMock(return_value="jwt-xyz"),
        authenticate_with_token=AsyncMock(return_value=mock_user),
        get_current_user=MagicMock(return_value=mock_user),
        # Sanctioned auth-API surface that provider/connect/_revoke.py drives.
        revoke_token=AsyncMock(),
        get_user_tokens=AsyncMock(return_value=[]),
        get_token_id_from_token=AsyncMock(side_effect=lambda t: f"tid:{t}"),
    )
    mass = MagicMock()
    mass.webserver = fake_ws
    mass.signal_event = MagicMock()
    return mass


@pytest.fixture
async def wizard_client(wizard_mass: MagicMock) -> AsyncIterator[TestClient]:
    """Mount the wizard on /mcp/v1 and yield an aiohttp TestClient."""
    unmount = await mount_connect_wizard(
        wizard_mass,
        mount_path="/mcp/v1",
        default_profile_provider=lambda: "Trusted",
        extra_origins_csv="",
    )
    async with TestClient(TestServer(build_aiohttp_app(wizard_mass.webserver))) as client:
        yield client
    unmount()


# ── HTML page + info endpoint ────────────────────────────────────────────────


async def test_connect_html_served(wizard_client: TestClient) -> None:
    """``GET /mcp/v1/connect`` returns an HTML page mentioning Music Assistant."""
    resp = await wizard_client.get("/mcp/v1/connect", headers={"Origin": "http://localhost:8095"})
    assert resp.status == 200
    assert resp.headers["Content-Type"].startswith("text/html")
    body = await resp.text()
    assert "Music Assistant" in body
    assert "connect" in body.lower()


async def test_connect_page_sets_security_headers(wizard_client: TestClient) -> None:
    """
    The wizard response carries Referrer-Policy, CSP, and X-Frame-Options.

    Two leak vectors motivate these:

    * The bootstrap token in the URL would be leaked via ``Referer`` on
      the GitHub footer link (or any future outbound link) without
      ``Referrer-Policy: no-referrer``.
    * Per-client long-lived MA tokens are cached in ``sessionStorage``; a
      future inline-data XSS in this page would steal them all without a
      tight Content-Security-Policy.
    """
    resp = await wizard_client.get("/mcp/v1/connect", headers={"Origin": "http://localhost:8095"})
    assert resp.headers.get("Referrer-Policy") == "no-referrer"

    csp = resp.headers.get("Content-Security-Policy") or ""
    # Each directive is necessary — script-src controls inline JS scope,
    # connect-src 'self' bars exfiltration to attacker origins, etc.
    for directive in (
        "default-src 'none'",
        "script-src 'unsafe-inline'",
        "style-src 'unsafe-inline'",
        "connect-src 'self'",
        "frame-ancestors 'none'",
    ):
        assert directive in csp, f"missing CSP directive: {directive!r}; got {csp!r}"

    assert resp.headers.get("X-Frame-Options") == "DENY"
    assert resp.headers.get("Cache-Control") == "no-store"


async def test_scheme_guard_rejects_plaintext_non_loopback_login(
    wizard_client: TestClient, wizard_mass: MagicMock
) -> None:
    """
    ``/connect/login`` over plaintext http to a non-loopback host is refused.

    The wizard's only credential-bearing endpoints (login/exchange/token) must
    not accept plaintext HTTP from a LAN-reachable host — the password and
    bootstrap tokens would be sniffable. HTTPS is allowed, and so is
    loopback (the bytes never leave the box). Anything else gets a 400.
    """
    # TestClient binds to 127.0.0.1, so the request scheme is http and we'd
    # naturally pass the loopback exception. Force ``request.host`` to a LAN
    # address via the Host header to exercise the rejection path.
    resp = await wizard_client.post(
        "/mcp/v1/connect/login",
        json={"username": "admin", "password": "hunter2"},
        headers={"Origin": "http://localhost:8095", "Host": "192.168.1.42:8095"},
    )
    assert resp.status == 400
    body = await resp.json()
    assert body["success"] is False
    assert "plaintext" in body["error"].lower() or "https" in body["error"].lower()
    # MA's login must NOT have been called — credentials never crossed the wire.
    wizard_mass.webserver.auth.login.assert_not_awaited()


async def test_scheme_guard_allows_loopback_plaintext_login(
    wizard_client: TestClient, wizard_mass: MagicMock
) -> None:
    """Loopback plaintext is allowed — the bytes never leave the box."""
    resp = await wizard_client.post(
        "/mcp/v1/connect/login",
        json={"username": "admin", "password": "hunter2"},
        headers={"Origin": "http://localhost:8095", "Host": "127.0.0.1:8095"},
    )
    assert resp.status == 200
    wizard_mass.webserver.auth.login.assert_awaited_once()


async def test_scheme_guard_rejects_plaintext_non_loopback_exchange(
    wizard_client: TestClient, wizard_mass: MagicMock
) -> None:
    """Bootstrap exchange over plaintext non-loopback is refused before any MA call."""
    resp = await wizard_client.post(
        "/mcp/v1/connect/exchange",
        json={"bootstrap": "boot-1"},
        headers={"Origin": "http://localhost:8095", "Host": "192.168.1.42:8095"},
    )
    assert resp.status == 400
    wizard_mass.webserver.auth.authenticate_with_token.assert_not_awaited()


def _install_fake_ingress_helper(monkeypatch: pytest.MonkeyPatch, *, is_ingress: bool) -> None:
    """
    Install a stub ``is_request_from_ingress`` MA helper that returns ``is_ingress``.

    HA terminates TLS at its public front door (``https://ha.example/…``)
    and forwards the request to MA over a local socket — so MA sees plain
    ``http://`` from a non-loopback host, but the public hop *is* HTTPS.
    The wizard's scheme guard mirrors the ``Origin``-check pattern: when
    ``music_assistant.controllers.webserver.helpers.auth_middleware
    .is_request_from_ingress`` returns True, the request is on MA's
    trusted ingress socket and the plaintext-LAN concern doesn't apply.
    """
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


async def test_scheme_guard_allows_plaintext_via_ha_ingress(
    wizard_client: TestClient,
    wizard_mass: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    HA ingress: plaintext non-loopback is OK when the request is on the trusted socket.

    Reproduces the production breakage on
    ``https://ha.nevskiy.su/api/hassio_ingress/<id>/mcp/v1/connect``:
    HA forwards the request to MA over a local socket, so the wizard
    sees ``request.scheme == "http"`` and a non-loopback ``request.host``
    even though the public hop is HTTPS. Without honouring the ingress
    helper, the wizard's scheme guard rejected the bootstrap exchange
    and the user was shown the login fields with
    ``Plaintext credential traffic from non-loopback hosts is not
    allowed`` on submit. The guard must recognise the trusted-ingress
    transport and let the request through.
    """
    _install_fake_ingress_helper(monkeypatch, is_ingress=True)
    resp = await wizard_client.post(
        "/mcp/v1/connect/exchange",
        json={"bootstrap": "boot-1"},
        headers={"Origin": "http://localhost:8095", "Host": "ha.example:8123"},
    )
    assert resp.status == 200
    wizard_mass.webserver.auth.authenticate_with_token.assert_awaited_with("boot-1")


async def test_scheme_guard_still_rejects_when_ingress_helper_returns_false(
    wizard_client: TestClient,
    wizard_mass: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The ingress bypass requires MA's helper to actually confirm the trusted socket.

    A direct LAN request (not via HA ingress) with the same shape — plaintext
    http, non-loopback host — must still be refused. This pins that the
    bypass is gated on ``is_request_from_ingress``, not on any other property
    of the request a hostile client could forge.
    """
    _install_fake_ingress_helper(monkeypatch, is_ingress=False)
    resp = await wizard_client.post(
        "/mcp/v1/connect/login",
        json={"username": "admin", "password": "hunter2"},
        headers={"Origin": "http://localhost:8095", "Host": "192.168.1.42:8095"},
    )
    assert resp.status == 400
    wizard_mass.webserver.auth.login.assert_not_awaited()


@pytest.fixture
async def wizard_client_trust_proxy(wizard_mass: MagicMock) -> AsyncIterator[TestClient]:
    """Wizard mounted with ``trust_forwarded_proto=True`` (TLS-terminating proxy)."""
    unmount = await mount_connect_wizard(
        wizard_mass,
        mount_path="/mcp/v1",
        default_profile_provider=lambda: "Trusted",
        extra_origins_csv="",
        trust_forwarded_proto=True,
    )
    async with TestClient(TestServer(build_aiohttp_app(wizard_mass.webserver))) as client:
        yield client
    unmount()


async def test_scheme_guard_trust_proxy_off_ignores_forwarded_proto(
    wizard_client: TestClient, wizard_mass: MagicMock
) -> None:
    """
    Default (trust off): ``X-Forwarded-Proto: https`` does NOT bypass the guard.

    The header is forgeable by any LAN client, so it must be inert unless the
    operator has explicitly opted in — secure by default, no behaviour change.
    """
    resp = await wizard_client.post(
        "/mcp/v1/connect/login",
        json={"username": "admin", "password": "hunter2"},
        headers={
            "Origin": "http://localhost:8095",
            "Host": "192.168.1.42:8095",
            "X-Forwarded-Proto": "https",
        },
    )
    assert resp.status == 400
    wizard_mass.webserver.auth.login.assert_not_awaited()


async def test_scheme_guard_trust_proxy_allows_forwarded_https(
    wizard_client_trust_proxy: TestClient, wizard_mass: MagicMock
) -> None:
    """
    Trust on + ``X-Forwarded-Proto: https`` → request is treated as secure.

    Reproduces the reverse-proxy deployment (nginx / NPM / Traefik / Caddy):
    TLS terminates at the proxy, the proxy-to-MA hop is plain HTTP, and the
    proxy reports the original scheme via ``X-Forwarded-Proto``.
    """
    resp = await wizard_client_trust_proxy.post(
        "/mcp/v1/connect/login",
        json={"username": "admin", "password": "hunter2"},
        headers={
            "Origin": "http://localhost:8095",
            "Host": "musicassistant.example.com",
            "X-Forwarded-Proto": "https",
        },
    )
    assert resp.status == 200
    wizard_mass.webserver.auth.login.assert_awaited_once()


async def test_scheme_guard_trust_proxy_accepts_forwarded_scheme_header(
    wizard_client_trust_proxy: TestClient, wizard_mass: MagicMock
) -> None:
    """Trust on: Nginx-Proxy-Manager's ``X-Forwarded-Scheme: https`` also counts."""
    resp = await wizard_client_trust_proxy.post(
        "/mcp/v1/connect/login",
        json={"username": "admin", "password": "hunter2"},
        headers={
            "Origin": "http://localhost:8095",
            "Host": "musicassistant.example.com",
            "X-Forwarded-Scheme": "https",
        },
    )
    assert resp.status == 200
    wizard_mass.webserver.auth.login.assert_awaited_once()


async def test_scheme_guard_trust_proxy_multi_hop_uses_first_value(
    wizard_client_trust_proxy: TestClient, wizard_mass: MagicMock
) -> None:
    """Trust on: a chained ``https, http`` list is read as the client hop (https)."""
    resp = await wizard_client_trust_proxy.post(
        "/mcp/v1/connect/login",
        json={"username": "admin", "password": "hunter2"},
        headers={
            "Origin": "http://localhost:8095",
            "Host": "musicassistant.example.com",
            "X-Forwarded-Proto": "https, http",
        },
    )
    assert resp.status == 200
    wizard_mass.webserver.auth.login.assert_awaited_once()


async def test_scheme_guard_trust_proxy_still_rejects_plain_http(
    wizard_client_trust_proxy: TestClient, wizard_mass: MagicMock
) -> None:
    """Trust on but no https forwarded header → still refused (genuine plaintext)."""
    resp = await wizard_client_trust_proxy.post(
        "/mcp/v1/connect/login",
        json={"username": "admin", "password": "hunter2"},
        headers={
            "Origin": "http://localhost:8095",
            "Host": "192.168.1.42:8095",
            "X-Forwarded-Proto": "http",
        },
    )
    assert resp.status == 400
    wizard_mass.webserver.auth.login.assert_not_awaited()


async def test_info_endpoint_shape(wizard_client: TestClient) -> None:
    """``GET /mcp/v1/connect/info`` returns the meta JSON the UI needs."""
    resp = await wizard_client.get(
        "/mcp/v1/connect/info", headers={"Origin": "http://localhost:8095"}
    )
    assert resp.status == 200
    data = await resp.json()
    for key in (
        "mount_path",
        "mcp_url_loopback",
        "mcp_url_advertised",
        "default_policy",
        "clients",
        "well_known_url",
    ):
        assert key in data, f"missing key: {key}"
    assert data["mount_path"] == "/mcp/v1"
    assert data["mcp_url_loopback"].endswith("/mcp/v1")
    assert isinstance(data["clients"], list)
    assert len(data["clients"]) == 15
    clients = {client["id"]: client for client in data["clients"]}
    assert {
        client_id: [method["id"] for method in client["methods"]]
        for client_id, client in clients.items()
    } == {
        "claude-code": ["cli", "project-config"],
        "cursor": ["user-config", "project-config"],
        "opencode": ["user-config", "project-config"],
        "windsurf": ["devin-user", "devin-project", "legacy-cascade"],
        "vscode": ["user-config", "workspace-config"],
        "github-copilot-cli": ["cli", "interactive", "user-config", "project-config"],
        "codex-cli": ["cli", "user-config"],
        "gemini-cli": ["cli", "user-config", "project-config"],
        "cline": ["user-config", "cli-wizard"],
        "roo-code": ["global-config", "project-config"],
        "zed": ["settings-ui", "user-config", "project-config"],
        "openclaw": ["cli", "user-config"],
        "openhands": ["cli", "user-config"],
        "hermes": ["cli", "user-config", "desktop-editor"],
        "custom": ["parameters"],
    }
    assert "claude-desktop" not in clients
    assert "chatgpt" not in clients
    assert data["default_policy"] == {"profile": "Trusted"}
    assert "permissions" not in data


async def test_info_exposes_only_default_profile(wizard_mass: MagicMock) -> None:
    """Connect metadata never exposes capability matrices or token overrides."""
    unmount = await mount_connect_wizard(
        wizard_mass,
        mount_path="/mcp/v1",
        default_profile_provider=lambda: "Trusted",
        extra_origins_csv="",
    )
    try:
        async with TestClient(TestServer(build_aiohttp_app(wizard_mass.webserver))) as client:
            resp = await client.get("/mcp/v1/connect/info")
            data = await resp.json()
            assert data["default_policy"] == {"profile": "Trusted"}
            assert "permissions" not in data
            assert "allow" not in data["default_policy"]
            assert "confirm" not in data["default_policy"]
            assert "deny" not in data["default_policy"]
    finally:
        unmount()


# ── Bootstrap exchange ───────────────────────────────────────────────────────


async def test_exchange_bootstrap_success(
    wizard_client: TestClient, wizard_mass: MagicMock, mock_user: MagicMock
) -> None:
    """A valid bootstrap token is exchanged for a session_token bound to the same user."""
    resp = await wizard_client.post(
        "/mcp/v1/connect/exchange",
        json={"bootstrap": "boot-1"},
        headers={"Origin": "http://localhost:8095"},
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["session_token"] == "jwt-xyz"
    assert data["user"]["user_id"] == mock_user.user_id

    wizard_mass.webserver.auth.authenticate_with_token.assert_awaited_with("boot-1")
    wizard_mass.webserver.auth.create_token.assert_awaited_with(
        user=mock_user,
        name="MCP — wizard session",
        is_long_lived=False,
    )


async def test_exchange_bootstrap_invalid_401(
    wizard_client: TestClient, wizard_mass: MagicMock
) -> None:
    """Invalid bootstrap → 401 and ``create_token`` is NOT called."""
    wizard_mass.webserver.auth.authenticate_with_token = AsyncMock(return_value=None)
    wizard_mass.webserver.auth.create_token.reset_mock()

    resp = await wizard_client.post(
        "/mcp/v1/connect/exchange",
        json={"bootstrap": "bad"},
        headers={"Origin": "http://localhost:8095"},
    )
    assert resp.status == 401
    wizard_mass.webserver.auth.create_token.assert_not_called()


async def test_exchange_revokes_bootstrap_on_success(
    wizard_client: TestClient, wizard_mass: MagicMock
) -> None:
    """Successful exchange revokes the bootstrap via ``auth.revoke_token``."""
    auth = wizard_mass.webserver.auth

    resp = await wizard_client.post(
        "/mcp/v1/connect/exchange",
        json={"bootstrap": "boot-1"},
        headers={"Origin": "http://localhost:8095"},
    )
    assert resp.status == 200

    auth.revoke_token.assert_awaited_once_with("tid:boot-1")


async def test_exchange_invalid_bootstrap_does_not_revoke(
    wizard_client: TestClient, wizard_mass: MagicMock
) -> None:
    """Invalid bootstrap → no revoke and no mint."""
    wizard_mass.webserver.auth.authenticate_with_token = AsyncMock(return_value=None)

    resp = await wizard_client.post(
        "/mcp/v1/connect/exchange",
        json={"bootstrap": "bad"},
        headers={"Origin": "http://localhost:8095"},
    )
    assert resp.status == 401
    wizard_mass.webserver.auth.revoke_token.assert_not_called()


async def test_exchange_revoke_failure_still_returns_session(
    wizard_client: TestClient, wizard_mass: MagicMock
) -> None:
    """A ``revoke_token`` exception is swallowed; the exchange still issues a session_token."""
    auth = wizard_mass.webserver.auth
    auth.revoke_token = AsyncMock(side_effect=RuntimeError("revoke failed"))

    resp = await wizard_client.post(
        "/mcp/v1/connect/exchange",
        json={"bootstrap": "boot-1"},
        headers={"Origin": "http://localhost:8095"},
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["session_token"] == "jwt-xyz"
    auth.create_token.assert_awaited_once()


async def test_exchange_get_token_id_none_skips_revoke(
    wizard_client: TestClient, wizard_mass: MagicMock
) -> None:
    """When ``get_token_id_from_token`` returns ``None`` the revoke is skipped, mint still happens."""
    auth = wizard_mass.webserver.auth
    auth.get_token_id_from_token = AsyncMock(return_value=None)

    resp = await wizard_client.post(
        "/mcp/v1/connect/exchange",
        json={"bootstrap": "boot-1"},
        headers={"Origin": "http://localhost:8095"},
    )
    assert resp.status == 200
    auth.revoke_token.assert_not_called()
    auth.create_token.assert_awaited_once()


# ── Login form fallback ──────────────────────────────────────────────────────


async def test_login_success_returns_token(
    wizard_client: TestClient, wizard_mass: MagicMock
) -> None:
    """Successful login returns the access_token issued by MA."""
    resp = await wizard_client.post(
        "/mcp/v1/connect/login",
        json={"username": "tester", "password": "secret"},
        headers={"Origin": "http://localhost:8095"},
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["session_token"] == "sess-1"
    assert data["user"]["username"] == "tester"

    wizard_mass.webserver.auth.login.assert_awaited_with(
        username="tester", password="secret", provider_id="builtin"
    )


async def test_login_failure_401(wizard_client: TestClient, wizard_mass: MagicMock) -> None:
    """Login failure → 401 with the error MA reported."""
    wizard_mass.webserver.auth.login = AsyncMock(
        return_value={"success": False, "error": "bad creds"}
    )

    resp = await wizard_client.post(
        "/mcp/v1/connect/login",
        json={"username": "x", "password": "y"},
        headers={"Origin": "http://localhost:8095"},
    )
    assert resp.status == 401
    body = await resp.json()
    assert body.get("error") == "bad creds"


async def test_login_accepts_dataclass_style_result(
    wizard_client: TestClient, wizard_mass: MagicMock
) -> None:
    """
    If MA migrates ``login`` to return a typed object, success still surfaces.

    The handler used to ``isinstance(result, dict)`` and fall through to "invalid
    credentials" for anything else — a silent break on the only credential
    path. The shape-agnostic accessor handles both forms now.
    """
    wizard_mass.webserver.auth.login = AsyncMock(
        return_value=SimpleNamespace(
            success=True,
            access_token="sess-via-dataclass",
            user={"user_id": "u1", "username": "tester", "role": "admin"},
        )
    )

    resp = await wizard_client.post(
        "/mcp/v1/connect/login",
        json={"username": "tester", "password": "secret"},
        headers={"Origin": "http://localhost:8095"},
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["session_token"] == "sess-via-dataclass"
    assert data["user"]["username"] == "tester"


async def test_login_dataclass_failure_returns_401(
    wizard_client: TestClient, wizard_mass: MagicMock
) -> None:
    """A typed failure result also surfaces correctly (not just dicts)."""
    wizard_mass.webserver.auth.login = AsyncMock(
        return_value=SimpleNamespace(success=False, error="dataclass error")
    )

    resp = await wizard_client.post(
        "/mcp/v1/connect/login",
        json={"username": "x", "password": "y"},
        headers={"Origin": "http://localhost:8095"},
    )
    assert resp.status == 401
    body = await resp.json()
    assert body.get("error") == "dataclass error"


# ── Per-client token mint ────────────────────────────────────────────────────


async def test_token_endpoint_mints_named(
    wizard_client: TestClient, wizard_mass: MagicMock, mock_user: MagicMock
) -> None:
    """Per-client mint creates a long-lived token labeled ``MCP — <Client>``."""
    resp = await wizard_client.post(
        "/mcp/v1/connect/token",
        json={"session_token": "sess-1", "client_id": "cursor"},
        headers={"Origin": "http://localhost:8095"},
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["token"] == "jwt-xyz"

    wizard_mass.webserver.auth.create_token.assert_awaited_with(
        user=mock_user,
        name="MCP — Cursor",
        is_long_lived=True,
    )


async def test_token_endpoint_unknown_client_400(wizard_client: TestClient) -> None:
    """Unknown ``client_id`` → 400."""
    resp = await wizard_client.post(
        "/mcp/v1/connect/token",
        json={"session_token": "sess-1", "client_id": "bogus"},
        headers={"Origin": "http://localhost:8095"},
    )
    assert resp.status == 400


async def test_token_endpoint_invalid_session_401(
    wizard_client: TestClient, wizard_mass: MagicMock
) -> None:
    """Invalid session_token → 401 and ``create_token`` is NOT called."""
    wizard_mass.webserver.auth.authenticate_with_token = AsyncMock(return_value=None)
    wizard_mass.webserver.auth.create_token.reset_mock()

    resp = await wizard_client.post(
        "/mcp/v1/connect/token",
        json={"session_token": "nope", "client_id": "cursor"},
        headers={"Origin": "http://localhost:8095"},
    )
    assert resp.status == 401
    wizard_mass.webserver.auth.create_token.assert_not_called()


async def test_token_endpoint_server_dedup_revokes_same_name(
    wizard_client: TestClient, wizard_mass: MagicMock
) -> None:
    """
    Prior tokens with the same client-token name for the user are revoked.

    Tokens with other names are left alone; ``create_token`` is still called
    once. Asserts the call against ``auth.revoke_token`` (sanctioned API),
    not the underlying DB.
    """
    auth = wizard_mass.webserver.auth
    auth.get_user_tokens = AsyncMock(
        return_value=[
            SimpleNamespace(token_id="old-1", name="MCP — Cursor", user_id="u1"),
            SimpleNamespace(token_id="old-2", name="MCP — Cursor", user_id="u1"),
            SimpleNamespace(token_id="keep", name="MCP — Other", user_id="u1"),
        ]
    )

    resp = await wizard_client.post(
        "/mcp/v1/connect/token",
        json={"session_token": "sess-1", "client_id": "cursor"},
        headers={"Origin": "http://localhost:8095"},
    )
    assert resp.status == 200

    revoked_ids = sorted(call.args[0] for call in auth.revoke_token.await_args_list)
    assert revoked_ids == ["old-1", "old-2"]
    auth.create_token.assert_awaited_once()


async def test_token_endpoint_dedup_lookup_failure_does_not_fail_mint(
    wizard_client: TestClient, wizard_mass: MagicMock
) -> None:
    """A ``get_user_tokens`` exception is logged but the mint still succeeds."""
    auth = wizard_mass.webserver.auth
    auth.get_user_tokens = AsyncMock(side_effect=RuntimeError("api down"))

    resp = await wizard_client.post(
        "/mcp/v1/connect/token",
        json={"session_token": "sess-1", "client_id": "cursor"},
        headers={"Origin": "http://localhost:8095"},
    )
    assert resp.status == 200
    auth.create_token.assert_awaited_once()


async def test_token_endpoint_no_prior_no_revoke(
    wizard_client: TestClient, wizard_mass: MagicMock
) -> None:
    """No prior tokens → ``auth.revoke_token`` is never called."""
    auth = wizard_mass.webserver.auth

    resp = await wizard_client.post(
        "/mcp/v1/connect/token",
        json={"session_token": "sess-1", "client_id": "cursor"},
        headers={"Origin": "http://localhost:8095"},
    )
    assert resp.status == 200
    auth.revoke_token.assert_not_called()


async def test_token_endpoint_revoke_failure_does_not_fail_mint(
    wizard_client: TestClient, wizard_mass: MagicMock
) -> None:
    """A ``revoke_token`` exception is swallowed; the new mint still happens."""
    auth = wizard_mass.webserver.auth
    auth.get_user_tokens = AsyncMock(
        return_value=[SimpleNamespace(token_id="old", name="MCP — Cursor", user_id="u1")]
    )
    auth.revoke_token = AsyncMock(side_effect=RuntimeError("revoke failed"))

    resp = await wizard_client.post(
        "/mcp/v1/connect/token",
        json={"session_token": "sess-1", "client_id": "cursor"},
        headers={"Origin": "http://localhost:8095"},
    )
    assert resp.status == 200
    auth.create_token.assert_awaited_once()


# ── Origin & mount ───────────────────────────────────────────────────────────


async def test_origin_rejection(wizard_client: TestClient) -> None:
    """A non-allowlisted Origin → 403."""
    resp = await wizard_client.get(
        "/mcp/v1/connect/info", headers={"Origin": "http://evil.example"}
    )
    assert resp.status == 403


async def test_mount_unmount_cycle(wizard_mass: MagicMock) -> None:
    """``mount_connect_wizard`` registers 5 routes; the returned callback removes all."""
    fake_ws = wizard_mass.webserver
    assert fake_ws.routes == []
    unmount = await mount_connect_wizard(
        wizard_mass,
        mount_path="/mcp/v1",
        default_profile_provider=lambda: "Safe queries",
        extra_origins_csv="",
    )
    assert len(fake_ws.routes) == 5
    unmount()
    assert fake_ws.routes == []


async def test_mount_path_relative(wizard_mass: MagicMock) -> None:
    """Wizard routes are nested under whatever ``mount_path`` is given."""
    unmount = await mount_connect_wizard(
        wizard_mass,
        mount_path="/custom",
        default_profile_provider=lambda: "Safe queries",
        extra_origins_csv="",
    )
    try:
        paths = [r[0] for r in wizard_mass.webserver.routes]
        assert all(p.startswith("/custom/connect") for p in paths)
    finally:
        unmount()


# ── ACTION handler (returned wizard URL) ─────────────────────────────────────


async def test_action_handler_signals_url_with_bootstrap(
    wizard_mass: MagicMock, mock_user: MagicMock
) -> None:
    """Action handler mints a bootstrap token and signals a URL containing it."""
    url = await handle_open_connect_action(
        wizard_mass,
        current_user=mock_user,
        mount_path="/mcp/v1",
    )

    wizard_mass.webserver.auth.create_token.assert_awaited_with(
        user=mock_user,
        name="MCP — wizard bootstrap",
        is_long_lived=False,
    )
    assert isinstance(url, str)
    # Path-only URL — the MA frontend resolves it against the user's location
    # so the wizard works in Docker / HA add-on deployments where MA's
    # advertised base_url points at an internal IP the browser cannot reach.
    assert url.startswith("/mcp/v1/connect")
    assert "bootstrap=jwt-xyz" in url


async def test_action_handler_uses_url_fragment_for_bootstrap(
    wizard_mass: MagicMock, mock_user: MagicMock
) -> None:
    """
    The bootstrap rides in the URL ``#fragment``, not the query string.

    Query-string form would leak the bootstrap into aiohttp access logs and
    every reverse-proxy log on the path; the GET request line is logged.
    Fragments are never sent to the server, so this is the only form that
    keeps short-lived bootstraps out of log files.
    """
    url = await handle_open_connect_action(
        wizard_mass,
        current_user=mock_user,
        mount_path="/mcp/v1",
    )

    assert isinstance(url, str)
    assert "#bootstrap=" in url, f"bootstrap should ride in #fragment, got {url!r}"
    assert "?bootstrap=" not in url, (
        f"bootstrap must not appear in query string (would leak to logs); got {url!r}"
    )


async def test_action_handler_no_user_signals_plain_url(wizard_mass: MagicMock) -> None:
    """Without a current user we still open the wizard, but without a bootstrap query."""
    wizard_mass.webserver.auth.create_token.reset_mock()

    url = await handle_open_connect_action(
        wizard_mass,
        current_user=None,
        mount_path="/mcp/v1",
    )

    wizard_mass.webserver.auth.create_token.assert_not_called()
    assert isinstance(url, str)
    assert "bootstrap=" not in url


async def test_action_handler_external_base_url_prepended(
    wizard_mass: MagicMock, mock_user: MagicMock
) -> None:
    """
    When ``external_base_url`` is provided, the signalled URL is fully qualified.

    Covers HA add-on ingress, where the path-only URL drops the ingress prefix
    and the wizard opens at the wrong location.
    """
    url = await handle_open_connect_action(
        wizard_mass,
        current_user=mock_user,
        mount_path="/mcp/v1",
        external_base_url="https://ha.example.com/d5369777_music_assistant_dev",
    )

    assert isinstance(url, str)
    assert url.startswith("https://ha.example.com/d5369777_music_assistant_dev/mcp/v1/connect")
    assert "bootstrap=jwt-xyz" in url


async def test_action_handler_external_base_url_strips_trailing_slash(
    wizard_mass: MagicMock,
) -> None:
    """A trailing slash on ``external_base_url`` must not produce a double-slash."""
    url = await handle_open_connect_action(
        wizard_mass,
        current_user=None,
        mount_path="/mcp/v1",
        external_base_url="https://ha.example.com/addon/",
    )

    assert url == "https://ha.example.com/addon/mcp/v1/connect"


async def test_action_handler_includes_ingress_aware_setup_callback(
    wizard_mass: MagicMock,
) -> None:
    """A setup callback uses the same ingress prefix as the opened wizard."""
    url = await handle_open_connect_action(
        wizard_mass,
        current_user=None,
        mount_path="/mcp/v1",
        external_base_url="https://ha.example.com/addon",
        setup_callback_path="/setup_flow/callback/a1b2",
    )

    fragment = parse_qs(urlsplit(url).fragment)
    assert fragment["setup_callback"] == ["/addon/setup_flow/callback/a1b2"]


def test_wizard_signals_setup_after_client_config_is_available() -> None:
    """The browser wizard retains and signals the setup callback after token generation."""
    assert 'params.get("setup_callback")' in HTML
    assert "signalSetupComplete();" in HTML
    assert "fetch(state.setupCallback" in HTML


async def test_action_handler_empty_external_base_url_falls_back_to_path(
    wizard_mass: MagicMock,
) -> None:
    """An empty / ``None`` ``external_base_url`` preserves the legacy path-only URL."""
    url = await handle_open_connect_action(
        wizard_mass,
        current_user=None,
        mount_path="/mcp/v1",
        external_base_url="",
    )

    assert url == "/mcp/v1/connect"


async def test_open_connect_gcs_prior_wizard_tokens(
    wizard_mass: MagicMock, mock_user: MagicMock
) -> None:
    """
    Prior MCP — wizard bootstrap/session tokens are revoked before the new bootstrap is minted.

    Per-client tokens (``MCP — Cursor`` etc.) are left untouched. Asserts
    against the sanctioned ``auth.revoke_token`` API.
    """
    auth = wizard_mass.webserver.auth
    auth.get_user_tokens = AsyncMock(
        return_value=[
            SimpleNamespace(token_id="boot-old", name="MCP — wizard bootstrap", user_id="u1"),
            SimpleNamespace(token_id="sess-old", name="MCP — wizard session", user_id="u1"),
            SimpleNamespace(token_id="cursor-keep", name="MCP — Cursor", user_id="u1"),
        ]
    )

    await handle_open_connect_action(
        wizard_mass,
        current_user=mock_user,
        mount_path="/mcp/v1",
    )

    revoked_ids = sorted(call.args[0] for call in auth.revoke_token.await_args_list)
    assert revoked_ids == ["boot-old", "sess-old"]
    auth.create_token.assert_awaited_once_with(
        user=mock_user,
        name="MCP — wizard bootstrap",
        is_long_lived=False,
    )


async def test_open_connect_gc_lookup_failure_does_not_block(
    wizard_mass: MagicMock, mock_user: MagicMock
) -> None:
    """A ``get_user_tokens`` exception is swallowed; the new bootstrap mint still happens."""
    auth = wizard_mass.webserver.auth
    auth.get_user_tokens = AsyncMock(side_effect=RuntimeError("api down"))

    await handle_open_connect_action(
        wizard_mass,
        current_user=mock_user,
        mount_path="/mcp/v1",
    )

    auth.create_token.assert_awaited_once()


async def test_open_connect_no_user_skips_gc(wizard_mass: MagicMock) -> None:
    """Without a current user there is no token listing and nothing is revoked."""
    auth = wizard_mass.webserver.auth

    await handle_open_connect_action(
        wizard_mass,
        current_user=None,
        mount_path="/mcp/v1",
    )

    auth.get_user_tokens.assert_not_called()
    auth.revoke_token.assert_not_called()


# ── Dispatch: WS-client auto-detect + config-override fallback ───────────────


def _matching_user() -> SimpleNamespace:
    return SimpleNamespace(user_id="u1", username="tester")


def test_detect_external_base_url_prefers_matching_client() -> None:
    """The detector returns the ``base_url`` of the WS client owned by the user."""
    user = _matching_user()
    other = SimpleNamespace(user_id="u2", username="someone-else")
    clients = [
        SimpleNamespace(
            _authenticated_user=other,
            base_url="https://wrong.example.com",
        ),
        SimpleNamespace(
            _authenticated_user=user,
            base_url="https://ha.example.com/d5369777_music_assistant_dev",
        ),
    ]
    mass = MagicMock()
    mass.webserver.clients = clients

    assert (
        _detect_external_base_url(mass, user)
        == "https://ha.example.com/d5369777_music_assistant_dev"
    )


def test_detect_external_base_url_returns_none_without_match() -> None:
    """No matching client → ``None`` so the dispatcher can fall through."""
    user = _matching_user()
    clients = [
        SimpleNamespace(
            _authenticated_user=SimpleNamespace(user_id="other", username="other"),
            base_url="https://other.example.com",
        ),
        SimpleNamespace(_authenticated_user=user, base_url=None),
    ]
    mass = MagicMock()
    mass.webserver.clients = clients

    assert _detect_external_base_url(mass, user) is None


def test_detect_external_base_url_handles_no_user() -> None:
    """No current user → ``None`` (the dispatcher then tries the config override)."""
    mass = MagicMock()
    mass.webserver.clients = []

    assert _detect_external_base_url(mass, None) is None


@pytest.mark.parametrize(
    "candidate",
    [
        "javascript:alert(1)",
        "//attacker.example.com",
        "ha.example.com/addon",  # missing scheme — would be treated as path-relative
        "ftp://example.com",
        "",
        "   ",
        None,
    ],
)
def test_sanitize_external_base_url_rejects_unsafe(candidate: str | None) -> None:
    """Only ``http(s)://`` values survive — anything else is dropped."""
    assert _sanitize_external_base_url(candidate) is None


@pytest.mark.parametrize(
    "candidate",
    [
        "https://ha.example.com/d5369777_music_assistant_dev",
        "http://localhost:8095",
        "HTTPS://Upper.Case.Example.COM",  # case-insensitive scheme check
    ],
)
def test_sanitize_external_base_url_accepts_http_schemes(candidate: str) -> None:
    """``http://`` and ``https://`` values pass through (whitespace trimmed)."""
    assert _sanitize_external_base_url(f"  {candidate}  ") == candidate


def _install_fake_ma_auth_middleware(monkeypatch: pytest.MonkeyPatch, user: object) -> None:
    """
    Make ``get_current_user()`` return ``user`` inside ``_dispatch_open_connect``.

    The provider imports ``music_assistant.controllers.webserver.helpers.auth_middleware``
    lazily; ``music_assistant`` is an optional / dev-only dep, so we inject a
    stub module tree into ``sys.modules`` rather than importing the real one.
    """
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
    auth_mod.get_current_user = lambda: user  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "music_assistant", pkg)
    monkeypatch.setitem(sys.modules, "music_assistant.controllers", controllers)
    monkeypatch.setitem(sys.modules, "music_assistant.controllers.webserver", webserver_pkg)
    monkeypatch.setitem(sys.modules, "music_assistant.controllers.webserver.helpers", helpers_pkg)
    monkeypatch.setitem(
        sys.modules,
        "music_assistant.controllers.webserver.helpers.auth_middleware",
        auth_mod,
    )


async def test_dispatch_detects_ws_client_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end dispatch: the WS client's ingress base_url ends up in the URL."""
    user = _matching_user()
    _install_fake_ma_auth_middleware(monkeypatch, user)

    mass = MagicMock()
    mass.webserver.clients = [
        SimpleNamespace(
            _authenticated_user=user,
            base_url="https://ha.example.com/d5369777_music_assistant_dev",
        )
    ]
    mass.webserver.auth.create_token = AsyncMock(return_value="jwt-xyz")
    url = await _dispatch_open_connect(
        mass,
        {"mount_path": "/mcp/v1"},
    )

    assert url is not None
    assert url.startswith("https://ha.example.com/d5369777_music_assistant_dev/mcp/v1/connect")
    assert "bootstrap=jwt-xyz" in url


async def test_dispatch_falls_back_to_config_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no WS client matches, an explicit ``connect_external_url`` wins."""
    user = _matching_user()
    _install_fake_ma_auth_middleware(monkeypatch, user)

    mass = MagicMock()
    mass.webserver.clients = []
    mass.webserver.auth.create_token = AsyncMock(return_value="jwt-xyz")
    url = await _dispatch_open_connect(
        mass,
        {
            "mount_path": "/mcp/v1",
            CONF_CONNECT_EXTERNAL_URL: "https://override.example.com",
        },
    )

    assert url is not None
    assert url.startswith("https://override.example.com/mcp/v1/connect")


async def test_dispatch_rejects_unsafe_override_and_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    An override with a non-``http(s)`` scheme is dropped → path-only fallback.

    Guards against an admin pasting ``javascript:…`` into the config; the
    frontend would otherwise hand that straight to ``window.open``.
    """
    user = _matching_user()
    _install_fake_ma_auth_middleware(monkeypatch, user)

    mass = MagicMock()
    mass.webserver.clients = []
    mass.webserver.auth.create_token = AsyncMock(return_value="jwt-xyz")
    url = await _dispatch_open_connect(
        mass,
        {
            "mount_path": "/mcp/v1",
            CONF_CONNECT_EXTERNAL_URL: "javascript:alert(1)",
        },
    )

    assert url is not None
    assert url.startswith("/mcp/v1/connect")
    assert "javascript" not in url


async def test_dispatch_falls_back_to_path_only_when_nothing_known(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No WS match and no override → legacy path-only URL (no regression)."""
    user = _matching_user()
    _install_fake_ma_auth_middleware(monkeypatch, user)

    mass = MagicMock()
    mass.webserver.clients = []
    mass.webserver.auth.create_token = AsyncMock(return_value="jwt-xyz")
    url = await _dispatch_open_connect(
        mass,
        {"mount_path": "/mcp/v1"},
    )

    assert url is not None
    assert url.startswith("/mcp/v1/connect")
    assert "://" not in url.split("?", 1)[0]


async def test_dispatch_uses_server_base_url_for_direct_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct clients receive an absolute URL that the frontend will open."""
    user = _matching_user()
    _install_fake_ma_auth_middleware(monkeypatch, user)

    mass = MagicMock()
    mass.webserver.clients = []
    mass.webserver.base_url = "http://192.0.2.20:8095"
    mass.webserver.auth.create_token = AsyncMock(return_value="jwt-xyz")

    url = await _dispatch_open_connect(mass, {"mount_path": "/mcp/v1"})

    assert url is not None
    assert url.startswith("http://192.0.2.20:8095/mcp/v1/connect")


# ── Client template integrity ────────────────────────────────────────────────


def test_cursor_template_round_trips() -> None:
    """The Cursor template renders to valid JSON with url + Authorization Bearer header."""
    cursor = lookup_client("cursor")
    assert cursor is not None
    method = cursor.methods[0]
    assert method.id == "user-config"
    rendered = method.template.replace("{{URL}}", "http://localhost:8095/mcp/v1").replace(
        "{{TOKEN}}", "TOK-123"
    )
    parsed = json.loads(rendered)
    server = parsed["mcpServers"]["ma"]
    assert server["url"] == "http://localhost:8095/mcp/v1"
    assert server["headers"]["Authorization"] == "Bearer TOK-123"


def test_opencode_template_round_trips() -> None:
    """The OpenCode preset renders an authenticated remote MCP configuration."""
    spec = lookup_client("opencode")
    assert spec is not None
    rendered = (
        spec.methods[0]
        .template.replace("{{URL}}", "http://localhost:8095/mcp/v1")
        .replace("{{TOKEN}}", "TOK-123")
    )
    parsed = json.loads(rendered)
    server = parsed["mcp"]["ma"]
    assert parsed["$schema"] == "https://opencode.ai/config.json"
    assert server["type"] == "remote"
    assert server["url"] == "http://localhost:8095/mcp/v1"
    assert server["enabled"] is True
    assert server["oauth"] is False
    assert server["headers"]["Authorization"] == "Bearer TOK-123"


def test_openhands_template_uses_http_transport_and_bearer_header() -> None:
    """The OpenHands preset follows the documented remote-server CLI syntax."""
    spec = lookup_client("openhands")
    assert spec is not None
    rendered = (
        spec.methods[0]
        .template.replace("{{URL}}", "http://localhost:8095/mcp/v1")
        .replace("{{TOKEN}}", "TOK-123")
    )
    assert rendered.startswith("openhands mcp add ma --transport http")
    assert '--header "Authorization: Bearer TOK-123"' in rendered
    assert rendered.endswith("http://localhost:8095/mcp/v1")


def test_github_copilot_cli_template_uses_mcp_add_form() -> None:
    """The Copilot CLI preset supplies every value requested by ``/mcp add``."""
    spec = lookup_client("github-copilot-cli")
    assert spec is not None
    rendered = (
        spec.methods[1]
        .template.replace("{{URL}}", "http://localhost:8095/mcp/v1")
        .replace("{{TOKEN}}", "TOK-123")
    )
    assert rendered.startswith("/mcp add\n")
    assert "Server Name: ma" in rendered
    assert "Server Type: HTTP" in rendered
    assert "URL: http://localhost:8095/mcp/v1" in rendered
    assert 'HTTP Headers: {"Authorization":"Bearer TOK-123"}' in rendered
    assert "Tools: *" in rendered


def test_claude_code_template_uses_positional_url() -> None:
    """
    ``claude mcp add`` takes the URL as a positional argument, not via ``--url``.

    Regression for the v0.3.x wizard shipping ``claude mcp add ma --transport http
    --url <URL>`` — the CLI ignored ``--url`` and registered an unreachable server.
    """
    spec = lookup_client("claude-code")
    assert spec is not None
    assert [method.id for method in spec.methods] == ["cli", "project-config"]
    rendered = (
        spec.methods[0]
        .template.replace("{{URL}}", "http://localhost:8095/mcp/v1")
        .replace("{{TOKEN}}", "TOK-123")
    )
    assert "--url" not in rendered, "claude mcp add does not accept a --url flag"
    # URL must appear right after the server name (the positional slot).
    assert "ma http://localhost:8095/mcp/v1" in rendered
    assert "--transport http" in rendered
    assert "--scope user" in rendered
    assert '--header "Authorization: Bearer TOK-123"' in rendered


def test_claude_code_manual_config_round_trips() -> None:
    """Claude Code's alternate project config remains valid authenticated JSON."""
    spec = lookup_client("claude-code")
    assert spec is not None
    method = spec.methods[1]
    rendered = method.template.replace("{{URL}}", "http://localhost:8095/mcp/v1").replace(
        "{{TOKEN}}", "TOK-123"
    )
    server = json.loads(rendered)["mcpServers"]["ma"]
    assert server == {
        "type": "http",
        "url": "http://localhost:8095/mcp/v1",
        "headers": {"Authorization": "Bearer TOK-123"},
    }


def test_current_cli_templates_use_streamable_http_and_bearer_options() -> None:
    """Reviewed CLI presets use each product's current first-party syntax."""
    rendered: dict[str, str] = {}
    for client_id in ("github-copilot-cli", "gemini-cli", "openclaw", "openhands"):
        spec = lookup_client(client_id)
        assert spec is not None
        rendered[client_id] = (
            spec.methods[0].template.replace("{{URL}}", "URL").replace("{{TOKEN}}", "TOKEN")
        )
    assert rendered["github-copilot-cli"].startswith("copilot mcp add --transport http")
    assert '--header "Authorization: Bearer TOKEN"' in rendered["github-copilot-cli"]
    assert rendered["gemini-cli"].startswith("gemini mcp add --scope user --transport http")
    assert rendered["openclaw"].startswith("openclaw mcp add ma")
    assert "--transport streamable-http" in rendered["openclaw"]
    assert rendered["openhands"].startswith("openhands mcp add ma")


def test_current_config_templates_use_product_specific_http_keys() -> None:
    """Reviewed JSON presets retain each client's distinct transport schema."""
    windsurf = lookup_client("windsurf")
    vscode = lookup_client("vscode")
    cline = lookup_client("cline")
    assert windsurf is not None
    assert vscode is not None
    assert cline is not None
    devin = json.loads(
        windsurf.methods[0].template.replace("{{URL}}", "URL").replace("{{TOKEN}}", "TOKEN")
    )["mcpServers"]["ma"]
    vs_server = json.loads(
        vscode.methods[0].template.replace("{{URL}}", "URL").replace("{{TOKEN}}", "TOKEN")
    )["servers"]["ma"]
    cline_server = json.loads(
        cline.methods[0].template.replace("{{URL}}", "URL").replace("{{TOKEN}}", "TOKEN")
    )["mcpServers"]["ma"]
    assert devin["transport"] == "http"
    assert vs_server["type"] == "http"
    assert cline_server["type"] == "streamableHttp"


def test_custom_template_exposes_connection_parameters() -> None:
    """Custom renders product-neutral values needed by any MCP client."""
    spec = lookup_client("custom")
    assert spec is not None
    assert spec.label == "Custom"
    assert len(spec.methods) == 1
    method = spec.methods[0]
    assert method.id == "parameters"
    assert method.kind == "text"
    rendered = method.template.replace("{{URL}}", "https://ma.example/mcp/v1").replace(
        "{{TOKEN}}", "TOK-123"
    )
    assert "Server name: ma" in rendered
    assert "Transport: Streamable HTTP" in rendered
    assert "URL: https://ma.example/mcp/v1" in rendered
    assert "Header name: Authorization" in rendered
    assert "Header value: Bearer TOK-123" in rendered


def test_roo_code_template_uses_streamable_http() -> None:
    """Roo Code renders its documented remote server configuration."""
    spec = lookup_client("roo-code")
    assert spec is not None
    assert [method.id for method in spec.methods] == ["global-config", "project-config"]
    rendered = (
        spec.methods[0]
        .template.replace("{{URL}}", "https://ma.example/mcp/v1")
        .replace("{{TOKEN}}", "TOK-123")
    )
    server = json.loads(rendered)["mcpServers"]["ma"]
    assert server == {
        "type": "streamable-http",
        "url": "https://ma.example/mcp/v1",
        "headers": {"Authorization": "Bearer TOK-123"},
        "disabled": False,
        "alwaysAllow": [],
    }
    assert "global" in spec.methods[0].config_path_hint.lower()
    assert spec.methods[1].config_path_hint == ".roo/mcp.json in the project root."


def test_openclaw_template_round_trips() -> None:
    """The OpenClaw preset uses its current add command and HTTP transport."""
    spec = lookup_client("openclaw")
    assert spec is not None
    rendered = (
        spec.methods[0]
        .template.replace("{{URL}}", "http://localhost:8095/mcp/v1")
        .replace("{{TOKEN}}", "TOK-123")
    )
    assert rendered.startswith("openclaw mcp add ma --url http://localhost:8095/mcp/v1")
    assert "--transport streamable-http" in rendered
    assert '--header "Authorization: Bearer TOK-123"' in rendered


def test_hermes_template_round_trips() -> None:
    """The Hermes preset renders valid YAML with url + Authorization Bearer header."""
    spec = lookup_client("hermes")
    assert spec is not None
    method = spec.methods[1]
    assert method.kind == "yaml"
    rendered = method.template.replace("{{URL}}", "http://localhost:8095/mcp/v1").replace(
        "{{TOKEN}}", "TOK-123"
    )
    parsed = yaml.safe_load(rendered)
    server = parsed["mcp_servers"]["ma"]
    assert server["url"] == "http://localhost:8095/mcp/v1"
    assert server["headers"]["Authorization"] == "Bearer TOK-123"


def test_all_clients_have_required_fields() -> None:
    """Every client spec has the fields the JS UI relies on."""
    seen_ids: set[str] = set()
    for spec in CLIENTS:
        assert spec.id
        assert spec.id not in seen_ids
        seen_ids.add(spec.id)
        assert spec.label
        assert spec.methods
        seen_method_ids: set[str] = set()
        for method in spec.methods:
            assert method.id
            assert method.id not in seen_method_ids
            seen_method_ids.add(method.id)
            assert method.label
            assert method.kind in {"json", "shell", "text", "toml", "yaml"}
            assert method.action in {"copy", "download"}
            assert "{{URL}}" in method.template
            assert "{{TOKEN}}" in method.template
            assert method.config_path_hint
            if method.kind in {"json", "toml", "yaml"}:
                assert method.action == "download"
                assert method.filename


def test_page_selects_recommended_method_without_minting() -> None:
    """Method selection is client-local presentation and cannot mint credentials."""
    assert "selectedMethodIds: {}" in HTML
    assert "c.methods[0].id" in HTML
    assert "selectMethod(c.id, method.id)" in HTML
    select_method = HTML.split("function selectMethod", 1)[1].split("function ", 1)[0]
    assert "mintForSelected" not in select_method
    assert "method-label recommended" in HTML


def test_page_uses_network_url_by_default() -> None:
    """Generated connection details prefer the advertised network endpoint."""
    assert '<button data-which="network" class="active">Network</button>' in HTML
    assert '<button data-which="loopback">Loopback</button>' in HTML
    assert 'urlMode: "network"' in HTML
    assert 'mode === "network"' in HTML
