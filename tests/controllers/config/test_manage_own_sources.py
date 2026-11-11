"""Tests for the commands a member may run on the music sources it owns."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.config_entries import ProviderAccess
from music_assistant_models.enums import ProviderSharing, ProviderType
from music_assistant_models.errors import InsufficientPermissions
from music_assistant_models.provider import ProviderManifest

from music_assistant.constants import CONF_PROVIDERS
from music_assistant.controllers.webserver.helpers.auth_middleware import set_current_user
from music_assistant.mass import MusicAssistant
from tests.common import set_music_source_access

MUSIC_DOMAIN = "own_music"
PLAYER_DOMAIN = "own_player"
OWN_INSTANCE = f"{MUSIC_DOMAIN}--aaaa"
OTHER_INSTANCE = f"{MUSIC_DOMAIN}--bbbb"
HOUSE_INSTANCE = f"{MUSIC_DOMAIN}--cccc"
PLAYER_INSTANCE = PLAYER_DOMAIN
OWNER_ID = "owner"
OTHER_ID = "other"


@pytest.fixture
def own_sources_mass(mass_minimal: MusicAssistant) -> MusicAssistant:
    """
    Provide a minimal server holding a music source per owner, plus a player provider.

    :param mass_minimal: The minimal server to configure.
    """
    for domain, prov_type in (
        (MUSIC_DOMAIN, ProviderType.MUSIC),
        (PLAYER_DOMAIN, ProviderType.PLAYER),
    ):
        mass_minimal._provider_manifests[domain] = ProviderManifest(
            type=prov_type,
            domain=domain,
            name=f"{domain} provider",
            description="",
            codeowners=[],
            multi_instance=prov_type == ProviderType.MUSIC,
        )
    set_music_source_access(
        mass_minimal,
        {
            OWN_INSTANCE: ProviderAccess(owner=OWNER_ID, sharing=ProviderSharing.PRIVATE),
            OTHER_INSTANCE: ProviderAccess(owner=OTHER_ID, sharing=ProviderSharing.PRIVATE),
            HOUSE_INSTANCE: None,
        },
    )
    mass_minimal.config.set(
        f"{CONF_PROVIDERS}/{PLAYER_INSTANCE}",
        {
            "type": ProviderType.PLAYER.value,
            "domain": PLAYER_DOMAIN,
            "instance_id": PLAYER_INSTANCE,
            "values": {},
        },
    )
    mass_minimal.music = MagicMock()
    for method in (
        "cleanup_provider_shortcuts",
        "cleanup_provider",
        "cleanup_library_shortcuts",
        "unschedule_provider_sync",
        "on_provider_loaded",
    ):
        setattr(mass_minimal.music, method, AsyncMock())
    return mass_minimal


def _user(user_id: str, role: str) -> User:
    """Return a user with the given id and role."""
    return User(user_id=user_id, username=user_id, role=role)


def _loaded_provider() -> MagicMock:
    """Return a stand-in for the loaded provider instance of the owned music source."""
    provider = MagicMock()
    provider.instance_id = OWN_INSTANCE
    provider.domain = MUSIC_DOMAIN
    provider.handle_config_action = AsyncMock(return_value=None)
    return provider


async def test_the_owner_renames_its_own_source(own_sources_mass: MusicAssistant) -> None:
    """A member may change the config of a music source it owns."""
    set_current_user(_user(OWNER_ID, UserRole.USER))

    with patch.object(own_sources_mass, "load_provider_config", AsyncMock()) as mock_load:
        config = await own_sources_mass.config.save_provider_config(
            MUSIC_DOMAIN, {"name": "Mine"}, instance_id=OWN_INSTANCE
        )

    assert config.name == "Mine"
    assert own_sources_mass.config.get(f"{CONF_PROVIDERS}/{OWN_INSTANCE}/name") == "Mine"
    # the source is enabled but not loaded, so saving loads it
    mock_load.assert_awaited_once()


@pytest.mark.parametrize("instance_id", [OWN_INSTANCE, HOUSE_INSTANCE])
async def test_another_member_may_not_save_a_source_it_does_not_own(
    own_sources_mass: MusicAssistant, instance_id: str
) -> None:
    """
    A member may not touch the config of a source of someone else or of the household.

    :param instance_id: The music source the member tries to save.
    """
    set_current_user(_user(OTHER_ID, UserRole.USER))

    with (
        patch.object(own_sources_mass, "load_provider_config", AsyncMock()) as mock_load,
        pytest.raises(InsufficientPermissions, match="required to manage"),
    ):
        await own_sources_mass.config.save_provider_config(
            MUSIC_DOMAIN, {"name": "Mine"}, instance_id=instance_id
        )

    assert own_sources_mass.config.get(f"{CONF_PROVIDERS}/{instance_id}/name") is None
    mock_load.assert_not_awaited()


@pytest.mark.parametrize("instance_id", [OWN_INSTANCE, OTHER_INSTANCE, HOUSE_INSTANCE])
async def test_an_admin_saves_any_source(
    own_sources_mass: MusicAssistant, instance_id: str
) -> None:
    """
    Managing the sources of the household means saving the config of all of them.

    :param instance_id: The music source the admin saves.
    """
    set_current_user(_user("admin", UserRole.ADMIN))

    with patch.object(own_sources_mass, "load_provider_config", AsyncMock()):
        config = await own_sources_mass.config.save_provider_config(
            MUSIC_DOMAIN, {"name": "Household"}, instance_id=instance_id
        )

    assert config.name == "Household"


async def test_the_owner_removes_its_own_source(own_sources_mass: MusicAssistant) -> None:
    """A member may remove a music source it owns, library entries included."""
    music = cast("MagicMock", own_sources_mass.music)
    set_current_user(_user(OWNER_ID, UserRole.USER))

    with patch.object(own_sources_mass, "unload_provider", AsyncMock()) as mock_unload:
        await own_sources_mass.config.remove_provider_config(OWN_INSTANCE)

    assert own_sources_mass.config.get(f"{CONF_PROVIDERS}/{OWN_INSTANCE}") is None
    mock_unload.assert_awaited_once_with(OWN_INSTANCE, True)
    music.cleanup_provider_shortcuts.assert_awaited_once_with(OWN_INSTANCE)
    music.cleanup_provider.assert_awaited_once_with(OWN_INSTANCE)
    music.cleanup_library_shortcuts.assert_awaited_once()


@pytest.mark.parametrize("instance_id", [OWN_INSTANCE, HOUSE_INSTANCE, PLAYER_INSTANCE])
async def test_another_member_may_not_remove_a_source_it_does_not_own(
    own_sources_mass: MusicAssistant, instance_id: str
) -> None:
    """
    A member may not remove a source of someone else, of the household or a player provider.

    :param instance_id: The provider instance the member tries to remove.
    """
    music = cast("MagicMock", own_sources_mass.music)
    set_current_user(_user(OTHER_ID, UserRole.USER))

    with (
        patch.object(own_sources_mass, "unload_provider", AsyncMock()) as mock_unload,
        pytest.raises(InsufficientPermissions, match="required to manage"),
    ):
        await own_sources_mass.config.remove_provider_config(instance_id)

    assert own_sources_mass.config.get(f"{CONF_PROVIDERS}/{instance_id}") is not None
    mock_unload.assert_not_awaited()
    music.cleanup_provider.assert_not_awaited()


async def test_removing_an_unknown_source_is_refused_on_existence(
    own_sources_mass: MusicAssistant,
) -> None:
    """A source that does not exist reads as unknown, also for a member that does not own it."""
    set_current_user(_user(OTHER_ID, UserRole.USER))

    with pytest.raises(KeyError):
        await own_sources_mass.config.remove_provider_config(f"{MUSIC_DOMAIN}--gone")


async def test_an_admin_removes_a_household_source(own_sources_mass: MusicAssistant) -> None:
    """Managing the sources of the household means removing any of them."""
    set_current_user(_user("admin", UserRole.ADMIN))

    with patch.object(own_sources_mass, "unload_provider", AsyncMock()) as mock_unload:
        await own_sources_mass.config.remove_provider_config(HOUSE_INSTANCE)

    assert own_sources_mass.config.get(f"{CONF_PROVIDERS}/{HOUSE_INSTANCE}") is None
    mock_unload.assert_awaited_once_with(HOUSE_INSTANCE, True)


async def test_the_owner_reloads_its_own_source(own_sources_mass: MusicAssistant) -> None:
    """A member may reload a music source it owns."""
    set_current_user(_user(OWNER_ID, UserRole.USER))

    with patch.object(own_sources_mass, "load_provider_config", AsyncMock()) as mock_load:
        await own_sources_mass.config._reload_provider(OWN_INSTANCE)

    mock_load.assert_awaited_once()


async def test_another_member_may_not_reload_a_household_source(
    own_sources_mass: MusicAssistant,
) -> None:
    """A source of the household is not a member's to reload."""
    set_current_user(_user(OTHER_ID, UserRole.USER))

    with (
        patch.object(own_sources_mass, "load_provider_config", AsyncMock()) as mock_load,
        pytest.raises(InsufficientPermissions, match="required to manage"),
    ):
        await own_sources_mass.config._reload_provider(HOUSE_INSTANCE)

    mock_load.assert_not_awaited()


