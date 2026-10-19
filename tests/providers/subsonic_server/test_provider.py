"""Tests for the Subsonic API plugin provider."""

from __future__ import annotations

from collections.abc import Iterator
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.enums import ConfigEntryType

from music_assistant.controllers.webserver.helpers.auth_middleware import set_current_user
from music_assistant.providers.subsonic_server import SubsonicServerProvider
from music_assistant.providers.subsonic_server.auth import derive_app_password
from music_assistant.providers.subsonic_server.constants import (
    CONF_ACTION_REGENERATE_PASSWORDS,
    CONF_JUKEBOX_PLAYER,
    CONF_JUKEBOX_PLAYER_NONE,
    SETUP_KEY_SECRET,
)

SECRET = "1" * 64
ADMIN = User(user_id="admin-id", username="admin", role=UserRole.ADMIN)
MEMBER = User(user_id="member-id", username="member", role=UserRole.USER)
DISABLED = User(user_id="disabled-id", username="disabled", role=UserRole.USER, enabled=False)
SERVICE = User(user_id="service-id", username="homeassistant", role=UserRole.SERVICE)


@pytest.fixture(autouse=True)
def _reset_current_user() -> Iterator[None]:
    yield
    set_current_user(None)


def _create_provider(stored_secret: str | None = SECRET) -> SubsonicServerProvider:
    provider = SubsonicServerProvider.__new__(SubsonicServerProvider)
    provider.mass = MagicMock()
    provider.mass.webserver.auth.list_users = AsyncMock(
        return_value=[ADMIN, MEMBER, DISABLED, SERVICE]
    )
    player = MagicMock(player_id="sonos_living_room", display_name="Living Room")
    provider.mass.players.all_players.return_value = [player]
    provider.config = MagicMock()
    provider.config.get_value.return_value = CONF_JUKEBOX_PLAYER_NONE
    setup_data: dict[str, str] = {SETUP_KEY_SECRET: stored_secret} if stored_secret else {}
    provider.get_setup_value = MagicMock(side_effect=setup_data.get)  # type: ignore[method-assign]
    provider._update_setup_data = MagicMock(  # type: ignore[method-assign]
        side_effect=setup_data.__setitem__
    )
    return provider


def _password_entries(provider_entries: tuple[object, ...]) -> dict[str, object]:
    return {
        entry.key: entry.default_value  # type: ignore[attr-defined]
        for entry in provider_entries
        if entry.key.startswith("app_password_")  # type: ignore[attr-defined]
    }


async def test_admin_sees_password_of_every_active_user() -> None:
    """An admin sees the passwords of enabled, non-service users and the regenerate action."""
    provider = _create_provider()
    set_current_user(ADMIN)
    entries = await provider.get_config_entries()
    assert _password_entries(entries) == {
        "app_password_admin-id": derive_app_password(SECRET, "admin-id"),
        "app_password_member-id": derive_app_password(SECRET, "member-id"),
    }
    assert any(entry.key == CONF_ACTION_REGENERATE_PASSWORDS for entry in entries)


async def test_member_sees_only_own_password() -> None:
    """A member only sees their own password and cannot regenerate passwords."""
    provider = _create_provider()
    set_current_user(MEMBER)
    entries = await provider.get_config_entries()
    assert _password_entries(entries) == {
        "app_password_member-id": derive_app_password(SECRET, "member-id")
    }
    assert all(entry.key != CONF_ACTION_REGENERATE_PASSWORDS for entry in entries)
    cast("MagicMock", provider.mass.webserver.auth.list_users).assert_not_called()


async def test_password_entries_are_read_only_and_labelled_with_username() -> None:
    """Password entries are read-only and carry the username for their label."""
    provider = _create_provider()
    set_current_user(MEMBER)
    entries = await provider.get_config_entries()
    entry = next(entry for entry in entries if entry.key == "app_password_member-id")
    assert entry.type == ConfigEntryType.STRING
    assert entry.read_only
    assert entry.translation_params == ["member"]


async def test_jukebox_player_options() -> None:
    """The jukebox player can be disabled or set to any player."""
    provider = _create_provider()
    set_current_user(ADMIN)
    entries = await provider.get_config_entries()
    entry = next(entry for entry in entries if entry.key == CONF_JUKEBOX_PLAYER)
    assert [option.value for option in entry.options] == [
        CONF_JUKEBOX_PLAYER_NONE,
        "sonos_living_room",
    ]
    assert entry.default_value == CONF_JUKEBOX_PLAYER_NONE


@pytest.mark.parametrize(
    ("value", "expected"),
    [(CONF_JUKEBOX_PLAYER_NONE, None), (None, None), ("sonos_living_room", "sonos_living_room")],
)
def test_jukebox_player_id(value: str | None, expected: str | None) -> None:
    """The none option and an empty value both disable the jukebox."""
    provider = _create_provider()
    cast("MagicMock", provider.config.get_value).return_value = value
    assert provider.jukebox_player_id == expected


def test_secret_is_generated_once() -> None:
    """Without a stored secret one is generated, stored and reused."""
    provider = _create_provider(stored_secret=None)
    password = provider.get_app_password("member-id")
    assert password == provider.get_app_password("member-id")
    provider._update_setup_data.assert_called_once()  # type: ignore[attr-defined]


async def test_regenerate_action_changes_passwords() -> None:
    """The regenerate action stores a new secret, which changes every password."""
    provider = _create_provider()
    set_current_user(ADMIN)
    before = provider.get_app_password("member-id")
    result = await provider.handle_config_action(CONF_ACTION_REGENERATE_PASSWORDS)
    assert isinstance(result, tuple)
    assert provider.get_app_password("member-id") != before


async def test_route_registered_and_removed() -> None:
    """Loading registers the /rest/* route, unloading removes it."""
    provider = _create_provider()
    unregister = MagicMock()
    register = cast("MagicMock", provider.mass.webserver.register_dynamic_route)
    register.return_value = unregister
    await provider.loaded_in_mass()
    path = register.call_args.args[0]
    assert path == "/rest/*"
    await provider.unload()
    unregister.assert_called_once()


async def test_reading_accounts_does_not_reveal_passwords() -> None:
    """A role that may read users but not manage them only sees its own password."""
    provider = _create_provider()
    set_current_user(SERVICE)
    entries = await provider.get_config_entries()
    assert _password_entries(entries) == {
        "app_password_service-id": derive_app_password(SECRET, "service-id")
    }
