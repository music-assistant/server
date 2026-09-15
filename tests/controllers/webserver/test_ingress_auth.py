"""Tests for authenticating a request coming from Home Assistant Ingress."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import contextmanager
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from music_assistant_models.auth import AuthProviderType, Scope, UserRole

from music_assistant.controllers.webserver.auth import AuthenticationManager
from music_assistant.controllers.webserver.controller import WebserverController
from music_assistant.controllers.webserver.helpers import auth_middleware
from music_assistant.controllers.webserver.helpers.auth_middleware import get_authenticated_user

if TYPE_CHECKING:
    from collections.abc import Iterator

    from music_assistant.mass import MusicAssistant


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
        patch.object(mass, "get_provider", return_value=hass_provider),
    ):
        yield request


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
    hass_provider = _ready_hass_provider(mass, "ha_alice", admin=admin)
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
