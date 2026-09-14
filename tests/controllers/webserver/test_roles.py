"""Tests for the builtin and custom user roles."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.auth import Role, Scope, User, UserRole
from music_assistant_models.config_entries import ProviderAccess
from music_assistant_models.enums import ProviderSharing
from music_assistant_models.errors import InvalidDataError

from music_assistant.controllers.webserver.auth import (
    DB_SCHEMA_VERSION,
    ROLE_NAME_MAX_LENGTH,
    AuthenticationManager,
)
from music_assistant.controllers.webserver.controller import WebserverController
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    ROLE_SCOPES,
    custom_role_scopes,
    has_scope,
    set_current_user,
)
from music_assistant.helpers.datetime import utc
from music_assistant.helpers.json import json_dumps, json_loads
from tests.common import set_music_source_access

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

GUEST_SCOPES = ROLE_SCOPES[UserRole.GUEST]
BUILTIN_ROLE_IDS = ["admin", "user", "guest", "service"]


@pytest.fixture
async def auth_manager(mass_minimal: MusicAssistant) -> AsyncGenerator[AuthenticationManager]:
    """
    Provide the authentication manager of a minimal server, with its database set up.

    :param mass_minimal: The minimal server to set up the authentication manager on.
    """
    webserver = WebserverController(mass_minimal)
    mass_minimal.webserver = webserver
    webserver.config = await mass_minimal.config.get_core_config("webserver")
    # deleting a user releases its playlists through the music controller, which the
    # minimal server does not run
    mass_minimal.music = MagicMock()
    mass_minimal.music.playlists.release_user_playlists = AsyncMock()
    await webserver.auth.setup()
    try:
        yield webserver.auth
    finally:
        await webserver.auth.close()


async def _sign_in_admin(auth_manager: AuthenticationManager) -> User:
    """Create an admin and make it the calling user."""
    admin = await auth_manager.create_user(username="admin", role=UserRole.ADMIN)
    set_current_user(admin)
    return admin


def _user(role: str, user_id: str = "user_1") -> User:
    """Return a user holding the given role, without storing it."""
    return User(user_id=user_id, username=user_id, role=role)


async def _store_role(auth_manager: AuthenticationManager, role_id: str, scopes: str) -> None:
    """Write a custom role row as is into the database, bypassing the rules of the API."""
    await auth_manager.database.insert(
        "roles",
        {
            "role_id": role_id,
            "name": role_id.title(),
            "scopes": scopes,
            "created_at": utc().isoformat(),
        },
    )


async def _restart(auth_manager: AuthenticationManager) -> None:
    """Close the authentication manager and set it up again on the same database."""
    await auth_manager.close()
    await auth_manager.setup()


def test_the_user_role_may_own_music_sources_and_the_service_role_may_not() -> None:
    """A user adds and manages its own music sources, the Home Assistant service account never."""
    assert has_scope(_user(UserRole.USER), Scope.CONFIG_PROVIDERS_OWN)
    assert not has_scope(_user(UserRole.USER), Scope.CONFIG_PROVIDERS_WRITE)
    assert not has_scope(_user(UserRole.SERVICE), Scope.CONFIG_PROVIDERS_OWN)
    assert not has_scope(_user(UserRole.GUEST), Scope.CONFIG_PROVIDERS_OWN)
    assert ROLE_SCOPES[UserRole.USER] - ROLE_SCOPES[UserRole.SERVICE] == {
        Scope.CONFIG_PROVIDERS_OWN
    }
    assert ROLE_SCOPES[UserRole.SERVICE] - ROLE_SCOPES[UserRole.USER] == {
        Scope.CONFIG_PLAYERS_WRITE,
        Scope.USERS_READ,
        Scope.USERS_IMPERSONATE,
    }


def test_every_user_may_list_the_roles_but_only_a_user_manager_may_change_them() -> None:
    """Every signed-in user needs the role names, changing a role takes users.manage."""
    assert getattr(AuthenticationManager.get_roles, "api_authenticated", None) is True
    assert getattr(AuthenticationManager.get_roles, "api_required_scope", "unset") is None
    for command in (
        AuthenticationManager.create_role,
        AuthenticationManager.update_role,
        AuthenticationManager.delete_role,
    ):
        assert getattr(command, "api_required_scope", None) is Scope.USERS_MANAGE


async def test_the_builtin_roles_are_listed_first_then_the_custom_roles_by_name(
    auth_manager: AuthenticationManager,
) -> None:
    """The builtin roles come first in a fixed order, the custom roles follow by name."""
    zeta = await auth_manager.create_role("zeta", [])
    alpha = await auth_manager.create_role("Alpha", [])

    roles = await auth_manager.get_roles()

    assert [role.role_id for role in roles] == [*BUILTIN_ROLE_IDS, alpha.role_id, zeta.role_id]
    assert [role.name for role in roles[:4]] == ["Administrator", "User", "Guest", "Service"]
    for role in roles[:4]:
        assert role.builtin
        assert role.scopes == sorted(ROLE_SCOPES[role.role_id])
    assert not alpha.builtin


@pytest.mark.parametrize("role_id", BUILTIN_ROLE_IDS)
async def test_a_builtin_role_can_not_be_changed_or_removed(
    auth_manager: AuthenticationManager, role_id: str
) -> None:
    """
    A builtin role is defined in code, so neither its name nor its scopes change.

    :param role_id: Id of the builtin role.
    """
    builtin_roles = await auth_manager.get_roles()

    with pytest.raises(InvalidDataError) as excinfo:
        await auth_manager.update_role(role_id, name="Renamed", scopes=[])
    assert excinfo.value.translation_key == "builtin_role_readonly"
    with pytest.raises(InvalidDataError) as excinfo:
        await auth_manager.delete_role(role_id)
    assert excinfo.value.translation_key == "builtin_role_readonly"

    assert await auth_manager.get_roles() == builtin_roles
    assert await auth_manager.database.get_count("roles") == 0


async def test_create_a_custom_role(auth_manager: AuthenticationManager) -> None:
    """A custom role gets a generated id and is stored with its scopes sorted."""
    admin = await _sign_in_admin(auth_manager)

    role = await auth_manager.create_role("  Kids  ", [Scope.CONFIG_PROVIDERS_OWN])
    other_role = await auth_manager.create_role("Teens", [])

    assert role.name == "Kids"
    assert not role.builtin
    assert role.scopes == sorted(
        GUEST_SCOPES | {Scope.CONFIG_PROVIDERS_OWN, Scope.CONFIG_PROVIDERS_READ}
    )
    # generated like a user id
    assert len(role.role_id) == len(admin.user_id)
    assert role.role_id != other_role.role_id
    row = await auth_manager.database.get_row("roles", {"role_id": role.role_id})
    assert row is not None
    assert row["name"] == "Kids"
    assert json_loads(row["scopes"]) == sorted(str(scope) for scope in role.scopes)
    kid = _user(role.role_id)
    assert has_scope(kid, Scope.CONFIG_PROVIDERS_OWN)
    assert not has_scope(kid, Scope.CONFIG_CORE_READ)


async def test_a_scope_change_applies_to_the_holders_of_the_role_right_away(
    auth_manager: AuthenticationManager,
) -> None:
    """The scopes of a custom role change for its holders the moment the role is updated."""
    role = await auth_manager.create_role("Kids", [Scope.LIBRARY_WRITE])
    kid = _user(role.role_id)

    updated_role = await auth_manager.update_role(
        role.role_id, name="Teens", scopes=[Scope.CONFIG_CORE_READ]
    )

    assert updated_role == Role(
        role_id=role.role_id,
        name="Teens",
        scopes=sorted(GUEST_SCOPES | {Scope.CONFIG_CORE_READ}),
    )
    assert (await auth_manager.get_roles())[-1] == updated_role
    assert has_scope(kid, Scope.CONFIG_CORE_READ)
    assert not has_scope(kid, Scope.LIBRARY_WRITE)


async def test_an_update_keeps_what_it_is_not_given(auth_manager: AuthenticationManager) -> None:
    """Updating only the name keeps the scopes of a role, and the other way around."""
    role = await auth_manager.create_role("Kids", [Scope.LIBRARY_WRITE])

    renamed_role = await auth_manager.update_role(role.role_id, name="Teens")
    assert renamed_role.scopes == role.scopes

    rescoped_role = await auth_manager.update_role(role.role_id, scopes=[])
    assert rescoped_role.name == "Teens"
    assert rescoped_role.scopes == sorted(GUEST_SCOPES)


async def test_delete_a_custom_role(auth_manager: AuthenticationManager) -> None:
    """A deleted custom role is gone, and so are the scopes it granted."""
    role = await auth_manager.create_role("Kids", [Scope.LIBRARY_WRITE])

    await auth_manager.delete_role(role.role_id)

    assert role.role_id not in {role.role_id for role in await auth_manager.get_roles()}
    assert await auth_manager.database.get_row("roles", {"role_id": role.role_id}) is None
    assert not has_scope(_user(role.role_id), Scope.LIBRARY_READ)
    with pytest.raises(InvalidDataError) as excinfo:
        await auth_manager.delete_role(role.role_id)
    assert excinfo.value.translation_key == "role_not_found"


async def test_a_role_can_not_be_deleted_while_a_user_holds_it(
    auth_manager: AuthenticationManager,
) -> None:
    """A role stays as long as a user holds it, even a user whose account is disabled."""
    await _sign_in_admin(auth_manager)
    role = await auth_manager.create_role("Kids", [])
    kid = await auth_manager.create_user(username="kid", role=role.role_id)
    await auth_manager.disable_user(kid.user_id)

    with pytest.raises(InvalidDataError) as excinfo:
        await auth_manager.delete_role(role.role_id)

    assert excinfo.value.translation_key == "role_in_use"
    assert role in await auth_manager.get_roles()


@pytest.mark.parametrize(
    "scope",
    [
        Scope.ALL,
        Scope.UNKNOWN,
        Scope.USERS_MANAGE,
        Scope.USERS_IMPERSONATE,
        Scope.LIBRARY_MANAGE,
        Scope.CONFIG_PROVIDERS_WRITE,
        Scope.CONFIG_CORE_WRITE,
        Scope.SYSTEM_MANAGE,
    ],
)
async def test_a_custom_role_can_never_hold_admin_rights(
    auth_manager: AuthenticationManager, scope: Scope
) -> None:
    """
    A custom role is refused every scope that amounts to admin rights, or grants nothing.

    :param scope: The scope a custom role can not be granted.
    """
    role = await auth_manager.create_role("Kids", [])

    with pytest.raises(InvalidDataError) as excinfo:
        await auth_manager.create_role("Admins", [Scope.LIBRARY_WRITE, scope])
    assert excinfo.value.translation_key == "role_scope_not_allowed"
    with pytest.raises(InvalidDataError) as excinfo:
        await auth_manager.update_role(role.role_id, scopes=[scope])
    assert excinfo.value.translation_key == "role_scope_not_allowed"

    assert (await auth_manager.get_roles())[4:] == [role]


def test_a_custom_role_always_holds_the_scopes_of_a_guest() -> None:
    """The scopes to sign in, browse and play come with every custom role."""
    assert custom_role_scopes([]) == sorted(GUEST_SCOPES)
    assert custom_role_scopes([Scope.USERS_INVITE, Scope.LIBRARY_READ]) == sorted(
        GUEST_SCOPES | {Scope.USERS_INVITE}
    )


@pytest.mark.parametrize(
    ("granted", "implied"),
    [
        (Scope.CONFIG_PLAYERS_WRITE, {Scope.CONFIG_PLAYERS_READ}),
        (Scope.CONFIG_PROVIDERS_OWN, {Scope.CONFIG_PROVIDERS_READ}),
    ],
)
def test_a_custom_role_holds_the_scopes_a_granted_scope_is_of_no_use_without(
    granted: Scope, implied: set[Scope]
) -> None:
    """
    A granted scope brings along the scopes it depends on.

    :param granted: The scope granted to the custom role.
    :param implied: The scopes the granted scope depends on.
    """
    assert custom_role_scopes([granted]) == sorted(GUEST_SCOPES | {granted} | implied)


@pytest.mark.parametrize(
    "name", ["kids", " KIDS ", "admin", "Administrator", "GUEST", "service", "User"]
)
async def test_a_role_name_is_unique_among_all_roles(
    auth_manager: AuthenticationManager, name: str
) -> None:
    """
    No two roles share a name, whatever its case, the builtin names and ids included.

    :param name: A name that is taken.
    """
    await auth_manager.create_role("Kids", [])
    teens = await auth_manager.create_role("Teens", [])

    with pytest.raises(InvalidDataError) as excinfo:
        await auth_manager.create_role(name, [])
    assert excinfo.value.translation_key == "role_name_taken"
    with pytest.raises(InvalidDataError) as excinfo:
        await auth_manager.update_role(teens.role_id, name=name)
    assert excinfo.value.translation_key == "role_name_taken"


async def test_a_role_may_keep_its_own_name_in_another_case(
    auth_manager: AuthenticationManager,
) -> None:
    """Renaming a role to its own name in another case is no clash with itself."""
    role = await auth_manager.create_role("Kids", [])

    assert (await auth_manager.update_role(role.role_id, name="KIDS")).name == "KIDS"


@pytest.mark.parametrize("name", ["", "   ", "x" * (ROLE_NAME_MAX_LENGTH + 1)])
async def test_a_role_name_is_required_and_limited_in_length(
    auth_manager: AuthenticationManager, name: str
) -> None:
    """
    A role name holds at least one character besides whitespace, and not too many.

    :param name: An invalid name.
    """
    with pytest.raises(InvalidDataError) as excinfo:
        await auth_manager.create_role(name, [])
    assert excinfo.value.translation_key == "role_name_invalid"
    assert excinfo.value.translation_args == [ROLE_NAME_MAX_LENGTH]

    longest_name = "x" * ROLE_NAME_MAX_LENGTH
    assert (await auth_manager.create_role(longest_name, [])).name == longest_name


async def test_a_user_can_be_created_with_a_custom_role(
    auth_manager: AuthenticationManager,
) -> None:
    """A new user may get a custom role right away, but no role that does not exist."""
    await _sign_in_admin(auth_manager)
    role = await auth_manager.create_role("Kids", [])

    kid = await auth_manager.create_user_with_api(
        username="kid", password="password123", role=role.role_id
    )

    assert kid.role == role.role_id
    stored_kid = await auth_manager.get_user(kid.user_id)
    assert stored_kid is not None
    assert stored_kid.role == role.role_id
    with pytest.raises(InvalidDataError) as excinfo:
        await auth_manager.create_user_with_api(
            username="nobody", password="password123", role="unknown"
        )
    assert excinfo.value.translation_key == "role_not_found"
    assert await auth_manager.get_user_by_username("nobody") is None


async def test_a_user_can_be_given_a_custom_role(auth_manager: AuthenticationManager) -> None:
    """A user may be given a custom role, which may own music sources as any member."""
    await _sign_in_admin(auth_manager)
    role = await auth_manager.create_role("Kids", [])
    member = await auth_manager.create_user(username="member", role=UserRole.USER)
    set_music_source_access(
        auth_manager.mass,
        {"spotify--owned": ProviderAccess(owner=member.user_id, sharing=ProviderSharing.PRIVATE)},
    )

    updated_member = await auth_manager.update_user_profile(
        user_id=member.user_id, role=role.role_id
    )

    assert updated_member.role == role.role_id
    with pytest.raises(InvalidDataError) as excinfo:
        await auth_manager.update_user_profile(user_id=member.user_id, role="unknown")
    assert excinfo.value.translation_key == "role_not_found"
    stored_member = await auth_manager.get_user(member.user_id)
    assert stored_member is not None
    assert stored_member.role == role.role_id


async def test_the_last_admin_can_not_give_up_the_admin_role(
    auth_manager: AuthenticationManager,
) -> None:
    """The only enabled admin keeps its role until another user is made an admin."""
    admin = await _sign_in_admin(auth_manager)

    with pytest.raises(InvalidDataError) as excinfo:
        await auth_manager.update_user_profile(role=UserRole.USER)
    assert excinfo.value.translation_key == "last_admin"
    stored_admin = await auth_manager.get_user(admin.user_id)
    assert stored_admin is not None
    assert stored_admin.role == UserRole.ADMIN

    await auth_manager.create_user(username="admin_2", role=UserRole.ADMIN)
    assert (await auth_manager.update_user_profile(role=UserRole.USER)).role == UserRole.USER


async def test_a_disabled_admin_does_not_count_as_another_admin(
    auth_manager: AuthenticationManager,
) -> None:
    """Only an enabled admin can take over, a disabled admin may lose its role."""
    admin = await _sign_in_admin(auth_manager)
    disabled_admin = await auth_manager.create_user(username="admin_2", role=UserRole.ADMIN)
    await auth_manager.disable_user(disabled_admin.user_id)

    with pytest.raises(InvalidDataError) as excinfo:
        await auth_manager.update_user_role(admin.user_id, UserRole.USER, admin)
    assert excinfo.value.translation_key == "last_admin"

    assert await auth_manager.update_user_role(disabled_admin.user_id, UserRole.USER, admin)


async def test_a_role_change_closes_the_sessions_of_the_user(
    auth_manager: AuthenticationManager,
) -> None:
    """A user reconnects after its role changed, so its sessions hold the new scopes."""
    admin = await _sign_in_admin(auth_manager)
    member = await auth_manager.create_user(username="member", role=UserRole.USER)

    with patch.object(auth_manager.webserver, "disconnect_websockets_for_user") as disconnect:
        assert await auth_manager.update_user_role(member.user_id, UserRole.USER, admin)
        disconnect.assert_not_called()
        assert await auth_manager.update_user_role(member.user_id, UserRole.GUEST, admin)

    disconnect.assert_called_once_with(member.user_id)


async def test_a_scope_change_closes_the_sessions_of_the_holders_of_the_role(
    auth_manager: AuthenticationManager,
) -> None:
    """The holders of a role reconnect when its scopes change, not when it is renamed."""
    role = await auth_manager.create_role("Kids", [])
    kids = [
        await auth_manager.create_user(username=f"kid_{index}", role=role.role_id)
        for index in range(2)
    ]
    await auth_manager.create_user(username="member", role=UserRole.USER)

    with patch.object(auth_manager.webserver, "disconnect_websockets_for_user") as disconnect:
        await auth_manager.update_role(role.role_id, name="Teens")
        # scopes that add up to the ones the role holds already are no change either
        await auth_manager.update_role(role.role_id, scopes=[Scope.LIBRARY_READ])
        disconnect.assert_not_called()
        await auth_manager.update_role(role.role_id, scopes=[Scope.LIBRARY_WRITE])

    assert sorted(call.args[0] for call in disconnect.call_args_list) == sorted(
        kid.user_id for kid in kids
    )


async def test_the_scope_map_holds_the_custom_roles(auth_manager: AuthenticationManager) -> None:
    """The scopes of each role by role id, the custom roles after the builtin ones."""
    role = await auth_manager.create_role("Kids", [Scope.LIBRARY_WRITE])

    scope_map = await auth_manager.get_role_scopes()

    assert list(scope_map) == [*BUILTIN_ROLE_IDS, role.role_id]
    assert scope_map[UserRole.ADMIN] == ["*"]
    assert scope_map[role.role_id] == sorted(str(scope) for scope in role.scopes)


async def test_the_custom_roles_are_loaded_from_the_database_at_setup(
    auth_manager: AuthenticationManager,
) -> None:
    """The custom roles survive a restart, and closing forgets them for another server."""
    role = await auth_manager.create_role("Kids", [Scope.LIBRARY_WRITE])
    kid = _user(role.role_id)

    await auth_manager.close()
    assert not has_scope(kid, Scope.LIBRARY_WRITE)
    await auth_manager.setup()

    assert role in await auth_manager.get_roles()
    assert has_scope(kid, Scope.LIBRARY_WRITE)


async def test_the_roles_table_is_added_to_an_existing_database(
    auth_manager: AuthenticationManager,
) -> None:
    """A database of the current schema without the roles table gets it, its users untouched."""
    admin = await auth_manager.create_user(username="admin", role=UserRole.ADMIN)
    await auth_manager.database.execute("DROP TABLE roles")
    await auth_manager.database.commit()

    await _restart(auth_manager)

    assert await auth_manager.database.get_count("roles") == 0
    schema_row = await auth_manager.database.get_row("settings", {"key": "schema_version"})
    assert schema_row is not None
    assert schema_row["value"] == str(DB_SCHEMA_VERSION)
    stored_admin = await auth_manager.get_user(admin.user_id)
    assert stored_admin is not None
    assert stored_admin.role == UserRole.ADMIN
    assert (await auth_manager.create_role("Kids", [])).name == "Kids"


@pytest.mark.parametrize(
    "stored_scopes",
    [
        ["*"],
        ["users.manage"],
        ["library.write", "users.impersonate"],
        ["library.write", "library.manage"],
        ["config.providers.write", "config.core.write", "system.manage"],
    ],
    ids=["all", "manage_users", "impersonate_users", "manage_library", "run_the_server"],
)
async def test_a_stored_role_never_grants_what_a_custom_role_can_not_hold(
    auth_manager: AuthenticationManager, stored_scopes: list[str]
) -> None:
    """
    A stored role keeps only the scopes a custom role may hold, as a tampered database has others.

    :param stored_scopes: The scopes of the stored role.
    """
    await _store_role(auth_manager, "tampered", json_dumps(stored_scopes))

    await _restart(auth_manager)

    user = _user("tampered")
    for scope in (
        Scope.USERS_MANAGE,
        Scope.USERS_IMPERSONATE,
        Scope.LIBRARY_MANAGE,
        Scope.CONFIG_PROVIDERS_WRITE,
        Scope.CONFIG_CORE_WRITE,
        Scope.SYSTEM_MANAGE,
    ):
        assert not has_scope(user, scope)
    roles = {role.role_id: role for role in await auth_manager.get_roles()}
    assert set(roles["tampered"].scopes) <= GUEST_SCOPES | {Scope.LIBRARY_WRITE}
    assert has_scope(user, Scope.LIBRARY_READ)


async def test_loading_drops_what_it_can_not_use_and_keeps_the_role(
    auth_manager: AuthenticationManager, caplog: pytest.LogCaptureFixture
) -> None:
    """An unknown scope is dropped, a role with unreadable scopes stays with the guest scopes."""
    await _store_role(auth_manager, "newer", json_dumps(["library.write", "some.future.scope"]))
    await _store_role(auth_manager, "broken", "{not json")

    await _restart(auth_manager)

    roles = {role.role_id: role for role in await auth_manager.get_roles()}
    assert roles["newer"].scopes == sorted(GUEST_SCOPES | {Scope.LIBRARY_WRITE})
    # still listed, so an admin can give it its scopes again
    assert roles["broken"].scopes == sorted(GUEST_SCOPES)
    assert "Custom role 'Broken' has unreadable scopes" in caplog.text
    assert "Ignoring scopes some.future.scope of custom role 'Newer'" in caplog.text


async def test_a_rename_keeps_the_stored_scopes_this_version_ignores(
    auth_manager: AuthenticationManager,
) -> None:
    """Renaming a role leaves its stored scopes alone, the ones of a newer version included."""
    await _store_role(auth_manager, "newer", json_dumps(["library.write", "some.future.scope"]))
    await _restart(auth_manager)

    await auth_manager.update_role("newer", name="Renamed")

    row = await auth_manager.database.get_row("roles", {"role_id": "newer"})
    assert row is not None
    assert row["name"] == "Renamed"
    assert json_loads(row["scopes"]) == ["library.write", "some.future.scope"]
