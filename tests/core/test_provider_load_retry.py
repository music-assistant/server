"""Tests for provider load retry decisions."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.errors import (
    AuthenticationFailed,
    AuthenticationRequired,
    InvalidToken,
    LoginFailed,
    MusicAssistantError,
    ProviderUnavailableError,
)

from music_assistant.mass import MusicAssistant


def _mass_with_load_error(error: MusicAssistantError) -> MusicAssistant:
    """Return a minimal MusicAssistant whose provider load raises the given error."""
    mass = object.__new__(MusicAssistant)
    provider_config = MagicMock(enabled=True, instance_id="test--instance", name="Test")
    mass.config = MagicMock()
    mass.config.get_provider_config = AsyncMock(return_value=provider_config)
    mass.load_provider_config = AsyncMock(side_effect=error)  # type: ignore[method-assign]
    mass.call_later = MagicMock()  # type: ignore[method-assign]
    mass._tracked_timers = {}
    return mass


@pytest.mark.parametrize(
    "error",
    [
        AuthenticationRequired("Authentication required"),
        AuthenticationFailed("Authentication failed"),
        LoginFailed("Login failed"),
        InvalidToken("Invalid token"),
    ],
)
async def test_authentication_load_errors_are_not_retried(error: MusicAssistantError) -> None:
    """Provider authentication failures wait for reconfiguration instead of retrying."""
    mass = _mass_with_load_error(error)

    await mass.load_provider("test--instance", allow_retry=True)

    cast("MagicMock", mass.call_later).assert_not_called()


async def test_transient_handled_load_error_is_retried() -> None:
    """A transient handled provider failure still schedules a delayed retry."""
    mass = _mass_with_load_error(ProviderUnavailableError("Temporarily unavailable"))

    await mass.load_provider("test--instance", allow_retry=True)

    cast("MagicMock", mass.call_later).assert_called_once_with(
        120,
        mass.load_provider,
        "test--instance",
        True,
        task_id="load_provider_test--instance",
    )
