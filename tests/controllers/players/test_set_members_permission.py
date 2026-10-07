"""Tests that changing group members honors the player filter of the calling user."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.errors import InsufficientPermissions

from music_assistant.controllers.players import PlayerController
from music_assistant.controllers.webserver.helpers.auth_middleware import current_user

ALLOWED_PLAYERS = ["leader", "member"]


@contextmanager
def _restricted_user() -> Iterator[None]:
    """Run the block as a user limited to the allowed players."""
    token = current_user.set(
        User(
            user_id="user_1",
            username="restricted",
            role=UserRole.USER,
            player_filter=ALLOWED_PLAYERS,
        )
    )
    try:
        yield
    finally:
        current_user.reset(token)


@pytest.fixture
def controller() -> PlayerController:
    """Create a PlayerController whose member handling is replaced by a mock."""
    mass = MagicMock()
    mass.config.get_raw_core_config_value = MagicMock(return_value="GLOBAL")
    controller = PlayerController(mass)
    controller.cmd_set_members = AsyncMock()  # type: ignore[method-assign]
    return controller


@pytest.mark.parametrize(
    ("target_player", "player_ids_to_add", "player_ids_to_remove"),
    [
        ("other", ["member"], None),
        ("leader", ["other"], None),
        ("leader", None, ["other"]),
    ],
)
async def test_set_members_with_a_player_outside_the_filter_is_refused(
    controller: PlayerController,
    target_player: str,
    player_ids_to_add: list[str] | None,
    player_ids_to_remove: list[str] | None,
) -> None:
    """The target and every member to add or remove must be inside the filter."""
    with _restricted_user(), pytest.raises(InsufficientPermissions):
        await controller.cmd_set_members_for_api(
            target_player, player_ids_to_add, player_ids_to_remove
        )
    controller.cmd_set_members.assert_not_awaited()  # type: ignore[attr-defined]
