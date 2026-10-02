"""Tests for the guest access helpers."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.auth import UserRole
from music_assistant_models.errors import InvalidDataError

from music_assistant.helpers import guest_access
from music_assistant.models.plugin import GuestSession, PluginProvider


def _create_mock_mass() -> MagicMock:
    """Create a mock MusicAssistant with a mocked auth controller."""
    mass = MagicMock()
    auth = mass.webserver.auth
    auth.get_user_by_username = AsyncMock(return_value=None)
    auth.create_user = AsyncMock()
    auth.get_active_join_code = AsyncMock(return_value=None)
    auth.generate_join_code = AsyncMock()
    auth.revoke_join_codes = AsyncMock(return_value=0)
    auth.revoke_tokens_for_user = AsyncMock(return_value=0)
    return mass


def _create_guest_session_plugin(instance_id: str) -> MagicMock:
    """Create a plugin mock exposing no active guest session by default."""
    provider = MagicMock(spec=PluginProvider)
    provider.instance_id = instance_id
    provider.available = True
    provider.get_active_guest_session = AsyncMock(return_value=None)
    return provider


async def test_get_active_guest_sessions_collects_plugins_in_stable_order() -> None:
    """Active sessions are collected by instance id and inactive plugins are omitted."""
    first = _create_guest_session_plugin("plugin_a")
    second = _create_guest_session_plugin("plugin_b")
    inactive = _create_guest_session_plugin("plugin_c")
    unavailable = _create_guest_session_plugin("plugin_d")
    unavailable.available = False
    first_session = GuestSession(provider=first, join_url="https://example.test/a")
    second_session = GuestSession(provider=second, join_url="https://example.test/b")
    first.get_active_guest_session.return_value = first_session
    second.get_active_guest_session.return_value = second_session
    mass = MagicMock(providers=[unavailable, inactive, second, first, MagicMock()])

    assert await guest_access.get_active_guest_sessions(mass) == [first_session, second_session]
    unavailable.get_active_guest_session.assert_not_awaited()


async def test_get_active_guest_sessions_skips_failing_plugin() -> None:
    """One broken guest-session provider does not hide sessions from healthy plugins."""
    broken = _create_guest_session_plugin("plugin_a")
    healthy = _create_guest_session_plugin("plugin_b")
    session = GuestSession(provider=healthy, join_url="https://example.test/join")
    broken.get_active_guest_session.side_effect = RuntimeError("boom")
    healthy.get_active_guest_session.return_value = session
    mass = MagicMock(providers=[healthy, broken])

    assert await guest_access.get_active_guest_sessions(mass) == [session]


async def test_get_active_guest_sessions_times_out_stalled_plugin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stalled plugin cannot prevent a healthy plugin from returning its session."""
    stalled = _create_guest_session_plugin("plugin_a")
    healthy = _create_guest_session_plugin("plugin_b")
    session = GuestSession(provider=healthy, join_url="https://example.test/join")
    stalled.get_active_guest_session.side_effect = asyncio.Event().wait
    healthy.get_active_guest_session.return_value = session
    mass = MagicMock(providers=[stalled, healthy])
    monkeypatch.setattr(guest_access, "GUEST_SESSION_TIMEOUT", 0.01)

    assert await guest_access.get_active_guest_sessions(mass) == [session]


async def test_get_or_create_guest_user_returns_existing() -> None:
    """An existing guest user is returned without creating a new one."""
    mass = _create_mock_mass()
    existing_user = MagicMock(role=UserRole.GUEST)
    mass.webserver.auth.get_user_by_username.return_value = existing_user

    user = await guest_access.get_or_create_guest_user(mass, "party_guest", "Party Guest")

    assert user is existing_user
    mass.webserver.auth.create_user.assert_not_awaited()


async def test_get_or_create_guest_user_rejects_non_guest() -> None:
    """An existing user with a non-guest role is never reused for guest access."""
    mass = _create_mock_mass()
    mass.webserver.auth.get_user_by_username.return_value = MagicMock(role=UserRole.ADMIN)

    with pytest.raises(InvalidDataError):
        await guest_access.get_or_create_guest_user(mass, "party_guest", "Party Guest")
    mass.webserver.auth.create_user.assert_not_awaited()


async def test_get_or_create_guest_user_creates_guest() -> None:
    """A missing guest user is created with the GUEST role."""
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


def test_credential_owner_encodes_the_lifetime_policy() -> None:
    """The owner prefix says whether a credential is session-scoped or account-bound."""
    guest = MagicMock(role=UserRole.GUEST, user_id="g1")
    user = MagicMock(role=UserRole.USER, user_id="u1")
    assert guest_access.credential_owner(guest) == "guest-g1"
    assert guest_access.credential_owner(user) == "user-u1"
    assert guest_access.is_session_scoped_owner("guest-g1")
    assert not guest_access.is_session_scoped_owner("user-u1")
    assert guest_access.credential_owners_for_user_id("x") == ("guest-x", "user-x")


def test_credential_owner_user_id_resolves_both_prefixes() -> None:
    """An owner id resolves back to its account, and other owner kinds resolve to nothing."""
    assert guest_access.credential_owner_user_id("guest-g1") == "g1"
    assert guest_access.credential_owner_user_id("user-u1") == "u1"
    assert guest_access.credential_owner_user_id("token-t1") is None
