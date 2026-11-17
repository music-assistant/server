"""Tests for the external identity storage and the email address of users in the auth database."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.auth import AuthProviderType, UserRole
from music_assistant_models.errors import InsufficientPermissions, InvalidDataError

from music_assistant.controllers.webserver.auth import AuthenticationManager
from music_assistant.controllers.webserver.controller import WebserverController
from music_assistant.controllers.webserver.helpers.auth_middleware import set_current_user
from music_assistant.controllers.webserver.helpers.login_flow import ExternalIdentity
from music_assistant.helpers.datetime import utc

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

ISSUER = "https://idp.example.com"


@pytest.fixture
async def auth_manager(mass_minimal: MusicAssistant) -> AsyncGenerator[AuthenticationManager]:
    """
    Provide the authentication manager of a minimal server, set up on a fresh auth.db.

    :param mass_minimal: The minimal server to set up the authentication manager on.
    """
    webserver = WebserverController(mass_minimal)
    mass_minimal.webserver = webserver
    webserver.config = await mass_minimal.config.get_core_config("webserver")
    mass_minimal.music = MagicMock()
    mass_minimal.music.database.delete = AsyncMock()
    mass_minimal.music.playlists.release_user_playlists = AsyncMock()
    mass_minimal.music.favorites.release_user = AsyncMock()
    await webserver.auth.setup()
    try:
        yield webserver.auth
    finally:
        set_current_user(None)
        await webserver.auth.close()


def _identity(subject: str = "sub-1", issuer: str = ISSUER) -> ExternalIdentity:
    """
    Return a verified OIDC identity.

    :param subject: The subject of the identity.
    :param issuer: The issuer of the identity.
    """
    return ExternalIdentity(
        provider_type=AuthProviderType.OIDC,
        issuer=issuer,
        subject=subject,
        email="Person@Example.com",
        email_verified=True,
        username="person",
        display_name="Person",
        claims={"groups": ["music"]},
    )


async def test_an_identity_is_stored_read_updated_and_deleted(
    auth_manager: AuthenticationManager,
) -> None:
    """An identity linked to a user can be found, refreshed and unlinked."""
    user = await auth_manager.create_user(username="person")

    stored = await auth_manager.add_identity(user.user_id, "oidc--main", _identity())

    assert await auth_manager.get_identity(ISSUER, "sub-1") == stored
    assert await auth_manager.get_user_identities(user.user_id) == [stored]
    assert stored.username_hint == "person"
    assert stored.claims == {"groups": ["music"]}
    assert stored.last_login_at is None

    login_at = utc() + timedelta(minutes=5)
    refreshed = ExternalIdentity(
        AuthProviderType.OIDC, ISSUER, "sub-1", email=None, display_name="Renamed"
    )
    await auth_manager.update_identity_snapshot(stored.identity_id, refreshed, login_at)

    updated = await auth_manager.get_identity(ISSUER, "sub-1")
    assert updated is not None
    assert updated.display_name == "Renamed"
    assert updated.email is None
    assert not updated.email_verified
    assert updated.username_hint is None
    assert updated.claims == {}
    assert updated.last_login_at == login_at
    assert updated.created_at == stored.created_at

    await auth_manager.delete_identity(stored.identity_id)
    assert await auth_manager.get_identity(ISSUER, "sub-1") is None


async def test_an_issuer_and_subject_link_to_one_user_only(
    auth_manager: AuthenticationManager,
) -> None:
    """A second identity with the same issuer and subject is refused, another issuer is not."""
    first = await auth_manager.create_user(username="first")
    second = await auth_manager.create_user(username="second")
    await auth_manager.add_identity(first.user_id, "oidc", _identity())

    with pytest.raises(InvalidDataError, match="already linked"):
        await auth_manager.add_identity(second.user_id, "oidc", _identity())

    other = await auth_manager.add_identity(
        second.user_id, "oidc", _identity(issuer="https://other.example.com")
    )
    assert await auth_manager.get_user_identities(second.user_id) == [other]
    assert len(await auth_manager.get_user_identities(first.user_id)) == 1


async def test_deleting_a_user_removes_its_identities(
    auth_manager: AuthenticationManager,
) -> None:
    """The identities of a deleted user are deleted with it."""
    admin = await auth_manager.create_user(username="admin", role=UserRole.ADMIN)
    user = await auth_manager.create_user(username="person")
    await auth_manager.add_identity(user.user_id, "oidc", _identity())
    kept = await auth_manager.add_identity(admin.user_id, "oidc", _identity("sub-admin"))
    set_current_user(admin)

    await auth_manager.delete_user(user.user_id)

    assert await auth_manager.get_identity(ISSUER, "sub-1") is None
    assert await auth_manager.get_identity(ISSUER, "sub-admin") == kept


async def test_orphaned_identities_are_pruned(auth_manager: AuthenticationManager) -> None:
    """An identity whose user no longer exists is removed by the cleanup."""
    user = await auth_manager.create_user(username="person")
    kept = await auth_manager.add_identity(user.user_id, "oidc", _identity())
    await auth_manager.add_identity("gone", "oidc", _identity("sub-orphan"))

    await auth_manager._prune_orphaned_user_rows()

    assert await auth_manager.get_identity(ISSUER, "sub-orphan") is None
    assert await auth_manager.get_identity(ISSUER, "sub-1") == kept


async def test_an_admin_sets_a_normalized_email_on_create_and_update(
    auth_manager: AuthenticationManager,
) -> None:
    """An email set by a manager of users is stored stripped and lower-cased, and can be cleared."""
    admin = await auth_manager.create_user(username="admin", role=UserRole.ADMIN)
    set_current_user(admin)

    created = await auth_manager.create_user_with_api(
        "person", "password123", email="  Person@Example.COM "
    )
    assert created.email == "person@example.com"

    updated = await auth_manager.update_user_profile(
        user_id=created.user_id, email="New@Example.com"
    )
    assert updated.email == "new@example.com"
    fetched = await auth_manager.get_user(created.user_id)
    assert fetched is not None
    assert fetched.email == "new@example.com"
    assert [u.email for u in await auth_manager.list_users() if u.username == "person"] == [
        "new@example.com"
    ]

    own = await auth_manager.update_user_profile(email="Admin@Example.com")
    assert own.email == "admin@example.com"

    cleared = await auth_manager.update_user_profile(user_id=created.user_id, email="")
    assert cleared.email is None
    second = await auth_manager.update_user_profile(user_id=admin.user_id, email="  ")
    assert second.email is None


async def test_an_email_held_by_another_user_is_refused(
    auth_manager: AuthenticationManager,
) -> None:
    """Two users can not hold the same email address, whatever its case."""
    admin = await auth_manager.create_user(username="admin", role=UserRole.ADMIN)
    set_current_user(admin)
    holder = await auth_manager.create_user_with_api(
        "holder", "password123", email="taken@example.com"
    )
    other = await auth_manager.create_user_with_api("other", "password123")

    with pytest.raises(InvalidDataError) as excinfo:
        await auth_manager.update_user_profile(user_id=other.user_id, email="TAKEN@example.com")
    assert excinfo.value.translation_key == "email_taken"
    with pytest.raises(InvalidDataError) as excinfo:
        await auth_manager.create_user_with_api("third", "password123", email="Taken@Example.com")
    assert excinfo.value.translation_key == "email_taken"
    assert await auth_manager.get_user_by_username("third") is None

    same = await auth_manager.update_user_profile(user_id=holder.user_id, email="Taken@Example.com")
    assert same.email == "taken@example.com"
    await auth_manager.update_user_profile(user_id=holder.user_id, email="")
    freed = await auth_manager.update_user_profile(user_id=other.user_id, email="taken@example.com")
    assert freed.email == "taken@example.com"


async def test_a_user_can_not_set_its_own_email(auth_manager: AuthenticationManager) -> None:
    """A user without the users.manage scope can not set or change its email address."""
    user = await auth_manager.create_user(username="person", role=UserRole.USER)
    set_current_user(user)

    with pytest.raises(InsufficientPermissions):
        await auth_manager.update_user_profile(email="person@example.com")
    with pytest.raises(InsufficientPermissions):
        await auth_manager.update_user_profile(user_id=user.user_id, email="")

    fetched = await auth_manager.get_user(user.user_id)
    assert fetched is not None
    assert fetched.email is None
    renamed = await auth_manager.update_user_profile(display_name="Person")
    assert renamed.display_name == "Person"
