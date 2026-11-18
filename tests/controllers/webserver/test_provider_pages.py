"""Tests for the pages a provider serves, behind a short-lived address."""

from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from aiohttp import WSMsgType
from music_assistant_models.auth import User, UserRole
from music_assistant_models.enums import ProviderTier
from music_assistant_models.errors import (
    ActionUnavailable,
    AuthenticationRequired,
    InsufficientPermissions,
    ProviderUnavailableError,
)

from music_assistant import providers as providers_package
from music_assistant.controllers.webserver import provider_pages
from music_assistant.controllers.webserver.provider_pages import (
    MAX_PAGES_PER_USER,
    ORIGIN_WIDE_HEADERS,
    PAGE_IDLE_TIMEOUT,
    PAGE_MAX_AGE,
)
from music_assistant.helpers.util import purge_provider_modules
from music_assistant.mass import PROVIDERS_PATH

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from aiohttp import ClientResponse

    from music_assistant.mass import MusicAssistant

PAGE = "page_provider"
PLAIN = "page_less_provider"
BARE = "bare_provider"
PAGE_DECLARED = {"label": "Picture frame", "scope": "library.write", "icon": "mdi-image-frame"}
PAGE_CODE = """
import asyncio

from aiohttp import web

from music_assistant.controllers.webserver.helpers.auth_middleware import get_current_user
from music_assistant.models.plugin import PluginProvider


class PageProvider(PluginProvider):
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
            async for message in ws:
                await ws.send_str(f"{message.data} for {get_current_user().username}")
            return ws
        if request.method == "POST":
            return web.json_response({"posted": await request.json(), "user": user})
        return web.Response(
            text=f"{path} {request.query_string} for {user}",
            headers={
                "Set-Cookie": "session=secret",
                "Content-Security-Policy": "img-src 'none'; report-uri /r; report-to grp",
                "Content-Security-Policy-Report-Only": "default-src 'none'; report-uri /r",
                "Report-To": '{"group":"grp","endpoints":[{"url":"https://example.org/r"}]}',
                "Reporting-Endpoints": 'grp="https://example.org/r"',
                "NEL": '{"report_to":"grp","max_age":86400}',
                "Clear-Site-Data": '"cookies", "storage"',
                "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
                "Alt-Svc": 'h3=":443"; ma=86400',
                "Service-Worker-Allowed": "/",
                "Accept-CH": "Sec-CH-UA-Platform",
                "Critical-CH": "Sec-CH-UA-Platform",
            },
        )


async def setup(mass, manifest, config):
    return PageProvider(mass, manifest, config)
"""
PLAIN_CODE = """
from music_assistant.models.plugin import PluginProvider


async def setup(mass, manifest, config):
    return PluginProvider(mass, manifest, config)
"""
# the sandbox flags that would give the page the server's origin, a download or the app's tab
SANDBOX_ESCAPES = ("allow-same-origin", "allow-downloads", "allow-top-navigation")


@pytest.fixture
def location(tmp_path: Path) -> Iterator[Path]:
    """Return a folder of community providers; what a test imports from it is undone."""
    folder = tmp_path / "community_providers"
    folder.mkdir()
    original_path = list(providers_package.__path__)
    imported_before = set(sys.modules)
    yield folder
    providers_package.__path__[:] = original_path
    for domain in (PAGE, PLAIN, BARE):
        if f"{providers_package.__name__}.{domain}" not in imported_before:
            purge_provider_modules(domain)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Return a single-element list holding the monotonic time the page sessions read."""
    now = [1000.0]
    monkeypatch.setattr(provider_pages, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now


async def test_open_page_answers_a_fresh_address_for_its_user(
    mass: MusicAssistant, location: Path
) -> None:
    """The command answers a fresh address of the page's root, which serves the caller."""
    instance_id = await _register(mass, location)
    user, token = await _user(mass, UserRole.USER)

    address = await _open(mass, token, instance_id)
    second = await _open(mass, token, instance_id)

    assert address.startswith("provider_page/")
    assert address.endswith("/")
    assert second != address
    for each in (address, second):
        async with _get(mass, f"{each}stats?range=week") as response:
            assert response.status == 200
            assert await response.text() == f"stats range=week for {user.username}"


