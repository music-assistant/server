"""Tests for the password policy applied wherever a built-in password is set."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.streams import StreamReader
from aiohttp.test_utils import make_mocked_request
from music_assistant_models.auth import UserRole
from music_assistant_models.errors import InvalidDataError

from music_assistant.controllers.webserver.controller import WebserverController
from music_assistant.controllers.webserver.helpers.auth_middleware import set_current_user
from music_assistant.controllers.webserver.helpers.auth_providers import (
    PASSWORD_MIN_LENGTH,
    validate_password,
)

if TYPE_CHECKING:
    from music_assistant.controllers.webserver.auth import AuthenticationManager
    from music_assistant.mass import MusicAssistant

TOO_SHORT = "x" * (PASSWORD_MIN_LENGTH - 1)
LONG_ENOUGH = "x" * PASSWORD_MIN_LENGTH
POLICY_ERROR = f"Password must be at least {PASSWORD_MIN_LENGTH} characters"


@pytest.fixture
async def webserver(mass_minimal: MusicAssistant) -> AsyncGenerator[WebserverController]:
    """
    Provide a webserver controller with its authentication set up and no users.

    :param mass_minimal: The minimal server to run the controller on.
    """
    # creating the first user migrates playlog rows through the music controller,
    # which the minimal server does not run
    mass_minimal.music = MagicMock()
    mass_minimal.music.database.execute = AsyncMock()
    mass_minimal.music.database.commit = AsyncMock()
    webserver = WebserverController(mass_minimal)
    mass_minimal.webserver = webserver
    webserver.config = await mass_minimal.config.get_core_config("webserver")
    await webserver.auth.setup()
    try:
        yield webserver
    finally:
        await webserver.auth.close()


@pytest.fixture
async def auth_manager(webserver: WebserverController) -> AuthenticationManager:
    """
    Provide the authentication manager with an admin as the calling user.

    :param webserver: The controller owning the authentication manager.
    """
    admin = await webserver.auth.create_user(username="admin", role=UserRole.ADMIN)
    set_current_user(admin)
    return webserver.auth


async def _post_setup(webserver: WebserverController, body: dict[str, Any]) -> web.Response:
    """
    Post the account details to the setup endpoint.

    :param webserver: The controller handling the request.
    :param body: The JSON body to post.
    """
    app = web.Application()
    app["mass"] = webserver.mass
    payload = StreamReader(MagicMock(), limit=2**16)
    payload.feed_data(json.dumps(body).encode())
    payload.feed_eof()
    request = make_mocked_request(
        "POST",
        "/setup",
        headers={"Content-Type": "application/json"},
        app=app,
        payload=payload,
    )
    return await webserver._handle_setup(request)


@pytest.mark.parametrize(
    ("password", "accepted"),
    [(None, False), ("", False), (TOO_SHORT, False), (LONG_ENOUGH, True)],
    ids=["none", "empty", "too_short", "minimum"],
)
def test_validate_password(password: str | None, accepted: bool) -> None:
    """A password is refused with the policy message unless it meets the minimum length."""
    if accepted:
        validate_password(password)
        return
    with pytest.raises(InvalidDataError, match=POLICY_ERROR):
        validate_password(password)


async def test_create_user_enforces_the_policy(auth_manager: AuthenticationManager) -> None:
    """An admin can only create a user with a password that meets the policy."""
    with pytest.raises(InvalidDataError, match=POLICY_ERROR):
        await auth_manager.create_user_with_api(username="shorty", password=TOO_SHORT)
    assert await auth_manager.get_user_by_username("shorty") is None

    user = await auth_manager.create_user_with_api(username="longer", password=LONG_ENOUGH)

    assert user.username == "longer"


async def test_profile_password_change_enforces_the_policy(
    auth_manager: AuthenticationManager,
) -> None:
    """A user can only change their own password to one that meets the policy."""
    user = await auth_manager.create_user_with_api(username="member", password=LONG_ENOUGH)
    set_current_user(user)
    builtin = auth_manager.login_providers["builtin"]

    with pytest.raises(InvalidDataError, match=POLICY_ERROR):
        await auth_manager.update_user_profile(password=TOO_SHORT)
    unchanged = await builtin.authenticate({"username": "member", "password": LONG_ENOUGH})
    assert unchanged.success

    new_password = "y" * PASSWORD_MIN_LENGTH
    await auth_manager.update_user_profile(password=new_password)

    accepted = await builtin.authenticate({"username": "member", "password": new_password})
    assert accepted.success


async def test_setup_refuses_a_too_short_password(webserver: WebserverController) -> None:
    """The first admin account is refused with the policy message when its password is short."""
    response = await _post_setup(webserver, {"username": "marcel", "password": TOO_SHORT})

    assert response.status == 400
    assert json.loads(response.text or "") == {"success": False, "error": POLICY_ERROR}
    assert not webserver.auth.has_users
