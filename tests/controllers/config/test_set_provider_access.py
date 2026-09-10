"""Tests for the command that sets who owns a music source and who else may use it."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.config_entries import ProviderAccess, ProviderConfig
from music_assistant_models.enums import EventType, ProviderSharing, ProviderType
from music_assistant_models.errors import InsufficientPermissions, InvalidDataError
from music_assistant_models.provider import ProviderManifest

from music_assistant.constants import CONF_PROVIDERS
from music_assistant.controllers.webserver.controller import WebserverController
from music_assistant.controllers.webserver.helpers.auth_middleware import set_current_user
from music_assistant.mass import MusicAssistant
from tests.common import set_music_source_access

if TYPE_CHECKING:
    from music_assistant_models.event import MassEvent

MUSIC_DOMAIN = "test_music"
MUSIC_INSTANCE = f"{MUSIC_DOMAIN}--aaaa"
OTHER_INSTANCE = f"{MUSIC_DOMAIN}--bbbb"
PLAYER_INSTANCE = "test_player"
BUILTIN_INSTANCE = "test_builtin"


@pytest.fixture
async def access_mass(mass_minimal: MusicAssistant) -> AsyncGenerator[MusicAssistant]:
    """Provide a minimal server with an auth database and a few source configs."""
    webserver = WebserverController(mass_minimal)
    mass_minimal.webserver = webserver
    webserver.config = await mass_minimal.config.get_core_config("webserver")
    await webserver.auth.setup()
    for domain, prov_type, builtin in (
        (MUSIC_DOMAIN, ProviderType.MUSIC, False),
        (PLAYER_INSTANCE, ProviderType.PLAYER, False),
        (BUILTIN_INSTANCE, ProviderType.MUSIC, True),
    ):
        mass_minimal._provider_manifests[domain] = ProviderManifest(
            type=prov_type,
            domain=domain,
            name=f"{domain} provider",
            description="",
            codeowners=[],
            multi_instance=not builtin,
            builtin=builtin,
        )
    set_music_source_access(mass_minimal, {MUSIC_INSTANCE: None, OTHER_INSTANCE: None})
    for instance_id, prov_type in (
        (PLAYER_INSTANCE, ProviderType.PLAYER),
        (BUILTIN_INSTANCE, ProviderType.MUSIC),
    ):
        mass_minimal.config.set(
            f"{CONF_PROVIDERS}/{instance_id}",
            {
                "type": prov_type.value,
                "domain": instance_id,
                "instance_id": instance_id,
                "values": {},
            },
        )
    try:
        yield mass_minimal
    finally:
        await webserver.auth.close()


async def _create_user(mass: MusicAssistant, username: str, role: UserRole = UserRole.USER) -> User:
    """Create a user and return it."""
    return await mass.webserver.auth.create_user(username=username, role=role)


def _stored_access(mass: MusicAssistant, instance_id: str) -> dict[str, object] | None:
    """Return the stored access record of the given music source."""
    access: dict[str, object] | None = mass.config.get(f"{CONF_PROVIDERS}/{instance_id}/access")
    return access


async def test_admin_sets_owner_and_shared_users(access_mass: MusicAssistant) -> None:
    """An admin decides who owns a music source and who it is shared with."""
    admin = await _create_user(access_mass, "admin", UserRole.ADMIN)
    owner = await _create_user(access_mass, "owner")
    guest = await _create_user(access_mass, "party_guest", UserRole.GUEST)
    set_current_user(admin)

    config = await access_mass.config.set_provider_access(
        MUSIC_INSTANCE,
        owner=owner.user_id,
        sharing=ProviderSharing.SELECTED,
        # the owner and a duplicate must not end up in the share list
        shared_users=[guest.user_id, admin.user_id, guest.user_id, owner.user_id],
    )

    assert config.access == ProviderAccess(
        owner=owner.user_id,
        sharing=ProviderSharing.SELECTED,
        shared_users=[guest.user_id, admin.user_id],
    )
    assert _stored_access(access_mass, MUSIC_INSTANCE) == {
        "owner": owner.user_id,
        "sharing": "selected",
        "shared_users": [guest.user_id, admin.user_id],
    }


async def test_owner_may_share_its_own_source(access_mass: MusicAssistant) -> None:
    """A member may change the sharing of a source it owns."""
    owner = await _create_user(access_mass, "owner")
    set_music_source_access(
        access_mass,
        {MUSIC_INSTANCE: ProviderAccess(owner=owner.user_id, sharing=ProviderSharing.PRIVATE)},
    )
    set_current_user(owner)

    config = await access_mass.config.set_provider_access(
        MUSIC_INSTANCE, owner=owner.user_id, sharing=ProviderSharing.MEMBERS
    )

    assert config.access == ProviderAccess(owner=owner.user_id, sharing=ProviderSharing.MEMBERS)


async def test_owner_may_not_hand_over_its_source(access_mass: MusicAssistant) -> None:
    """Handing a music source to someone else is up to an admin."""
    owner = await _create_user(access_mass, "owner")
    other = await _create_user(access_mass, "other")
    set_music_source_access(
        access_mass,
        {MUSIC_INSTANCE: ProviderAccess(owner=owner.user_id, sharing=ProviderSharing.PRIVATE)},
    )
    set_current_user(owner)

    with pytest.raises(InsufficientPermissions):
        await access_mass.config.set_provider_access(
            MUSIC_INSTANCE, owner=other.user_id, sharing=ProviderSharing.PRIVATE
        )


async def test_a_member_may_not_share_another_users_source(access_mass: MusicAssistant) -> None:
    """Only the owner of a music source may share it."""
    owner = await _create_user(access_mass, "owner")
    other = await _create_user(access_mass, "other")
    set_music_source_access(
        access_mass,
        {MUSIC_INSTANCE: ProviderAccess(owner=owner.user_id, sharing=ProviderSharing.PRIVATE)},
    )
    set_current_user(other)

    with pytest.raises(InsufficientPermissions):
        await access_mass.config.set_provider_access(
            MUSIC_INSTANCE, owner=other.user_id, sharing=ProviderSharing.PRIVATE
        )


async def test_the_system_user_can_not_own_a_source(access_mass: MusicAssistant) -> None:
    """The Home Assistant system user is a service account, not a member."""
    admin = await _create_user(access_mass, "admin", UserRole.ADMIN)
    system_user = await access_mass.webserver.auth.get_homeassistant_system_user()
    set_current_user(admin)

    with pytest.raises(InvalidDataError):
        await access_mass.config.set_provider_access(MUSIC_INSTANCE, owner=system_user.user_id)
    with pytest.raises(InvalidDataError):
        await access_mass.config.set_provider_access(
            MUSIC_INSTANCE,
            owner=admin.user_id,
            sharing=ProviderSharing.SELECTED,
            shared_users=[system_user.user_id],
        )
    assert _stored_access(access_mass, MUSIC_INSTANCE) is None


async def test_a_guest_can_not_own_a_source(access_mass: MusicAssistant) -> None:
    """A guest account is temporary, so it can not be given a music source."""
    admin = await _create_user(access_mass, "admin", UserRole.ADMIN)
    guest = await _create_user(access_mass, "party_guest", UserRole.GUEST)
    set_current_user(admin)

    with pytest.raises(InvalidDataError):
        await access_mass.config.set_provider_access(MUSIC_INSTANCE, owner=guest.user_id)


async def test_an_unknown_user_is_refused(access_mass: MusicAssistant) -> None:
    """A source can only be given to a user that exists and is enabled."""
    admin = await _create_user(access_mass, "admin", UserRole.ADMIN)
    disabled = await _create_user(access_mass, "disabled")
    set_current_user(admin)
    await access_mass.webserver.auth.disable_user(disabled.user_id)

    with pytest.raises(InvalidDataError):
        await access_mass.config.set_provider_access(MUSIC_INSTANCE, owner="does-not-exist")
    with pytest.raises(InvalidDataError):
        await access_mass.config.set_provider_access(MUSIC_INSTANCE, owner=disabled.user_id)


@pytest.mark.parametrize("instance_id", [PLAYER_INSTANCE, BUILTIN_INSTANCE])
async def test_only_a_real_music_source_can_be_owned(
    access_mass: MusicAssistant, instance_id: str
) -> None:
    """Players and the builtin provider always serve the entire household."""
    admin = await _create_user(access_mass, "admin", UserRole.ADMIN)
    set_current_user(admin)

    with pytest.raises(InvalidDataError):
        await access_mass.config.set_provider_access(instance_id, owner=admin.user_id)


async def test_shared_users_are_dropped_unless_the_source_is_shared_with_a_selection(
    access_mass: MusicAssistant,
) -> None:
    """A share list is only meaningful with SELECTED sharing."""
    admin = await _create_user(access_mass, "admin", UserRole.ADMIN)
    member = await _create_user(access_mass, "member")
    set_current_user(admin)

    config = await access_mass.config.set_provider_access(
        MUSIC_INSTANCE,
        owner=admin.user_id,
        sharing=ProviderSharing.EVERYONE,
        shared_users=[member.user_id],
    )

    assert config.access == ProviderAccess(owner=admin.user_id, sharing=ProviderSharing.EVERYONE)


async def test_a_loaded_source_follows_the_stored_record(access_mass: MusicAssistant) -> None:
    """The loaded instance must not keep serving the access it was loaded with."""
    admin = await _create_user(access_mass, "admin", UserRole.ADMIN)
    provider = SimpleNamespace(
        instance_id=MUSIC_INSTANCE,
        domain=MUSIC_DOMAIN,
        available=True,
        manifest=SimpleNamespace(type=ProviderType.MUSIC),
        supported_features=set(),
        get_config_entries=AsyncMock(return_value=()),
        config=ProviderConfig(
            values={},
            type=ProviderType.MUSIC,
            domain=MUSIC_DOMAIN,
            instance_id=MUSIC_INSTANCE,
        ),
    )
    access_mass._providers[MUSIC_INSTANCE] = provider  # type: ignore[assignment]
    set_current_user(admin)

    await access_mass.config.set_provider_access(
        MUSIC_INSTANCE, owner=admin.user_id, sharing=ProviderSharing.PRIVATE
    )

    assert provider.config.access == ProviderAccess(
        owner=admin.user_id, sharing=ProviderSharing.PRIVATE
    )


async def test_changed_access_is_signalled(access_mass: MusicAssistant) -> None:
    """Every client refreshes its provider list, since its music sources may have changed."""
    admin = await _create_user(access_mass, "admin", UserRole.ADMIN)
    events: list[MassEvent] = []
    access_mass.subscribe(events.append, EventType.PROVIDERS_UPDATED)
    set_current_user(admin)

    await access_mass.config.set_provider_access(MUSIC_INSTANCE, owner=admin.user_id)

    # the event callbacks run on the next loop iteration
    await asyncio.sleep(0)
    assert [event.event for event in events] == [EventType.PROVIDERS_UPDATED]
