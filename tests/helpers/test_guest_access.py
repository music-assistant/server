"""Tests for the guest access helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.auth import UserRole
from music_assistant_models.errors import InvalidDataError

from music_assistant.helpers import guest_access


def _create_mock_mass() -> MagicMock:
    """Create a mock MusicAssistant with a mocked auth controller."""
    mass = MagicMock()
    auth = mass.webserver.auth
    auth.get_user_by_username = AsyncMock(return_value=None)
    auth.create_user = AsyncMock()
    auth.update_user_filters = AsyncMock()
    auth.get_active_join_code = AsyncMock(return_value=None)
    auth.generate_join_code = AsyncMock()
    auth.revoke_join_codes = AsyncMock(return_value=0)
    auth.revoke_tokens_for_user = AsyncMock(return_value=0)
    return mass


async def test_get_or_create_guest_user_returns_existing() -> None:
    """Default guest access preserves an existing filter."""
    mass = _create_mock_mass()
    existing_user = MagicMock(
        role=UserRole.GUEST,
        player_filter=["existing_party_queue"],
    )
    mass.webserver.auth.get_user_by_username.return_value = existing_user

    user = await guest_access.get_or_create_guest_user(mass, "party_guest", "Party Guest")

    assert user is existing_user
    mass.webserver.auth.create_user.assert_not_awaited()
    mass.webserver.auth.update_user_filters.assert_not_awaited()
    mass.webserver.disconnect_websockets_for_user.assert_not_called()


async def test_get_or_create_guest_user_rejects_non_guest() -> None:
    """An existing user with a non-guest role is never reused for guest access."""
    mass = _create_mock_mass()
    mass.webserver.auth.get_user_by_username.return_value = MagicMock(role=UserRole.ADMIN)

    with pytest.raises(InvalidDataError):
        await guest_access.get_or_create_guest_user(mass, "party_guest", "Party Guest")
    mass.webserver.auth.create_user.assert_not_awaited()
    mass.webserver.auth.update_user_filters.assert_not_awaited()


async def test_get_or_create_guest_user_creates_guest() -> None:
    """Default guest access preserves the existing unrestricted create behavior."""
    mass = _create_mock_mass()
    created_user = MagicMock()
    mass.webserver.auth.create_user.return_value = created_user

    user = await guest_access.get_or_create_guest_user(mass, "party_guest", "Party Guest")

    assert user is created_user
    mass.webserver.auth.create_user.assert_awaited_once_with(
        username="party_guest",
        role=UserRole.GUEST,
        display_name="Party Guest",
    )


async def test_get_or_create_guest_user_creates_restricted_guest() -> None:
    """Opting into managed access creates a guest with the restrictive sentinel."""
    mass = _create_mock_mass()
    created_user = MagicMock()
    mass.webserver.auth.create_user.return_value = created_user

    user = await guest_access.get_or_create_guest_user(
        mass,
        "quiz_guest",
        "Quiz Guest",
        allowed_player_ids=(),
    )

    assert user is created_user
    mass.webserver.auth.create_user.assert_awaited_once_with(
        username="quiz_guest",
        role=UserRole.GUEST,
        display_name="Quiz Guest",
        player_filter=[guest_access.GUEST_ACCESS_RESTRICTED_PLAYER_ID],
    )


async def test_get_or_create_guest_user_migrates_unrestricted_guest() -> None:
    """An existing unrestricted guest is updated and its stale sockets are disconnected."""
    mass = _create_mock_mass()
    existing_user = MagicMock(
        role=UserRole.GUEST,
        player_filter=[],
        user_id="guest_user_id",
    )
    updated_user = MagicMock()
    mass.webserver.auth.get_user_by_username.return_value = existing_user
    mass.webserver.auth.update_user_filters.return_value = updated_user

    user = await guest_access.get_or_create_guest_user(
        mass,
        "quiz_guest",
        "Quiz Guest",
        allowed_player_ids=(),
    )

    assert user is updated_user
    mass.webserver.auth.update_user_filters.assert_awaited_once_with(
        existing_user,
        [guest_access.GUEST_ACCESS_RESTRICTED_PLAYER_ID],
        None,
    )
    mass.webserver.disconnect_websockets_for_user.assert_called_once_with("guest_user_id")


async def test_get_or_create_guest_user_manages_allowed_players() -> None:
    """Allowed player IDs are stored with the sentinel in deterministic order."""
    mass = _create_mock_mass()
    created_user = MagicMock()
    mass.webserver.auth.create_user.return_value = created_user

    user = await guest_access.get_or_create_guest_user(
        mass,
        "managed_guest",
        "Managed Guest",
        {"venue_player", "remote_player"},
    )

    assert user is created_user
    mass.webserver.auth.create_user.assert_awaited_once_with(
        username="managed_guest",
        role=UserRole.GUEST,
        display_name="Managed Guest",
        player_filter=[
            guest_access.GUEST_ACCESS_RESTRICTED_PLAYER_ID,
            "remote_player",
            "venue_player",
        ],
    )


async def test_get_or_create_guest_user_refreshes_allowed_player() -> None:
    """A changed allowed player replaces stale access and disconnects existing sockets."""
    mass = _create_mock_mass()
    existing_user = MagicMock(
        role=UserRole.GUEST,
        player_filter=[
            guest_access.GUEST_ACCESS_RESTRICTED_PLAYER_ID,
            "old_player",
        ],
        user_id="managed_guest_id",
    )
    updated_user = MagicMock()
    mass.webserver.auth.get_user_by_username.return_value = existing_user
    mass.webserver.auth.update_user_filters.return_value = updated_user

    user = await guest_access.get_or_create_guest_user(
        mass,
        "managed_guest",
        "Managed Guest",
        ("new_player",),
    )

    assert user is updated_user
    mass.webserver.auth.update_user_filters.assert_awaited_once_with(
        existing_user,
        [
            guest_access.GUEST_ACCESS_RESTRICTED_PLAYER_ID,
            "new_player",
        ],
        None,
    )
    mass.webserver.disconnect_websockets_for_user.assert_called_once_with("managed_guest_id")


async def test_get_or_create_guest_user_ignores_filter_order() -> None:
    """Equivalent managed filters do not cause a database update or reconnect."""
    mass = _create_mock_mass()
    existing_user = MagicMock(
        role=UserRole.GUEST,
        player_filter=["party_player", guest_access.GUEST_ACCESS_RESTRICTED_PLAYER_ID],
    )
    mass.webserver.auth.get_user_by_username.return_value = existing_user

    user = await guest_access.get_or_create_guest_user(
        mass,
        "managed_guest",
        "Managed Guest",
        ("party_player",),
    )

    assert user is existing_user
    mass.webserver.auth.update_user_filters.assert_not_awaited()
    mass.webserver.disconnect_websockets_for_user.assert_not_called()


async def test_get_or_create_join_code_reuses_active_code() -> None:
    """An active join code is reused instead of generating a new one."""
    mass = _create_mock_mass()
    mass.webserver.auth.get_active_join_code.return_value = "ABC123"

    code = await guest_access.get_or_create_join_code(mass, MagicMock())

    assert code == "ABC123"
    mass.webserver.auth.generate_join_code.assert_not_awaited()


async def test_get_or_create_join_code_generates_new_code() -> None:
    """A new join code is generated when no active code exists."""
    mass = _create_mock_mass()
    mass.webserver.auth.generate_join_code.return_value = ("XYZ789", MagicMock())
    user = MagicMock()

    code = await guest_access.get_or_create_join_code(
        mass, user, expires_in_hours=4, max_uses=5, device_name="Quiz Guest"
    )

    assert code == "XYZ789"
    mass.webserver.auth.generate_join_code.assert_awaited_once_with(
        user=user,
        expires_in_hours=4,
        max_uses=5,
        device_name="Quiz Guest",
    )


def test_build_join_url_remote_access() -> None:
    """The remote access URL is used when remote access is enabled."""
    mass = _create_mock_mass()
    mass.webserver.remote_access.is_enabled = True
    mass.webserver.remote_access.remote_id = "remote123"

    url = guest_access.build_join_url(mass, "ABC123")

    assert url == "https://app.music-assistant.io/?remote_id=remote123&join=ABC123"


def test_build_join_url_local() -> None:
    """The local base URL is used when remote access is disabled."""
    mass = _create_mock_mass()
    mass.webserver.remote_access.is_enabled = False
    mass.webserver.base_url = "http://192.168.1.2:8095"

    url = guest_access.build_join_url(mass, "ABC123")

    assert url == "http://192.168.1.2:8095/?join=ABC123"


async def test_revoke_guest_access() -> None:
    """Join codes and tokens of the guest user are revoked."""
    mass = _create_mock_mass()
    user = MagicMock()
    mass.webserver.auth.get_user_by_username.return_value = user
    mass.webserver.auth.revoke_join_codes.return_value = 2
    mass.webserver.auth.revoke_tokens_for_user.return_value = 3

    result = await guest_access.revoke_guest_access(mass, "party_guest")

    assert result == (2, 3)
    mass.webserver.auth.revoke_join_codes.assert_awaited_once_with(user)
    mass.webserver.auth.revoke_tokens_for_user.assert_awaited_once_with(user)


async def test_revoke_guest_access_no_user() -> None:
    """Revoking access for an unknown user is a no-op."""
    mass = _create_mock_mass()

    result = await guest_access.revoke_guest_access(mass, "unknown_guest")

    assert result == (0, 0)
    mass.webserver.auth.revoke_join_codes.assert_not_awaited()
