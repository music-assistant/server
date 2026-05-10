"""Tests for the Connect Wizard — endpoints, action handler, client templates.

These tests run against the real :func:`mount_connect_wizard` flow on a
``FakeWebserver`` (no real MA stack required); ``mass.webserver.auth`` is
stubbed with ``AsyncMock`` / ``MagicMock`` so we can assert exactly which auth
calls the wizard fires for each user-facing operation.
"""
# ruff: noqa: D401
#   D401: pytest fixture/test docstrings describe *what is returned*.
#   S101: ``assert`` is the pytest convention.
#   PLR2004: small magic numbers (10 client specs, 5 routes) are obvious in context.
# mypy: disable-error-code="type-arg"

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from music_assistant.providers.fastmcp_server.connect.actions import handle_open_connect_action
from music_assistant.providers.fastmcp_server.connect.clients import CLIENTS, lookup_client
from music_assistant.providers.fastmcp_server.connect.mount import mount_connect_wizard

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
        version="0.3.0",
        enabled_tags_provider=lambda: ["query:library", "control:playback"],
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


async def test_info_endpoint_shape(wizard_client: TestClient) -> None:
    """``GET /mcp/v1/connect/info`` returns the meta JSON the UI needs."""
    resp = await wizard_client.get(
        "/mcp/v1/connect/info", headers={"Origin": "http://localhost:8095"}
    )
    assert resp.status == 200
    data = await resp.json()
    for key in (
        "version",
        "mount_path",
        "mcp_url_loopback",
        "mcp_url_advertised",
        "permissions",
        "clients",
        "well_known_url",
    ):
        assert key in data, f"missing key: {key}"
    assert data["mount_path"] == "/mcp/v1"
    assert data["mcp_url_loopback"].endswith("/mcp/v1")
    assert isinstance(data["clients"], list)
    assert len(data["clients"]) >= 10
    assert isinstance(data["permissions"], list)
    assert all(isinstance(p, str) for p in data["permissions"])


async def test_info_reflects_enabled_tags(wizard_mass: MagicMock) -> None:
    """``info.permissions`` reflects whatever ``enabled_tags_provider()`` returns."""
    unmount = await mount_connect_wizard(
        wizard_mass,
        mount_path="/mcp/v1",
        version="0.3.0",
        enabled_tags_provider=lambda: ["control:playback", "edit:queue"],
        extra_origins_csv="",
    )
    try:
        async with TestClient(TestServer(build_aiohttp_app(wizard_mass.webserver))) as client:
            resp = await client.get("/mcp/v1/connect/info")
            data = await resp.json()
            assert "control:playback" in data["permissions"]
            assert "edit:queue" in data["permissions"]
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
        version="0.3.0",
        enabled_tags_provider=list,
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
        version="0.3.0",
        enabled_tags_provider=list,
        extra_origins_csv="",
    )
    try:
        paths = [r[0] for r in wizard_mass.webserver.routes]
        assert all(p.startswith("/custom/connect") for p in paths)
    finally:
        unmount()


# ── ACTION handler (signal_event) ────────────────────────────────────────────


async def test_action_handler_signals_url_with_bootstrap(
    wizard_mass: MagicMock, mock_user: MagicMock
) -> None:
    """Action handler mints a bootstrap token and signals a URL containing it."""
    await handle_open_connect_action(
        wizard_mass,
        current_user=mock_user,
        mount_path="/mcp/v1",
        base_url="http://localhost:8095",
    )

    wizard_mass.webserver.auth.create_token.assert_awaited_with(
        user=mock_user,
        name="MCP — wizard bootstrap",
        is_long_lived=False,
    )
    wizard_mass.signal_event.assert_called_once()
    args, kwargs = wizard_mass.signal_event.call_args
    url = kwargs.get("data") if "data" in kwargs else args[-1]
    assert isinstance(url, str)
    # Path-only URL — the MA frontend resolves it against the user's location
    # so the wizard works in Docker / HA add-on deployments where MA's
    # advertised base_url points at an internal IP the browser cannot reach.
    assert url.startswith("/mcp/v1/connect")
    assert "bootstrap=jwt-xyz" in url


async def test_action_handler_no_user_signals_plain_url(wizard_mass: MagicMock) -> None:
    """Without a current user we still open the wizard, but without a bootstrap query."""
    wizard_mass.webserver.auth.create_token.reset_mock()

    await handle_open_connect_action(
        wizard_mass,
        current_user=None,
        mount_path="/mcp/v1",
        base_url="http://localhost:8095",
    )

    wizard_mass.webserver.auth.create_token.assert_not_called()
    wizard_mass.signal_event.assert_called_once()
    args, kwargs = wizard_mass.signal_event.call_args
    url = kwargs.get("data") if "data" in kwargs else args[-1]
    assert isinstance(url, str)
    assert "bootstrap=" not in url


# ── Client template integrity ────────────────────────────────────────────────


def test_cursor_template_round_trips() -> None:
    """The Cursor template renders to valid JSON with url + Authorization Bearer header."""
    cursor = lookup_client("cursor")
    assert cursor is not None
    rendered = cursor.template.replace("{{URL}}", "http://localhost:8095/mcp/v1").replace(
        "{{TOKEN}}", "TOK-123"
    )
    parsed = json.loads(rendered)
    server = parsed["mcpServers"]["ma"]
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
        assert spec.kind in {"json", "shell", "toml"}
        assert "{{URL}}" in spec.template
        assert "{{TOKEN}}" in spec.template
        assert spec.config_path_hint  # non-empty doc hint