async def test_open_page_refusals(mass: MusicAssistant, location: Path) -> None:
    """The command refuses a missing or unavailable instance, no page, and a user without scope."""
    instance_id = await _register(mass, location)
    bare_id = await _setup(mass, BARE)
    _, token = await _user(mass, UserRole.USER)
    _, guest_token = await _user(mass, UserRole.GUEST)
    provider = mass.get_provider(instance_id)
    assert provider is not None

    assert await _api_error(mass, token, "unknown--1") == ProviderUnavailableError.error_code
    assert await _api_error(mass, token, bare_id) == ActionUnavailable.error_code
    assert await _api_error(mass, guest_token, instance_id) == InsufficientPermissions.error_code
    provider.available = False
    assert await _api_error(mass, token, instance_id) == ProviderUnavailableError.error_code
    provider.available = True
    # without a calling user there is no one to bind the address to
    with pytest.raises(AuthenticationRequired):
        mass.open_provider_page(instance_id)


async def test_headers_forced_on_every_response(mass: MusicAssistant, location: Path) -> None:
    """A normal, streamed or error response gets the sandbox headers and loses its cookies."""
    instance_id = await _register(mass, location)
    _, token = await _user(mass, UserRole.USER)
    address = await _open(mass, token, instance_id)

    for path, status in (("", 200), ("stream", 200), ("fail", 500)):
        async with _get(mass, f"{address}{path}") as response:
            assert response.status == status
            _assert_page_headers(response)


async def test_reporting_removed(mass: MusicAssistant, location: Path) -> None:
    """Nothing in a page's response can have the browser report the address elsewhere."""
    instance_id = await _register(mass, location)
    _, token = await _user(mass, UserRole.USER)

    async with _get(mass, await _open(mass, token, instance_id)) as response:
        policies = response.headers.getall("Content-Security-Policy")
        # the provider's own policy is kept, without its reporting directives
        assert policies[0] == "img-src 'none'"
        assert not any("report" in policy for policy in policies)
        for header in (
            "Content-Security-Policy-Report-Only",
            "Report-To",
            "Reporting-Endpoints",
            "NEL",
        ):
            assert header not in response.headers


async def test_origin_wide_headers_removed(mass: MusicAssistant, location: Path) -> None:
    """Nothing in a page's response can act on the server's host or origin beyond the sandbox."""
    instance_id = await _register(mass, location)
    _, token = await _user(mass, UserRole.USER)

    async with _get(mass, await _open(mass, token, instance_id)) as response:
        assert response.status == 200
        for header in ORIGIN_WIDE_HEADERS:
            assert header not in response.headers
    assert {
        "Set-Cookie",
        "Clear-Site-Data",
        "Strict-Transport-Security",
        "Alt-Svc",
        "Service-Worker-Allowed",
        "Accept-CH",
        "Critical-CH",
    } <= set(ORIGIN_WIDE_HEADERS)


async def test_page_posts_json_and_upgrades_to_websocket(
    mass: MusicAssistant, location: Path
) -> None:
    """A page can post JSON to its own sub-paths and open a WebSocket, as its user."""
    instance_id = await _register(mass, location)
    user, token = await _user(mass, UserRole.USER)
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


async def test_preflight_rechecks_too(mass: MusicAssistant, location: Path) -> None:
    """A preflight gets no permission once the user has lost the scope of the page."""
    instance_id = await _register(mass, location)
    user, token = await _user(mass, UserRole.USER)
    address = await _open(mass, token, instance_id)

    await mass.webserver.auth.database.update(
        "users", {"user_id": user.user_id}, {"role": UserRole.GUEST.value}
    )
    async with mass.http_session_no_ssl.options(
        f"{mass.webserver.internal_base_url}/{address}api",
        headers={"Origin": "null", "Access-Control-Request-Method": "POST"},
    ) as response:
        assert response.status == 403
        assert "Access-Control-Allow-Methods" not in response.headers


