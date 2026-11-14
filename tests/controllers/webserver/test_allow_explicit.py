"""Tests for a member's allow_explicit setting, which only an admin may change."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.errors import InsufficientPermissions

from music_assistant.controllers.webserver.auth import DB_SCHEMA_VERSION, AuthenticationManager
from music_assistant.controllers.webserver.controller import WebserverController
from music_assistant.controllers.webserver.helpers.auth_middleware import set_current_user

if TYPE_CHECKING:
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
    mass_minimal.music = MagicMock()
    mass_minimal.music.playlists.release_user_playlists = AsyncMock()
    await webserver.auth.setup()
    try:
        yield webserver.auth
    finally:
        await webserver.auth.close()


async def _restart_on_schema(auth_manager: AuthenticationManager, version: int) -> None:
    """Mark the database as written by the given schema version and set it up again."""
    await auth_manager.database.update(
        "settings", {"key": "schema_version"}, {"value": str(version)}
    )
    await auth_manager.database.commit()
    await auth_manager.close()
    await auth_manager.setup()


async def _admin_and_member(auth_manager: AuthenticationManager) -> tuple[User, User]:
    """Create an admin and a member."""
    admin = await auth_manager.create_user(username="admin", role=UserRole.ADMIN)
    member = await auth_manager.create_user(username="kid", role=UserRole.USER)
    return admin, member


async def _stored(auth_manager: AuthenticationManager, user: User) -> User:
    """Return the user as the database now holds it."""
    stored = await auth_manager.get_user(user.user_id)
    assert stored is not None
    return stored


async def test_a_new_member_is_allowed_explicit_content(
    auth_manager: AuthenticationManager,
) -> None:
    """A new member gets explicit content, as before the setting existed."""
    _, member = await _admin_and_member(auth_manager)

    assert member.allow_explicit is True
    assert (await _stored(auth_manager, member)).allow_explicit is True


async def test_the_migration_adds_the_column_and_can_run_twice(
    auth_manager: AuthenticationManager,
) -> None:
    """A schema 5 database gets the column, existing members stay allowed, a rerun is safe."""
    _, member = await _admin_and_member(auth_manager)
    await auth_manager.database.execute("ALTER TABLE users DROP COLUMN allow_explicit")
    await auth_manager.database.commit()

    await _restart_on_schema(auth_manager, 5)
    # the column is there now, so a second run must not fail on it
    await _restart_on_schema(auth_manager, 5)

    assert (await _stored(auth_manager, member)).allow_explicit is True
    schema_row = await auth_manager.database.get_row("settings", {"key": "schema_version"})
    assert schema_row is not None
    assert schema_row["value"] == str(DB_SCHEMA_VERSION)


async def test_an_admin_restricts_and_allows_a_member(
    auth_manager: AuthenticationManager,
) -> None:
    """An admin switches the setting, which is stored, listed and switched back."""
    admin, member = await _admin_and_member(auth_manager)
    set_current_user(admin)

    restricted = await auth_manager.update_user_profile(
        user_id=member.user_id, allow_explicit=False
    )

    assert restricted.allow_explicit is False
    assert (await _stored(auth_manager, member)).allow_explicit is False
    listed = {user.user_id: user for user in await auth_manager.list_users()}
    assert listed[member.user_id].allow_explicit is False

    await auth_manager.update_user_profile(user_id=member.user_id, allow_explicit=True)
    assert (await _stored(auth_manager, member)).allow_explicit is True


async def test_a_member_can_not_change_their_own(auth_manager: AuthenticationManager) -> None:
    """Without the users.manage scope the setting can not be changed, not even one's own."""
    admin, member = await _admin_and_member(auth_manager)
    set_current_user(admin)
    await auth_manager.update_user_profile(user_id=member.user_id, allow_explicit=False)
    set_current_user(await _stored(auth_manager, member))

    with pytest.raises(InsufficientPermissions):
        await auth_manager.update_user_profile(allow_explicit=True)

    assert (await _stored(auth_manager, member)).allow_explicit is False


async def test_a_member_keeps_it_when_changing_their_profile(
    auth_manager: AuthenticationManager,
) -> None:
    """A member saving their own profile and preferences leaves the setting alone."""
    admin, member = await _admin_and_member(auth_manager)
    set_current_user(admin)
    await auth_manager.update_user_profile(user_id=member.user_id, allow_explicit=False)
    set_current_user(await _stored(auth_manager, member))

    updated = await auth_manager.update_user_profile(
        display_name="Kid", preferences={"allow_explicit": True}
    )

    assert updated.allow_explicit is False


async def test_an_admin_creates_a_restricted_member(auth_manager: AuthenticationManager) -> None:
    """A member created by an admin can start out restricted."""
    admin = await auth_manager.create_user(username="admin", role=UserRole.ADMIN)
    set_current_user(admin)

    created = await auth_manager.create_user_with_api(
        username="kid", password="password123", allow_explicit=False
    )

    assert created.allow_explicit is False
    assert (await _stored(auth_manager, created)).allow_explicit is False


async def test_open_sessions_get_the_new_setting(mass_minimal: MusicAssistant) -> None:
    """The live sessions of a member hold the new setting right away, others are untouched."""
    webserver = WebserverController(mass_minimal)
    member = User(user_id="kid", username="kid", role=UserRole.USER)
    other = User(user_id="other", username="other", role=UserRole.USER)
    webserver.clients = {
        MagicMock(authenticated_user=member),
        MagicMock(authenticated_user=other),
    }

    webserver.update_active_user_filters("kid", allow_explicit=False)

    assert member.allow_explicit is False
    assert other.allow_explicit is True
