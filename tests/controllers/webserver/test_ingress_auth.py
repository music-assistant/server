"""Tests for signing in a Home Assistant user, through Ingress or the Home Assistant login."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import contextmanager
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from music_assistant_models.api import CommandMessage
from music_assistant_models.auth import AuthProviderType, Scope, User, UserRole

from music_assistant.constants import (
    CONF_AUTH_ALLOW_SELF_REGISTRATION,
    HASSIO_SUPERVISOR_IP,
    HOMEASSISTANT_SYSTEM_USER,
    INGRESS_SERVER_PORT,
)
from music_assistant.controllers.webserver import websocket_client
from music_assistant.controllers.webserver.auth import AuthenticationManager
from music_assistant.controllers.webserver.controller import WebserverController
from music_assistant.controllers.webserver.helpers import auth_middleware, auth_providers
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    get_authenticated_user,
    set_current_user,
)
from music_assistant.controllers.webserver.helpers.auth_providers import (
    AuthResult,
    HomeAssistantOAuthProvider,
    HomeAssistantProviderConfig,
)
from music_assistant.controllers.webserver.websocket_client import WebsocketClientHandler

if TYPE_CHECKING:
    from collections.abc import Iterator

    from music_assistant.mass import MusicAssistant

INGRESS_IP = "172.30.32.1"


@pytest.fixture
async def auth_manager(mass_minimal: MusicAssistant) -> AsyncGenerator[AuthenticationManager]:
    """
    Provide the authentication manager of a minimal server, with its database set up.

    :param mass_minimal: The minimal server to set up the authentication manager on.
    """
    webserver = WebserverController(mass_minimal)
    mass_minimal.webserver = webserver
    webserver.config = await mass_minimal.config.get_core_config("webserver")
    # creating the first user migrates playlog rows through the music controller,
    # which the minimal server does not run
    mass_minimal.music = MagicMock()
    mass_minimal.music.database.execute = AsyncMock()
    mass_minimal.music.database.commit = AsyncMock()
    await webserver.auth.setup()
    try:
        yield webserver.auth
    finally:
        await webserver.auth.close()


def _ready_hass_provider(
    mass: MusicAssistant,
    ha_user_id: str,
    *,
    admin: bool,
    details: tuple[str | None, str | None, str | None] = (None, None, None),
) -> MagicMock:
    """
    Return a mock Home Assistant provider, marked ready, that knows the given user.

    :param mass: The server whose hass provider ready event is set.
    :param ha_user_id: The Home Assistant user id the provider knows about.
    :param admin: Whether that Home Assistant account is an admin.
    :param details: The (username, display_name, avatar_url) the provider returns for the user.
    """
    hass_provider = MagicMock()
    hass_provider.available = True
    hass_provider.get_user_details = AsyncMock(return_value=details)
    group_ids = ["system-admin"] if admin else ["system-users"]
    hass_provider.hass.send_command = AsyncMock(
        return_value=[{"id": ha_user_id, "group_ids": group_ids}]
    )
    mass.get_provider_ready_event("hass").set()
    return hass_provider


@contextmanager
def _ingress_request(
    mass: MusicAssistant,
    headers: dict[str, str],
    *,
    from_ingress: bool = True,
    hass_provider: MagicMock | None = None,
) -> Iterator[web.Request]:
    """
    Build a request with the given headers and patch the ingress checks for the call.

    :param mass: The minimal server serving the request.
    :param headers: The request headers, such as the ingress user headers.
    :param from_ingress: Whether the request is treated as coming from Home Assistant Ingress.
    :param hass_provider: The mock Home Assistant provider get_provider resolves to, if any.
    """
    app = web.Application()
    app["mass"] = mass
    request = make_mocked_request("GET", "/", headers=headers, app=app)
    with (
        patch.object(auth_middleware, "is_request_from_ingress", return_value=from_ingress),
        patch.object(websocket_client, "is_request_from_ingress", return_value=from_ingress),
        patch.object(mass, "get_provider", return_value=hass_provider),
    ):
        yield request


def _socket_request(mass: MusicAssistant, headers: dict[str, str], peer_ip: str) -> web.Request:
    """
    Build a request received on the ingress site from the given peer address.

    :param mass: The minimal server serving the request.
    :param headers: The request headers, such as the ingress user headers.
    :param peer_ip: The address of the peer that opened the connection.
    """
    app = web.Application()
    app["mass"] = mass
    app["ingress_site"] = (INGRESS_IP, INGRESS_SERVER_PORT)
    extra_info = {"sockname": (INGRESS_IP, INGRESS_SERVER_PORT), "peername": (peer_ip, 54321)}
    transport = MagicMock()
    transport.get_extra_info.side_effect = extra_info.get
    return make_mocked_request("GET", "/", headers=headers, app=app, transport=transport)


async def _create_user(
    auth_manager: AuthenticationManager,
    username: str,
    *,
    ha_user_id: str | None = None,
    disabled: bool = False,
) -> User:
    """
    Create a user with display name "Old name", optionally linked to HA and disabled.

    :param auth_manager: The authentication manager to create the user with.
    :param username: The username of the user.
    :param ha_user_id: The Home Assistant user id to link the user to, if any.
    :param disabled: Whether to disable the user's account (as an admin would).
    """
    user = await auth_manager.create_user(username=username, display_name="Old name")
    if ha_user_id:
        await auth_manager.link_user_to_provider(user, AuthProviderType.HOME_ASSISTANT, ha_user_id)
    if disabled:
        admin = await auth_manager.create_user(username=f"{username}_admin", role=UserRole.ADMIN)
        set_current_user(admin)
        await auth_manager.disable_user(user.user_id)
    return user


async def _get_ha_link(
    auth_manager: AuthenticationManager, ha_user_id: str
) -> dict[str, object] | None:
    """
    Return the stored link of the given Home Assistant user id, if any.

    :param auth_manager: The authentication manager whose database holds the links.
    :param ha_user_id: The Home Assistant user id to look up.
    """
    row = await auth_manager.database.get_row(
        "user_auth_providers",
        {"provider_type": AuthProviderType.HOME_ASSISTANT.value, "provider_user_id": ha_user_id},
    )
    return dict(row) if row else None


def _oauth_provider(mass: MusicAssistant) -> HomeAssistantOAuthProvider:
    """
    Return a Home Assistant OAuth login provider for the given server.

    :param mass: The server the login provider signs users in to.
    """
    ha_config: HomeAssistantProviderConfig = {"ha_url": "http://ha.local:8123"}
    return HomeAssistantOAuthProvider(mass, "homeassistant", ha_config)


async def _ha_login_callback(
    mass: MusicAssistant,
    ha_user_id: str,
    details: tuple[str | None, str | None, str | None],
) -> AuthResult:
    """
    Complete a Home Assistant login for the given HA user and return its result.

    :param mass: The server the user signs in to.
    :param ha_user_id: The Home Assistant user id the login resolves to.
    :param details: The (username, display_name, avatar_url) Home Assistant returns for the user.
    """
    provider = _oauth_provider(mass)
    provider._oauth_sessions["login_state"] = None
    hass_provider = _ready_hass_provider(mass, ha_user_id, admin=False, details=details)
    with (
        patch.object(mass, "get_provider", return_value=hass_provider),
        patch.object(
            auth_providers, "get_token", AsyncMock(return_value={"access_token": "ha_token"})
        ),
        patch.object(
            provider, "_fetch_ha_user_id_via_websocket", AsyncMock(return_value=ha_user_id)
        ),
    ):
        return await provider.handle_oauth_callback(
            "ha_code", "login_state", "http://ma.local:8095/auth/callback"
        )


@pytest.mark.parametrize(
    ("admin", "expected_role"),
    [(True, UserRole.ADMIN), (False, UserRole.USER)],
    ids=["admin", "non_admin"],
)
async def test_a_new_ingress_user_gets_the_role_of_its_home_assistant_account(
    auth_manager: AuthenticationManager, admin: bool, expected_role: str
) -> None:
    """
    A user signing in through Ingress for the first time is created with its HA role.

    :param admin: Whether the Home Assistant account is an admin.
    :param expected_role: The role the created user is expected to hold.
    """
    mass = auth_manager.mass
    hass_provider = _ready_hass_provider(
        mass, "ha_alice", admin=admin, details=("alice", None, None)
    )
    headers = {"X-Remote-User-ID": "ha_alice", "X-Remote-User-Name": "Alice"}

    with _ingress_request(mass, headers, hass_provider=hass_provider) as request:
        user = await get_authenticated_user(request)

    assert user is not None
    assert user.role == expected_role
    assert user.username == "alice"
    linked = await auth_manager.get_user_by_provider_link(
        AuthProviderType.HOME_ASSISTANT, "ha_alice"
    )
    assert linked is not None
    assert linked.user_id == user.user_id


@pytest.mark.parametrize("custom", [False, True], ids=["builtin_role", "custom_role"])
async def test_ingress_keeps_the_role_of_an_already_linked_user(
    auth_manager: AuthenticationManager, custom: bool
) -> None:
    """
    A user already linked to Home Assistant keeps its stored role on an Ingress sign-in.

    :param custom: Whether the linked user holds a custom role rather than a builtin one.
    """
    mass = auth_manager.mass
    role = (
        (await auth_manager.create_role("Kids", [Scope.LIBRARY_WRITE])).role_id
        if custom
        else UserRole.GUEST
    )
    existing = await auth_manager.create_user(username="alice", role=role)
    await auth_manager.link_user_to_provider(existing, AuthProviderType.HOME_ASSISTANT, "ha_alice")
    # the Home Assistant account is an admin and refreshes the display name, yet the stored
    # role must win over the admin status even as the sign-in does update the user
    hass_provider = _ready_hass_provider(
        mass, "ha_alice", admin=True, details=(None, "Alice from HA", None)
    )
    headers = {"X-Remote-User-ID": "ha_alice", "X-Remote-User-Name": "Alice"}

    with _ingress_request(mass, headers, hass_provider=hass_provider) as request:
        user = await get_authenticated_user(request)

    assert user is not None
    assert user.user_id == existing.user_id
    assert user.role == role
    # the display name is refreshed from Home Assistant, so the user is genuinely updated
    assert user.display_name == "Alice from HA"
    # the Home Assistant admin status is never consulted for an already-linked user
    hass_provider.hass.send_command.assert_not_called()


@pytest.mark.parametrize("custom", [False, True], ids=["builtin_role", "custom_role"])
async def test_ingress_links_a_username_match_keeping_its_role(
    auth_manager: AuthenticationManager, custom: bool
) -> None:
    """
    An existing user matched by username is linked to Home Assistant, keeping its stored role.

    :param custom: Whether the matched user holds a custom role rather than a builtin one.
    """
    mass = auth_manager.mass
    role = (
        (await auth_manager.create_role("Kids", [Scope.LIBRARY_WRITE])).role_id
        if custom
        else UserRole.USER
    )
    existing = await auth_manager.create_user(username="bob", role=role)
    # the Home Assistant account is an admin, yet a username match must not re-derive the role
    hass_provider = _ready_hass_provider(mass, "ha_bob", admin=True, details=("bob", None, None))
    headers = {"X-Remote-User-ID": "ha_bob", "X-Remote-User-Name": "bob"}

    with _ingress_request(mass, headers, hass_provider=hass_provider) as request:
        user = await get_authenticated_user(request)

    assert user is not None
    assert user.user_id == existing.user_id
    assert user.role == role
    linked = await auth_manager.get_user_by_provider_link(AuthProviderType.HOME_ASSISTANT, "ha_bob")
    assert linked is not None
    assert linked.user_id == existing.user_id
    # the Home Assistant admin status is never consulted for a username match
    hass_provider.hass.send_command.assert_not_called()


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-Remote-User-ID": "ha_alice"},
        {"X-Remote-User-Name": "Alice"},
    ],
    ids=["no_headers", "no_username", "no_user_id"],
)
async def test_ingress_without_both_user_headers_authenticates_no_user(
    auth_manager: AuthenticationManager, headers: dict[str, str]
) -> None:
    """
    An Ingress request missing either user header authenticates nobody.

    :param headers: The incomplete set of ingress headers on the request.
    """
    with _ingress_request(auth_manager.mass, headers) as request:
        user = await get_authenticated_user(request)

    assert user is None


async def test_a_non_ingress_request_ignores_the_ingress_headers(
    auth_manager: AuthenticationManager,
) -> None:
    """A request not coming from Ingress is never authenticated from the HA user headers."""
    headers = {"X-Remote-User-ID": "ha_alice", "X-Remote-User-Name": "Alice"}

    with _ingress_request(auth_manager.mass, headers, from_ingress=False) as request:
        user = await get_authenticated_user(request)

    assert user is None
    # the ingress headers neither created nor linked a user
    linked = await auth_manager.get_user_by_provider_link(
        AuthProviderType.HOME_ASSISTANT, "ha_alice"
    )
    assert linked is None


async def test_ingress_refuses_a_disabled_linked_user_under_another_username(
    auth_manager: AuthenticationManager,
) -> None:
    """
    A disabled user linked to the HA account is refused, even when its username differs.

    No new account is created for the Home Assistant user and the disabled user is untouched.
    """
    mass = auth_manager.mass
    disabled = await _create_user(auth_manager, "alice_old", ha_user_id="ha_alice", disabled=True)
    user_count = len(await auth_manager.list_users())
    hass_provider = _ready_hass_provider(
        mass, "ha_alice", admin=False, details=("alice", "Alice from HA", None)
    )
    headers = {"X-Remote-User-ID": "ha_alice", "X-Remote-User-Name": "alice"}

    with _ingress_request(mass, headers, hass_provider=hass_provider) as request:
        user = await get_authenticated_user(request)

    assert user is None
    assert len(await auth_manager.list_users()) == user_count
    row = await auth_manager.database.get_row("users", {"user_id": disabled.user_id})
    assert row is not None
    assert not row["enabled"]
    assert row["display_name"] == "Old name"


async def test_ingress_refuses_a_username_match_with_a_disabled_user(
    auth_manager: AuthenticationManager,
) -> None:
    """An unlinked HA user whose username matches a disabled user is refused, and not linked."""
    mass = auth_manager.mass
    await _create_user(auth_manager, "bob", disabled=True)
    hass_provider = _ready_hass_provider(mass, "ha_bob", admin=False, details=("bob", None, None))
    headers = {"X-Remote-User-ID": "ha_bob", "X-Remote-User-Name": "Bob"}

    with _ingress_request(mass, headers, hass_provider=hass_provider) as request:
        user = await get_authenticated_user(request)

    assert user is None
    assert await _get_ha_link(auth_manager, "ha_bob") is None


async def test_ingress_site_request_from_the_supervisor_authenticates_the_linked_user(
    auth_manager: AuthenticationManager,
) -> None:
    """A request on the ingress site opened by the Supervisor signs in the linked HA user."""
    created = await _create_user(auth_manager, "alice", ha_user_id="ha_alice")
    headers = {"X-Remote-User-ID": "ha_alice", "X-Remote-User-Name": "alice"}

    user = await get_authenticated_user(
        _socket_request(auth_manager.mass, headers, HASSIO_SUPERVISOR_IP)
    )

    assert user is not None
    assert user.user_id == created.user_id


@pytest.mark.parametrize("peer_ip", ["127.0.0.1", "172.30.32.1"])
async def test_ingress_site_request_from_another_peer_authenticates_no_user(
    auth_manager: AuthenticationManager, peer_ip: str
) -> None:
    """
    A request on the ingress site from a peer other than the Supervisor ignores the HA headers.

    :param peer_ip: The address of the peer that opened the connection.
    """
    await _create_user(auth_manager, "alice", ha_user_id="ha_alice")
    headers = {"X-Remote-User-ID": "ha_alice", "X-Remote-User-Name": "alice"}

    user = await get_authenticated_user(_socket_request(auth_manager.mass, headers, peer_ip))

    assert user is None


async def test_ingress_does_not_link_a_username_match_unconfirmed_by_home_assistant(
    auth_manager: AuthenticationManager,
) -> None:
    """An HA user id Home Assistant can not confirm neither signs in, links nor creates a user."""
    mass = auth_manager.mass
    await auth_manager.create_user(username="bob", role=UserRole.ADMIN)
    user_count = len(await auth_manager.list_users())
    hass_provider = _ready_hass_provider(mass, "ha_unknown", admin=True)
    mass.get_provider_ready_event("hass").set()
    headers = {"X-Remote-User-ID": "ha_unknown", "X-Remote-User-Name": "bob"}

    with _ingress_request(mass, headers, hass_provider=hass_provider) as request:
        user = await get_authenticated_user(request)

    assert user is None
    assert await _get_ha_link(auth_manager, "ha_unknown") is None
    assert len(await auth_manager.list_users()) == user_count


async def test_ingress_links_the_username_home_assistant_confirms(
    auth_manager: AuthenticationManager,
) -> None:
    """An unlinked user is matched by the username Home Assistant returns, not the header one."""
    mass = auth_manager.mass
    existing = await auth_manager.create_user(username="bob")
    await auth_manager.create_user(username="mallory")
    hass_provider = _ready_hass_provider(
        mass, "ha_bob", admin=False, details=("bob", "Bob from HA", None)
    )
    headers = {"X-Remote-User-ID": "ha_bob", "X-Remote-User-Name": "mallory"}

    with _ingress_request(mass, headers, hass_provider=hass_provider) as request:
        user = await get_authenticated_user(request)

    assert user is not None
    assert user.user_id == existing.user_id
    assert user.display_name == "Bob from HA"
    link = await _get_ha_link(auth_manager, "ha_bob")
    assert link is not None
    assert link["user_id"] == existing.user_id


@pytest.mark.parametrize("disabled", [False, True], ids=["enabled", "disabled"])
async def test_ingress_websocket_signs_in_the_linked_user_unless_disabled(
    auth_manager: AuthenticationManager, disabled: bool
) -> None:
    """
    An Ingress websocket connection is signed in as the linked user, unless it is disabled.

    A refused connection receives no events until it authenticates with a token.

    :param disabled: Whether the linked user's account is disabled.
    """
    mass = auth_manager.mass
    linked = await _create_user(auth_manager, "alice", ha_user_id="ha_alice", disabled=disabled)
    hass_provider = _ready_hass_provider(mass, "ha_alice", admin=False)
    headers = {"X-Remote-User-ID": "ha_alice", "X-Remote-User-Name": "alice"}

    with _ingress_request(mass, headers, hass_provider=hass_provider) as request:
        client = WebsocketClientHandler(auth_manager.webserver, request)
        await client._handle_ingress_auth()

    signed_in = client._authenticated_user
    assert (signed_in.user_id if signed_in else None) == (None if disabled else linked.user_id)
    assert (client._events_unsub_callback is None) == disabled


async def test_ingress_websocket_without_user_headers_subscribes_after_token_auth(
    auth_manager: AuthenticationManager,
) -> None:
    """An Ingress websocket connection without HA user headers gets events only after token auth."""
    token = await auth_manager.get_homeassistant_system_user_token()

    with _ingress_request(auth_manager.mass, {}) as request:
        client = WebsocketClientHandler(auth_manager.webserver, request)
        await client._handle_ingress_auth()
        signed_in_before_auth = client._authenticated_user
        subscribed_before_auth = client._events_unsub_callback is not None

        with patch.object(client, "_send_message", AsyncMock()):
            await client._handle_auth_command(
                CommandMessage(message_id="1", command="auth", args={"token": token})
            )

    assert signed_in_before_auth is None
    assert not subscribed_before_auth
    assert client._authenticated_user is not None
    assert client._authenticated_user.username == HOMEASSISTANT_SYSTEM_USER
    assert client._events_unsub_callback is not None


async def test_ingress_websocket_is_closed_when_the_sign_in_fails(
    auth_manager: AuthenticationManager,
) -> None:
    """An Ingress websocket connection whose sign-in raises is closed and cleaned up."""
    mass = auth_manager.mass
    # Home Assistant confirms the username, but the role lookup does not know the id and raises
    hass_provider = _ready_hass_provider(
        mass, "ha_someone_else", admin=False, details=("alice", None, None)
    )
    headers = {"X-Remote-User-ID": "ha_alice", "X-Remote-User-Name": "alice"}

    with (
        _ingress_request(mass, headers, hass_provider=hass_provider) as request,
        patch.object(mass, "dashboard", MagicMock(), create=True) as dashboard,
    ):
        client = WebsocketClientHandler(auth_manager.webserver, request)
        with (
            patch.object(client.wsock, "prepare", AsyncMock()),
            patch.object(client.wsock, "close", AsyncMock()) as close,
            patch.object(client.wsock, "receive", AsyncMock(side_effect=RuntimeError)) as receive,
            patch.object(client, "_send_message", AsyncMock()),
        ):
            await client.handle_client()

    assert client._authenticated_user is None
    assert client._writer_task is not None
    assert client._writer_task.done()
    receive.assert_not_awaited()
    close.assert_awaited_once()
    dashboard.handle_client_disconnected.assert_called_once_with(client.client_id)


async def test_ha_login_resolves_a_disabled_linked_user_under_another_username(
    auth_manager: AuthenticationManager,
) -> None:
    """
    The HA login resolves to the disabled linked user, even when its username differs.

    No new account is created for the Home Assistant user and the disabled user is untouched.
    """
    mass = auth_manager.mass
    disabled = await _create_user(auth_manager, "alice_old", ha_user_id="ha_alice", disabled=True)
    user_count = len(await auth_manager.list_users())
    hass_provider = _ready_hass_provider(mass, "ha_alice", admin=False)

    with patch.object(mass, "get_provider", return_value=hass_provider):
        user = await _oauth_provider(mass)._get_or_create_user("alice", "Alice from HA", "ha_alice")

    assert user is not None
    assert user.user_id == disabled.user_id
    assert not user.enabled
    assert user.display_name == "Old name"
    assert len(await auth_manager.list_users()) == user_count


async def test_ha_login_does_not_link_a_username_match_with_a_disabled_user(
    auth_manager: AuthenticationManager,
) -> None:
    """An unlinked HA user whose username matches a disabled user resolves to it, unlinked."""
    disabled = await _create_user(auth_manager, "bob", disabled=True)

    user = await _oauth_provider(auth_manager.mass)._get_or_create_user("Bob", None, "ha_bob")

    assert user is not None
    assert user.user_id == disabled.user_id
    assert not user.enabled
    assert await _get_ha_link(auth_manager, "ha_bob") is None


async def test_ha_login_links_a_username_match_and_refreshes_its_details(
    auth_manager: AuthenticationManager,
) -> None:
    """An existing user matched by username is linked to HA and gets the HA display name."""
    existing = await _create_user(auth_manager, "bob")

    user = await _oauth_provider(auth_manager.mass)._get_or_create_user(
        "Bob", "Bob from HA", "ha_bob"
    )

    assert user is not None
    assert user.user_id == existing.user_id
    assert user.display_name == "Bob from HA"
    link = await _get_ha_link(auth_manager, "ha_bob")
    assert link is not None
    assert link["user_id"] == existing.user_id


@pytest.mark.parametrize("display_name", [None, "Alice from HA"], ids=["no_details", "details"])
async def test_ha_login_callback_refuses_a_disabled_user(
    auth_manager: AuthenticationManager, display_name: str | None
) -> None:
    """
    The HA login callback refuses to sign in a disabled user.

    :param display_name: The display name Home Assistant returns for the user, if any.
    """
    await _create_user(auth_manager, "alice", ha_user_id="ha_alice", disabled=True)

    result = await _ha_login_callback(auth_manager.mass, "ha_alice", ("alice", display_name, None))

    assert result == AuthResult(success=False, error="User account is disabled")


async def test_ha_login_callback_signs_in_an_enabled_linked_user(
    auth_manager: AuthenticationManager,
) -> None:
    """The HA login callback signs in the linked user and refreshes its display name."""
    linked = await _create_user(auth_manager, "alice", ha_user_id="ha_alice")

    result = await _ha_login_callback(
        auth_manager.mass, "ha_alice", ("alice", "Alice from HA", None)
    )

    assert result.success
    assert result.user is not None
    assert result.user.user_id == linked.user_id
    assert result.user.display_name == "Alice from HA"


async def test_ha_login_callback_refuses_a_new_user_with_self_registration_off(
    auth_manager: AuthenticationManager,
) -> None:
    """With self-registration off, the HA login refuses an unknown HA user and creates nothing."""
    auth_manager.webserver.config.update({CONF_AUTH_ALLOW_SELF_REGISTRATION: False})
    user_count = len(await auth_manager.list_users())

    result = await _ha_login_callback(
        auth_manager.mass, "ha_carol", ("carol", "Carol from HA", None)
    )

    assert result == AuthResult(
        success=False, error="Self-registration is disabled. Please contact an administrator."
    )
    assert len(await auth_manager.list_users()) == user_count
    assert await _get_ha_link(auth_manager, "ha_carol") is None
