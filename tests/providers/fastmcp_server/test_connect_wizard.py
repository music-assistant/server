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

from music_assistant.providers.fastmcp_server import (
    _detect_external_base_url,
    _dispatch_open_connect,
    _sanitize_external_base_url,
)
from music_assistant.providers.fastmcp_server.connect.actions import handle_open_connect_action
from music_assistant.providers.fastmcp_server.connect.clients import CLIENTS, lookup_client
from music_assistant.providers.fastmcp_server.connect.mount import mount_connect_wizard
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


async def test_action_handler_external_base_url_prepended(
    wizard_mass: MagicMock, mock_user: MagicMock
) -> None:
    """When ``external_base_url`` is provided, the signalled URL is fully qualified.

    Covers HA add-on ingress, where the path-only URL drops the ingress prefix
    and the wizard opens at the wrong location.
    """
    await handle_open_connect_action(
        wizard_mass,
        current_user=mock_user,
        mount_path="/mcp/v1",
        external_base_url="https://ha.example.com/d5369777_music_assistant_dev",
    )

    wizard_mass.signal_event.assert_called_once()
    args, kwargs = wizard_mass.signal_event.call_args
    url = kwargs.get("data") if "data" in kwargs else args[-1]
    assert isinstance(url, str)
    assert url.startswith("https://ha.example.com/d5369777_music_assistant_dev/mcp/v1/connect")
    assert "bootstrap=jwt-xyz" in url


async def test_action_handler_external_base_url_strips_trailing_slash(
    wizard_mass: MagicMock,
) -> None:
    """A trailing slash on ``external_base_url`` must not produce a double-slash."""
    await handle_open_connect_action(
        wizard_mass,
        current_user=None,
        mount_path="/mcp/v1",
        external_base_url="https://ha.example.com/addon/",
    )

    args, kwargs = wizard_mass.signal_event.call_args
    url = kwargs.get("data") if "data" in kwargs else args[-1]
    assert url == "https://ha.example.com/addon/mcp/v1/connect"


async def test_action_handler_empty_external_base_url_falls_back_to_path(
    wizard_mass: MagicMock,
) -> None:
    """An empty / ``None`` ``external_base_url`` preserves the legacy path-only URL."""
    await handle_open_connect_action(
        wizard_mass,
        current_user=None,
        mount_path="/mcp/v1",
        external_base_url="",
    )

    args, kwargs = wizard_mass.signal_event.call_args
    url = kwargs.get("data") if "data" in kwargs else args[-1]
    assert url == "/mcp/v1/connect"


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
    """Make ``get_current_user()`` return ``user`` inside ``_dispatch_open_connect``.

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

    signalled: list[str] = []
    mass = MagicMock()
    mass.webserver.clients = [
        SimpleNamespace(
            _authenticated_user=user,
            base_url="https://ha.example.com/d5369777_music_assistant_dev",
        )
    ]
    mass.webserver.auth.create_token = AsyncMock(return_value="jwt-xyz")
    mass.signal_event = MagicMock(
        side_effect=lambda _evt, object_id, data: signalled.append(data)  # noqa: ARG005
    )

    await _dispatch_open_connect(
        mass,
        {"mount_path": "/mcp/v1", "session_id": "sess-x"},
    )

    assert signalled, "expected signal_event to be called"
    url = signalled[0]
    assert url.startswith("https://ha.example.com/d5369777_music_assistant_dev/mcp/v1/connect")
    assert "bootstrap=jwt-xyz" in url


async def test_dispatch_falls_back_to_config_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no WS client matches, an explicit ``connect_external_url`` wins."""
    user = _matching_user()
    _install_fake_ma_auth_middleware(monkeypatch, user)

    signalled: list[str] = []
    mass = MagicMock()
    mass.webserver.clients = []
    mass.webserver.auth.create_token = AsyncMock(return_value="jwt-xyz")
    mass.signal_event = MagicMock(
        side_effect=lambda _evt, object_id, data: signalled.append(data)  # noqa: ARG005
    )

    await _dispatch_open_connect(
        mass,
        {
            "mount_path": "/mcp/v1",
            "session_id": "sess-y",
            CONF_CONNECT_EXTERNAL_URL: "https://override.example.com",
        },
    )

    url = signalled[0]
    assert url.startswith("https://override.example.com/mcp/v1/connect")


async def test_dispatch_rejects_unsafe_override_and_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An override with a non-``http(s)`` scheme is dropped → path-only fallback.

    Guards against an admin pasting ``javascript:…`` into the config; the
    frontend would otherwise hand that straight to ``window.open``.
    """
    user = _matching_user()
    _install_fake_ma_auth_middleware(monkeypatch, user)

    signalled: list[str] = []
    mass = MagicMock()
    mass.webserver.clients = []
    mass.webserver.auth.create_token = AsyncMock(return_value="jwt-xyz")
    mass.signal_event = MagicMock(
        side_effect=lambda _evt, object_id, data: signalled.append(data)  # noqa: ARG005
    )

    await _dispatch_open_connect(
        mass,
        {
            "mount_path": "/mcp/v1",
            "session_id": "sess-bad",
            CONF_CONNECT_EXTERNAL_URL: "javascript:alert(1)",
        },
    )

    url = signalled[0]
    assert url.startswith("/mcp/v1/connect")
    assert "javascript" not in url


async def test_dispatch_falls_back_to_path_only_when_nothing_known(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No WS match and no override → legacy path-only URL (no regression)."""
    user = _matching_user()
    _install_fake_ma_auth_middleware(monkeypatch, user)

    signalled: list[str] = []
    mass = MagicMock()
    mass.webserver.clients = []
    mass.webserver.auth.create_token = AsyncMock(return_value="jwt-xyz")
    mass.signal_event = MagicMock(
        side_effect=lambda _evt, object_id, data: signalled.append(data)  # noqa: ARG005
    )

    await _dispatch_open_connect(
        mass,
        {"mount_path": "/mcp/v1", "session_id": "sess-z"},
    )

    url = signalled[0]
    assert url.startswith("/mcp/v1/connect")
    assert "://" not in url.split("?", 1)[0]


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
