"""
Tests for the shared player-access rule.

A user with a non-empty player_filter may only use the players in it; an empty filter, or
the full-access Scope.ALL, leaves the user unrestricted. A user may always use the private
client player they connected on, even when it is not in their filter.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace

from music_assistant_models.auth import User, UserRole

from music_assistant.controllers.webserver.helpers.auth_middleware import (
    has_player_access,
    player_access_filter,
    sendspin_player_id,
)

ALLOWED_PLAYER = "kitchen"
OTHER_PLAYER = "living_room"
OWN_CLIENT = "browser_session_1"


def _user(role: UserRole, player_filter: list[str]) -> User:
    """Build a user with the given role and player filter."""
    return User(user_id="user_1", username="tester", role=role, player_filter=player_filter)


def _player(player_id: str, *, private: bool) -> SimpleNamespace:
    """Build a stand-in player carrying just what the access rule reads."""
    return SimpleNamespace(player_id=player_id, private=private)


@contextmanager
def _connected_on(player_id: str | None) -> Iterator[None]:
    """Run the block as a caller connected on the given private client player."""
    token = sendspin_player_id.set(player_id)
    try:
        yield
    finally:
        sendspin_player_id.reset(token)


def test_unauthenticated_caller_is_unrestricted() -> None:
    """A caller without a user may use any player."""
    assert player_access_filter(None) is None
    assert has_player_access(None, OTHER_PLAYER)


def test_empty_filter_is_unrestricted() -> None:
    """A user with an empty player filter may use any player."""
    user = _user(UserRole.USER, [])
    assert player_access_filter(user) is None
    assert has_player_access(user, OTHER_PLAYER)


def test_full_access_role_ignores_the_filter() -> None:
    """A Scope.ALL role is unrestricted even when a player filter is set."""
    admin = _user(UserRole.ADMIN, [ALLOWED_PLAYER])
    assert player_access_filter(admin) is None
    assert has_player_access(admin, OTHER_PLAYER)


def test_restricted_user_is_limited_to_the_filter() -> None:
    """A restricted user may use the players in the filter and no others."""
    user = _user(UserRole.USER, [ALLOWED_PLAYER])
    assert player_access_filter(user) == [ALLOWED_PLAYER]
    assert has_player_access(user, ALLOWED_PLAYER)
    assert not has_player_access(user, OTHER_PLAYER)
    assert not has_player_access(user, OTHER_PLAYER, _player(OTHER_PLAYER, private=False))


def test_own_client_player_is_permitted() -> None:
    """The private client player the user connected on is usable even when filtered out."""
    user = _user(UserRole.USER, [ALLOWED_PLAYER])
    with _connected_on(OWN_CLIENT):
        assert has_player_access(user, OWN_CLIENT, _player(OWN_CLIENT, private=True))


def test_shared_speaker_claimed_as_the_client_player_is_refused() -> None:
    """Announcing a shared speaker's id as the client id does not grant access to it."""
    user = _user(UserRole.USER, [ALLOWED_PLAYER])
    with _connected_on(OTHER_PLAYER):
        assert not has_player_access(user, OTHER_PLAYER, _player(OTHER_PLAYER, private=False))
