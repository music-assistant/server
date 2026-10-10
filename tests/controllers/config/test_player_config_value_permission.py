"""Tests that reading a player config value honors the player filter of the calling user."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.errors import InsufficientPermissions

from music_assistant.controllers.webserver.helpers.auth_middleware import current_user
from tests.controllers.config.helpers import build_bare_config_controller


async def test_a_player_outside_the_filter_is_refused() -> None:
    """A restricted user cannot read the config of a player outside their filter."""
    mock_mass = MagicMock()
    mock_mass.players.get_player.return_value = None
    controller = build_bare_config_controller(mock_mass)
    token = current_user.set(
        User(user_id="user_1", username="restricted", role=UserRole.USER, player_filter=["a"])
    )
    try:
        with pytest.raises(InsufficientPermissions):
            await controller.get_player_config_value("b", "volume_normalization")
    finally:
        current_user.reset(token)
