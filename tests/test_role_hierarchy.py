"""Tests for the role hierarchy used by API command access control."""

import pytest
from music_assistant_models.auth import UserRole

from music_assistant.helpers.api import ROLE_LEVELS, has_required_role


def test_role_levels_ordering() -> None:
    """Guest is the lowest privileged role, admin the highest."""
    assert ROLE_LEVELS[UserRole.GUEST] < ROLE_LEVELS[UserRole.USER]
    assert ROLE_LEVELS[UserRole.USER] < ROLE_LEVELS[UserRole.ADMIN]


@pytest.mark.parametrize(
    ("user_role", "required_role", "expected"),
    [
        # required_role=None: any authenticated user (including guest) is allowed
        (UserRole.GUEST, None, True),
        (UserRole.USER, None, True),
        (UserRole.ADMIN, None, True),
        # required_role="user": guest rejected, user and admin allowed
        (UserRole.GUEST, "user", False),
        (UserRole.USER, "user", True),
        (UserRole.ADMIN, "user", True),
        # required_role="admin": only admin allowed
        (UserRole.GUEST, "admin", False),
        (UserRole.USER, "admin", False),
        (UserRole.ADMIN, "admin", True),
    ],
)
def test_has_required_role(user_role: UserRole, required_role: str | None, expected: bool) -> None:
    """A user's role must be at least the required role level to be granted access."""
    assert has_required_role(user_role, required_role) is expected
