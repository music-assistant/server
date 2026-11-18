"""Tests for the pages a provider serves, behind a short-lived address."""

from __future__ import annotations

import json
import logging
import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from music_assistant_models.auth import Scope, User, UserRole
from music_assistant_models.config_entries import ConfigActionResult
from music_assistant_models.enums import ProviderTier

from music_assistant import providers as providers_package
from music_assistant.controllers.webserver import provider_pages
from music_assistant.controllers.webserver.helpers.auth_middleware import user_auth_context
from music_assistant.controllers.webserver.provider_pages import (
    MAX_PAGES_PER_USER,
    PAGE_CSP,
    PAGE_IDLE_TIMEOUT,
    PAGE_MAX_AGE,
    ProviderPageManager,
)
from music_assistant.helpers.util import purge_provider_modules

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from aiohttp import ClientResponse

    from music_assistant.mass import MusicAssistant

PAGE = "page_provider"
PLAIN = "page_less_provider"
OPEN = {
    "id": "open_page",
    "label": "Open the page",
    "media_types": ["album"],
    "scope": "library.write",
}
ALBUM_URI = "library://album/1"
PAGE_CODE = """
from aiohttp import web
from music_assistant_models.config_entries import ConfigActionResult

from music_assistant.controllers.webserver.helpers.auth_middleware import get_current_user
from music_assistant.models.plugin import PluginProvider

# what an action answers, set by the test
ANSWER = {"open_page": "stats?range=week"}


class PageProvider(PluginProvider):
    async def handle_menu_action(self, action_id, uri, player_id):
        return ConfigActionResult(**ANSWER)

    async def handle_config_action(self, action):
        return ConfigActionResult(**ANSWER)

    async def handle_page_request(self, request, path):
        user = get_current_user().username
        if path == "stream":
            response = web.StreamResponse()
            response.set_cookie("session", "secret")
            await response.prepare(request)
            await response.write(f"streamed for {user}".encode())
            return response
        if path == "fail":
            raise RuntimeError("the page broke")
        if path == "ws":
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.send_str(f"{await ws.receive_str()} for {user}")
            await ws.close()
            return ws
        if request.method == "POST":
            return web.json_response({"posted": await request.json(), "user": user})
        return web.Response(
            text=f"{path} {request.query_string} for {user}",
            headers={"Set-Cookie": "session=secret", "Content-Security-Policy": "img-src 'none'"},
        )


async def setup(mass, manifest, config):
    return PageProvider(mass, manifest, config)
"""
PLAIN_CODE = """
from music_assistant_models.config_entries import ConfigActionResult

from music_assistant.models.plugin import PluginProvider


class PlainProvider(PluginProvider):
    async def handle_menu_action(self, action_id, uri, player_id):
        return ConfigActionResult(open_page="")


async def setup(mass, manifest, config):
    return PlainProvider(mass, manifest, config)
"""


@pytest.fixture
def location(tmp_path: Path) -> Iterator[Path]:
    """Return a folder of community providers; what a test imports from it is undone."""
    folder = tmp_path / "community_providers"
    folder.mkdir()
    original_path = list(providers_package.__path__)
    imported_before = set(sys.modules)
    yield folder
    providers_package.__path__[:] = original_path
    for domain in (PAGE, PLAIN):
        if f"{providers_package.__name__}.{domain}" not in imported_before:
            purge_provider_modules(domain)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Return a single-element list holding the monotonic time the page sessions read."""
    now = [1000.0]
    monkeypatch.setattr(provider_pages, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now


@pytest.fixture
def pages() -> ProviderPageManager:
    """Return a page manager on a stubbed webserver."""
    webserver = MagicMock()
    webserver.logger = logging.getLogger("test_provider_pages")
    return ProviderPageManager(webserver)


@pytest.fixture
def user() -> User:
    """Return a user to call as."""
    return User(user_id="u1", username="user", role=UserRole.USER)


async def test_menu_action_opens_page_for_its_user(mass: MusicAssistant, location: Path) -> None:
    """A menu action's page path becomes an address that serves the provider as that user."""
    instance_id = await _register(mass, location)
    user, token = await _user(mass, UserRole.USER)

    address = await _open(mass, token, instance_id)

    assert address.startswith("provider_page/")
    assert address.endswith("/stats?range=week")
    async with _get(mass, address) as response:
        assert response.status == 200
        assert await response.text() == f"stats range=week for {user.username}"
    # opened again, the page gets a fresh address and the first one keeps working
    second = await _open(mass, token, instance_id)
    assert second != address
    for each in (address, second):
        async with _get(mass, each) as response:
            assert response.status == 200


