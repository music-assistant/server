"""Tests for Home Assistant Ingress sign-in on the internal ingress site."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import contextmanager
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from music_assistant_models.auth import AuthProviderType, User, UserRole

from music_assistant.constants import (
    HASSIO_SUPERVISOR_IP,
    HOMEASSISTANT_SYSTEM_USER,
    INGRESS_SERVER_PORT,
)
from music_assistant.controllers.webserver import websocket_client
from music_assistant.controllers.webserver.auth import AuthenticationManager
from music_assistant.controllers.webserver.controller import WebserverController
from music_assistant.controllers.webserver.helpers import auth_middleware
from music_assistant.controllers.webserver.helpers.auth_middleware import get_authenticated_user

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


async def test_system_user_token_is_accepted_on_the_ingress_site_from_the_host(
    auth_manager: AuthenticationManager,
) -> None:
    """The HA integration connects from the host with the system user token, not headers."""
    token = await auth_manager.get_homeassistant_system_user_token()
    headers = {"Authorization": f"Bearer {token}"}

    user = await get_authenticated_user(_socket_request(auth_manager.mass, headers, INGRESS_IP))

    assert user is not None
    assert user.username == HOMEASSISTANT_SYSTEM_USER


async def test_ingress_site_request_from_the_supervisor_authenticates_the_linked_user(
    auth_manager: AuthenticationManager,
) -> None:
    """A request on the ingress site opened by the Supervisor signs in the linked HA user."""
    created = await _create_user(auth_manager, "alice", ha_user_id="ha_alice")
    # the linked user needs no HA lookup; mark the provider ready so none is awaited
    auth_manager.mass.get_provider_ready_event("hass").set()
    headers = {"X-Remote-User-ID": "ha_alice", "X-Remote-User-Name": "alice"}

    user = await get_authenticated_user(
        _socket_request(auth_manager.mass, headers, HASSIO_SUPERVISOR_IP)
    )

    assert user is not None
    assert user.user_id == created.user_id


@pytest.mark.parametrize("peer_ip", ["127.0.0.1", INGRESS_IP])
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
    hass_provider: MagicMock | None = None,
) -> Iterator[web.Request]:
    """
    Build a request with the given headers, treated as relayed by the Ingress proxy.

    :param mass: The minimal server serving the request.
    :param headers: The request headers, such as the ingress user headers.
    :param hass_provider: The mock Home Assistant provider get_provider resolves to, if any.
    """
    app = web.Application()
    app["mass"] = mass
    request = make_mocked_request("GET", "/", headers=headers, app=app)
    with (
        patch.object(auth_middleware, "is_request_from_ingress", return_value=True),
        patch.object(auth_middleware, "is_request_from_ingress_proxy", return_value=True),
        patch.object(websocket_client, "is_request_from_ingress", return_value=True),
        patch.object(websocket_client, "is_request_from_ingress_proxy", return_value=True),
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
    auth_manager: AuthenticationManager, username: str, *, ha_user_id: str | None = None
) -> User:
    """
    Create a user, optionally linked to the given Home Assistant user id.

    :param auth_manager: The authentication manager to create the user with.
    :param username: The username of the user.
    :param ha_user_id: The Home Assistant user id to link the user to, if any.
    """
    user = await auth_manager.create_user(username=username, display_name="Old name")
    if ha_user_id:
        await auth_manager.link_user_to_provider(user, AuthProviderType.HOME_ASSISTANT, ha_user_id)
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
