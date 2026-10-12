"""Fixtures for testing the AI Radio plugin provider."""

from __future__ import annotations

from collections.abc import Generator

import pytest
from music_assistant_models.auth import User, UserRole

from music_assistant.controllers.webserver.helpers.auth_middleware import set_current_user


@pytest.fixture
def kitchen_only_user() -> Generator[None]:
    """Make the calling user a member that only has access to the kitchen player."""
    set_current_user(
        User(user_id="kid", username="kid", role=UserRole.USER, player_filter=["kitchen"])
    )
    yield
    set_current_user(None)