async def test_settings_action_opens_page(mass: MusicAssistant, location: Path) -> None:
    """A settings action button opens the page in the same way."""
    instance_id = await _register(mass, location)
    _, token = await _user(mass, UserRole.ADMIN)

    result = await _api(
        mass,
        token,
        "config/providers/invoke_action",
        instance_id=instance_id,
        action="open",
    )

    assert result["open_page"].startswith("provider_page/")
    async with _get(mass, result["open_page"]) as response:
        assert response.status == 200


@pytest.mark.parametrize("path", ["", "stream", "fail"])
async def test_headers_forced_on_every_response(
    mass: MusicAssistant, location: Path, path: str
) -> None:
    """A normal, streamed or error response gets the sandbox headers and loses its cookies."""
    instance_id = await _register(mass, location)
    _, token = await _user(mass, UserRole.USER)
    _answer(open_page=path)
    address = await _open(mass, token, instance_id)

    async with _get(mass, address) as response:
        assert response.status == (500 if path == "fail" else 200)
        _assert_page_headers(response)


async def test_provider_policy_kept_next_to_the_sandbox(
    mass: MusicAssistant, location: Path
) -> None:
    """A policy the provider sets stays, as a second one that can only narrow the sandbox."""
    instance_id = await _register(mass, location)
    _, token = await _user(mass, UserRole.USER)

    async with _get(mass, await _open(mass, token, instance_id)) as response:
        assert response.headers.getall("Content-Security-Policy") == [
            "img-src 'none'",
            PAGE_CSP,
        ]


async def test_page_posts_json_and_upgrades_to_websocket(
    mass: MusicAssistant, location: Path
) -> None:
    """A page can post JSON to its own sub-paths and open a WebSocket, as its user."""
    instance_id = await _register(mass, location)
    user, token = await _user(mass, UserRole.USER)
    _answer(open_page="")
    root = f"{mass.webserver.internal_base_url}/{await _open(mass, token, instance_id)}"

    # the preflight of a cross-origin JSON post, from the page's opaque origin
    async with mass.http_session_no_ssl.options(
        f"{root}api/connect",
        headers={"Origin": "null", "Access-Control-Request-Method": "POST"},
    ) as response:
        assert response.status == 204
        assert "POST" in response.headers["Access-Control-Allow-Methods"]
        assert response.headers["Access-Control-Allow-Headers"] == "Content-Type"
        _assert_page_headers(response)
    async with mass.http_session_no_ssl.post(
        f"{root}api/connect", json={"client": "claude"}, headers={"Origin": "null"}
    ) as response:
        assert await response.json() == {"posted": {"client": "claude"}, "user": user.username}
        _assert_page_headers(response)
    async with mass.http_session_no_ssl.ws_connect(f"{root}ws") as ws:
        await ws.send_str("hello")
        assert await ws.receive_str(timeout=10) == f"hello for {user.username}"


async def test_provider_without_page(mass: MusicAssistant, location: Path) -> None:
    """A provider that serves no page answers not found, with the sandbox headers."""
    await _register(mass, location)
    instance_id = await _setup(mass, PLAIN)
    _, token = await _user(mass, UserRole.USER)

    async with _get(mass, await _open(mass, token, instance_id)) as response:
        assert response.status == 404
        assert await response.text() == "This provider has no page."
        _assert_page_headers(response)


async def test_unknown_address(mass: MusicAssistant) -> None:
    """An address the server never issued answers that it has expired."""
    async with _get(mass, "provider_page/made-up/") as response:
        assert response.status == 410
        _assert_page_headers(response)


async def test_address_expires_when_idle(
    mass: MusicAssistant, location: Path, clock: list[float]
) -> None:
    """An address lapses after a quiet spell, while requests keep it alive until then."""
    instance_id = await _register(mass, location)
    _, token = await _user(mass, UserRole.USER)
    address = await _open(mass, token, instance_id)

    clock[0] += PAGE_IDLE_TIMEOUT - 1
    async with _get(mass, address) as response:
        assert response.status == 200
    clock[0] += PAGE_IDLE_TIMEOUT + 1
    async with _get(mass, address) as response:
        assert response.status == 410
        assert "open it again" in await response.text()