async def test_provider_without_page_handler(mass: MusicAssistant, location: Path) -> None:
    """A provider that declares a page but serves none answers not found, sandboxed."""
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


async def test_open_websocket_is_not_idle(
    mass: MusicAssistant, location: Path, clock: list[float]
) -> None:
    """A WebSocket still open keeps its address from lapsing for being idle."""
    instance_id = await _register(mass, location)
    _, token = await _user(mass, UserRole.USER)
    address = await _open(mass, token, instance_id)

    async with _ws(mass, address) as ws:
        clock[0] += PAGE_IDLE_TIMEOUT + 1
        await _open(mass, token, instance_id)  # drops the addresses that have lapsed
        await ws.send_str("still")
        assert (await ws.receive(timeout=10)).type == WSMsgType.TEXT
        async with _get(mass, address) as response:
            assert response.status == 200


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


async def test_websocket_closed_at_max_age(
    mass: MusicAssistant, location: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A WebSocket still open ends when its address reaches its maximum age."""
    monkeypatch.setattr(provider_pages, "PAGE_MAX_AGE", 0.5)
    instance_id = await _register(mass, location)
    _, token = await _user(mass, UserRole.USER)
    address = await _open(mass, token, instance_id)

    async with _ws(mass, address) as ws:
        await _assert_closed(ws)
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
async def test_addresses_and_websockets_dropped_with_the_user(
    mass: MusicAssistant, location: Path, withdraw: str
) -> None:
    """Disabling or deleting a user drops its addresses and ends its open WebSockets."""
    instance_id = await _register(mass, location)
    user, token = await _user(mass, UserRole.USER)
    address = await _open(mass, token, instance_id)
    _, admin_token = await _user(mass, UserRole.ADMIN)

    async with _ws(mass, address) as ws:
        await _api(mass, admin_token, f"auth/user/{withdraw}", user_id=user.user_id)
        await _assert_closed(ws)
    if withdraw == "disable":
        # an account enabled again does not get its old addresses back
        await _api(mass, admin_token, "auth/user/enable", user_id=user.user_id)
    async with _get(mass, address) as response:
        assert response.status == 410


async def test_role_change_ends_open_websockets(mass: MusicAssistant, location: Path) -> None:
    """A role change drops the user's addresses and ends the WebSockets open on them."""
    instance_id = await _register(mass, location)
    user, token = await _user(mass, UserRole.USER)
    address = await _open(mass, token, instance_id)
    _, admin_token = await _user(mass, UserRole.ADMIN)

    async with _ws(mass, address) as ws:
        await _api(mass, admin_token, "auth/user/update", user_id=user.user_id, role="guest")
        await _assert_closed(ws)
    async with _get(mass, address) as response:
        assert response.status == 410


async def test_rechecks_the_instance(mass: MusicAssistant, location: Path) -> None:
    """An address only serves while its instance is available."""
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


async def test_unload_drops_addresses_and_ends_websockets(
    mass: MusicAssistant, location: Path
) -> None:
    """Unloading the instance drops its addresses and ends the WebSockets open on them."""
    instance_id = await _register(mass, location)
    _, token = await _user(mass, UserRole.USER)
    address = await _open(mass, token, instance_id)

    async with _ws(mass, address) as ws:
        await mass.unload_provider(instance_id)
        await _assert_closed(ws)
    await mass.load_provider(instance_id)
    async with _get(mass, address) as response:
        assert response.status == 410


async def test_address_never_reaches_a_reloaded_instance(
    mass: MusicAssistant, location: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An address is bound to the loaded instance, not only to its id."""
    instance_id = await _register(mass, location)
    _, token = await _user(mass, UserRole.USER)
    address = await _open(mass, token, instance_id)
    # without the drop on unload, only the binding to the instance can refuse it
    monkeypatch.setattr(mass.webserver.provider_pages, "drop_instance", lambda _id: None)

    await mass.unload_provider(instance_id)
    await mass.load_provider(instance_id)
    provider = mass.get_provider(instance_id)
    assert provider is not None
    await provider.initialized.wait()

    async with _get(mass, address) as response:
        assert response.status == 410


async def test_demo_page(mass: MusicAssistant) -> None:
    """The demo plugin declares a page, which greets the user who opened it."""
    await mass.register_provider_manifest(
        os.path.join(PROVIDERS_PATH, "_demo_plugin_provider"), ProviderTier.CORE
    )
    instance_id = await _setup(mass, "_demo_plugin_provider")
    _, token = await _user(mass, UserRole.GUEST)

    async with _get(mass, await _open(mass, token, instance_id)) as response:
        page = await response.text()

    assert page == "<!doctype html><p>Hello user_guest, from the demo plugin.</p>"


def _assert_page_headers(response: ClientResponse) -> None:
    """Assert the headers the server forces on every response of a provider page."""
    policies = response.headers.getall("Content-Security-Policy")
    directives = [d.strip().split() for policy in policies for d in policy.split(";")]
    sandboxes = [directive for directive in directives if directive and directive[0] == "sandbox"]
    assert sandboxes, policies
    assert not any(
        flag.startswith(SANDBOX_ESCAPES) for sandbox in sandboxes for flag in sandbox[1:]
    ), policies
    assert ["frame-ancestors", "'none'"] in directives
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Access-Control-Allow-Origin"] == "*"
    assert "Set-Cookie" not in response.headers


async def _assert_closed(ws: Any) -> None:
    """Assert that the server ends a WebSocket of a page."""
    message = await ws.receive(timeout=10)
    assert message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR)


async def _register(mass: MusicAssistant, location: Path) -> str:
    """
    Register the community test providers and set up the page provider.

    Returns the instance id of the page provider.

    :param mass: The server.
    :param location: The folder of community providers to write them in.
    """
    for domain, code, page in (
        (PAGE, PAGE_CODE, PAGE_DECLARED),
        (PLAIN, PLAIN_CODE, PAGE_DECLARED),
        (BARE, PLAIN_CODE, None),
    ):
        provider_dir = location / domain
        provider_dir.mkdir()
        manifest: dict[str, Any] = {
            "type": "plugin",
            "domain": domain,
            "name": domain,
            "description": "test provider",
            "codeowners": [],
            "page": page,
        }
        (provider_dir / "manifest.json").write_text(json.dumps(manifest))
        (provider_dir / "__init__.py").write_text(code)
    mass.add_provider_location(str(location))
    for domain in (PAGE, PLAIN, BARE):
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
    Open the page of a provider instance as a user and return its address.

    :param mass: The server.
    :param token: The token of the calling user.
    :param instance_id: The provider instance.
    """
    address: str = await _api(mass, token, "providers/open_page", instance_id=instance_id)
    return address


async def _api_error(mass: MusicAssistant, token: str, instance_id: str) -> int:
    """
    Open the page of a provider instance as a user, expecting a refusal, and return its error code.

    :param mass: The server.
    :param token: The token of the calling user.
    :param instance_id: The provider instance.
    """
    async with mass.http_session_no_ssl.post(
        f"{mass.webserver.internal_base_url}/api",
        json={
            "message_id": "1",
            "command": "providers/open_page",
            "args": {"instance_id": instance_id},
        },
        headers={"Authorization": f"Bearer {token}"},
    ) as response:
        assert response.status != 200
        error_code: int = (await response.json())["error_code"]
        return error_code


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
    :param address: The page address the server issued, with an optional sub-path.
    """
    return mass.http_session_no_ssl.get(f"{mass.webserver.internal_base_url}/{address}")


def _ws(mass: MusicAssistant, address: str) -> Any:
    """
    Open the WebSocket of the test page at a page address.

    :param mass: The server.
    :param address: The page address the server issued.
    """
    return mass.http_session_no_ssl.ws_connect(f"{mass.webserver.internal_base_url}/{address}ws")