@pytest.mark.parametrize("role", [UserRole.USER, UserRole.ADMIN], ids=["member", "admin"])
async def test_reloading_a_vanished_source_is_a_no_op(
    own_sources_mass: MusicAssistant, role: str
) -> None:
    """
    A source removed before the reload ran is silently skipped, whoever asked for it.

    :param role: Role id of the calling user.
    """
    set_current_user(_user("caller", role))

    with patch.object(own_sources_mass, "load_provider_config", AsyncMock()) as mock_load:
        await own_sources_mass.config._reload_provider(f"{MUSIC_DOMAIN}--gone")

    mock_load.assert_not_awaited()


async def test_the_owner_invokes_an_action_on_its_own_source(
    own_sources_mass: MusicAssistant,
) -> None:
    """A member may press an action button in the options of a music source it owns."""
    provider = _loaded_provider()
    set_current_user(_user(OWNER_ID, UserRole.USER))

    with patch.object(own_sources_mass, "get_provider", MagicMock(return_value=provider)):
        result = await own_sources_mass.config.invoke_provider_config_action(
            OWN_INSTANCE, "refresh"
        )

    assert result == []
    provider.handle_config_action.assert_awaited_once_with("refresh")


async def test_another_member_may_not_invoke_an_action_on_a_source_it_does_not_own(
    own_sources_mass: MusicAssistant,
) -> None:
    """An action button of another member's source is off limits, the action never runs."""
    provider = _loaded_provider()
    set_current_user(_user(OTHER_ID, UserRole.USER))

    with (
        patch.object(own_sources_mass, "get_provider", MagicMock(return_value=provider)),
        pytest.raises(InsufficientPermissions, match="required to manage"),
    ):
        await own_sources_mass.config.invoke_provider_config_action(OWN_INSTANCE, "refresh")

    provider.handle_config_action.assert_not_awaited()


async def test_the_server_itself_invokes_an_action_on_any_source(
    own_sources_mass: MusicAssistant,
) -> None:
    """A server-side caller has no user context and is trusted with every source."""
    provider = _loaded_provider()
    set_current_user(None)

    with patch.object(own_sources_mass, "get_provider", MagicMock(return_value=provider)):
        result = await own_sources_mass.config.invoke_provider_config_action(
            OWN_INSTANCE, "refresh"
        )

    assert result == []
    provider.handle_config_action.assert_awaited_once_with("refresh")