async def test_address_expires_at_its_max_age(
    mass: MusicAssistant, location: Path, clock: list[float]
) -> None:
    """An address in constant use still lapses at its maximum age."""
    instance_id = await _register(mass, location)
    _, token = await _user(mass, UserRole.USER)
    address = await _open(mass, token, instance_id)

    step = PAGE_IDLE_TIMEOUT - 1
    for _ in range(PAGE_MAX_AGE // step):
        clock[0] += step
        async with _get(mass, address) as response:
            assert response.status == 200
    clock[0] += step
    async with _get(mass, address) as response:
        assert response.status == 410


async def test_oldest_address_dropped_beyond_the_limit(
    mass: MusicAssistant, location: Path
) -> None:
    """A user holds a limited number of addresses, and the oldest goes first."""
    instance_id = await _register(mass, location)
    _, token = await _user(mass, UserRole.USER)
    addresses = [await _open(mass, token, instance_id) for _ in range(MAX_PAGES_PER_USER + 1)]

    async with _get(mass, addresses[0]) as response:
        assert response.status == 410
    async with _get(mass, addresses[1]) as response:
        assert response.status == 200


async def test_rechecks_the_user(mass: MusicAssistant, location: Path) -> None:
    """A user that is disabled or loses the scope can no longer open an issued address."""
    instance_id = await _register(mass, location)
    user, token = await _user(mass, UserRole.USER)
    address = await _open(mass, token, instance_id)
    users = mass.webserver.auth.database

    # changed in the database directly, so only the re-check on the request can catch it
    await users.update("users", {"user_id": user.user_id}, {"role": UserRole.GUEST.value})
    async with _get(mass, address) as response:
        assert response.status == 403
    await users.update("users", {"user_id": user.user_id}, {"role": UserRole.USER.value})
    await users.update("users", {"user_id": user.user_id}, {"enabled": 0})
    async with _get(mass, address) as response:
        assert response.status == 403
    await users.update("users", {"user_id": user.user_id}, {"enabled": 1})
    async with _get(mass, address) as response:
        assert response.status == 200


@pytest.mark.parametrize("withdraw", ["disable", "delete"])
async def test_addresses_dropped_with_the_user(
    mass: MusicAssistant, location: Path, withdraw: str
) -> None:
    """Disabling or deleting a user drops the addresses it holds."""
    instance_id = await _register(mass, location)
    user, token = await _user(mass, UserRole.USER)
    address = await _open(mass, token, instance_id)

    _, admin_token = await _user(mass, UserRole.ADMIN)
    await _api(mass, admin_token, f"auth/user/{withdraw}", user_id=user.user_id)
    if withdraw == "disable":
        # an account enabled again does not get its old addresses back
        await _api(mass, admin_token, "auth/user/enable", user_id=user.user_id)

    async with _get(mass, address) as response:
        assert response.status == 410


async def test_rechecks_the_instance(mass: MusicAssistant, location: Path) -> None:
    """An address only serves while its instance is available, and lapses when it unloads."""
    instance_id = await _register(mass, location)
    _, token = await _user(mass, UserRole.USER)
    address = await _open(mass, token, instance_id)
    provider = mass.get_provider(instance_id)
    assert provider is not None

    provider.available = False
    async with _get(mass, address) as response:
        assert response.status == 503
    provider.available = True
    async with _get(mass, address) as response:
        assert response.status == 200
    await mass.unload_provider(instance_id)
    await mass.load_provider(instance_id)
    async with _get(mass, address) as response:
        assert response.status == 410


@pytest.mark.parametrize(
    "path",
    [
        "https://example.org/page",
        "javascript:alert(1)",
        "/api",
        "//example.org/page",
        "../../api",
        "stats/../../api",
        "%2e%2e/api",
        "..\\api",
        ".\t./api",
    ],
)
def test_page_path_leaving_the_page_refused(
    pages: ProviderPageManager, user: User, caplog: pytest.LogCaptureFixture, path: str
) -> None:
    """A page path with a scheme, an absolute path or a step up is not opened."""
    with user_auth_context(user), caplog.at_level(logging.WARNING):
        result = ConfigActionResult(open_page=path)
        pages.prepare_action_result(_provider(), result, Scope.LIBRARY_READ)

    assert result.open_page is None
    assert "answered a page that can not be opened" in caplog.text


@pytest.mark.parametrize("path", ["", "stats", "a/b.c/", "stats?from=../x#..", "..x/y"])
def test_page_path_inside_the_page_accepted(
    pages: ProviderPageManager, user: User, path: str
) -> None:
    """A relative path inside the page is appended to the page's address."""
    with user_auth_context(user):
        result = ConfigActionResult(open_page=path)
        pages.prepare_action_result(_provider(), result, Scope.LIBRARY_READ)

    assert result.open_page is not None
    assert result.open_page.startswith("provider_page/")
    assert result.open_page.split("/", 2)[2] == path


def test_page_needs_a_user(pages: ProviderPageManager) -> None:
    """Without a calling user there is no one to bind the page to, so it is not opened."""
    result = ConfigActionResult(open_page="stats")
    pages.prepare_action_result(_provider(), result, Scope.LIBRARY_READ)

    assert result.open_page is None


@pytest.mark.parametrize(
    ("tier", "url", "kept"),
    [
        (ProviderTier.COMMUNITY, "https://example.org/connect", True),
        (ProviderTier.COMMUNITY, "http://example.org/connect", False),
        (ProviderTier.COMMUNITY, "ftp://example.org/connect", False),
        (ProviderTier.CORE, "http://192.168.1.5:8095/mcp/connect", True),
    ],
)
def test_open_url_https_only_for_community_providers(
    pages: ProviderPageManager,
    caplog: pytest.LogCaptureFixture,
    tier: ProviderTier,
    url: str,
    kept: bool,
) -> None:
    """A community provider's url is only opened over https; a built-in one keeps http."""
    with caplog.at_level(logging.WARNING):
        result = ConfigActionResult(open_url=url)
        pages.prepare_action_result(_provider(tier), result, Scope.LIBRARY_READ)

    assert result.open_url == (url if kept else None)
    assert ("not https" in caplog.text) is not kept


def _provider(tier: ProviderTier = ProviderTier.COMMUNITY) -> Any:
    """Return a stand-in provider instance of the given tier."""
    return SimpleNamespace(instance_id="page--1", manifest=SimpleNamespace(tier=tier))


def _assert_page_headers(response: ClientResponse) -> None:
    """Assert the headers the server forces on every response of a provider page."""
    assert PAGE_CSP in response.headers.getall("Content-Security-Policy")
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Access-Control-Allow-Origin"] == "*"
    assert "Set-Cookie" not in response.headers


def _answer(**answer: Any) -> None:
    """Set what the actions of the page provider answer."""
    module = sys.modules[f"{providers_package.__name__}.{PAGE}"]
    module.ANSWER.clear()
    module.ANSWER.update(answer)


async def _register(mass: MusicAssistant, location: Path) -> str:
    """
    Register the community test providers and set up the page provider.

    Returns the instance id of the page provider.

    :param mass: The server.
    :param location: The folder of community providers to write them in.
    """
    for domain, code in ((PAGE, PAGE_CODE), (PLAIN, PLAIN_CODE)):
        provider_dir = location / domain
        provider_dir.mkdir()
        manifest = {
            "type": "plugin",
            "domain": domain,
            "name": domain,
            "description": "test provider",
            "codeowners": [],
            "menu_actions": [OPEN],
        }
        (provider_dir / "manifest.json").write_text(json.dumps(manifest))
        (provider_dir / "__init__.py").write_text(code)
    mass.add_provider_location(str(location))
    for domain in (PAGE, PLAIN):
        await mass.register_provider_manifest(str(location / domain), ProviderTier.COMMUNITY)
    return await _setup(mass, PAGE)


async def _setup(mass: MusicAssistant, domain: str) -> str:
    """
    Set up an instance of a provider and return its instance id once it is loaded.

    :param mass: The server.
    :param domain: The domain of the provider.
    """
    step = await mass.config.setup_provider(domain)
    assert step.result is not None
    instance_id: str = step.result["instance_id"]
    provider = mass.get_provider(instance_id)
    assert provider is not None
    await provider.initialized.wait()
    return instance_id


async def _user(mass: MusicAssistant, role: UserRole) -> tuple[User, str]:
    """
    Create a user with the given role and return it with a token of it.

    :param mass: The server to create the user on.
    :param role: The role of the user.
    """
    user = await mass.webserver.auth.create_user(username=f"user_{role}", role=role)
    return user, await mass.webserver.auth.create_token(user, "test")


async def _open(mass: MusicAssistant, token: str, instance_id: str) -> str:
    """
    Open the page through the menu action and return its address.

    :param mass: The server.
    :param token: The token of the calling user.
    :param instance_id: The provider instance.
    """
    result = await _api(
        mass,
        token,
        "providers/invoke_menu_action",
        instance_id=instance_id,
        action_id=OPEN["id"],
        uri=ALBUM_URI,
    )
    address: str = result["open_page"]
    return address


async def _api(mass: MusicAssistant, token: str, command: str, **args: Any) -> Any:
    """
    Call an API command over HTTP and return its result.

    :param mass: The server.
    :param token: The token of the calling user.
    :param command: The command.
    :param args: Its arguments.
    """
    async with mass.http_session_no_ssl.post(
        f"{mass.webserver.internal_base_url}/api",
        json={"message_id": "1", "command": command, "args": args},
        headers={"Authorization": f"Bearer {token}"},
    ) as response:
        assert response.status == 200, await response.text()
        return await response.json()


def _get(mass: MusicAssistant, address: str) -> Any:
    """
    Request a page address the way a browser tab opens it, without any credentials.

    :param mass: The server.
    :param address: The page address the server issued.
    """
    return mass.http_session_no_ssl.get(f"{mass.webserver.internal_base_url}/{address}")
