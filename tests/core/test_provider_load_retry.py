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

from music_assistant.mass import PROVIDER_RETRY_JITTER, MusicAssistant


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

    call = cast("MagicMock", mass.call_later).call_args
    assert call.args[1:] == (mass.load_provider, "test--instance", True)
    assert call.kwargs == {"retry_attempt": 1, "task_id": "load_provider_test--instance"}


@pytest.mark.parametrize(
    ("retry_attempt", "expected_delay"),
    [(0, 10), (1, 30), (2, 60), (3, 120), (9, 120)],
)
async def test_retry_delay_grows_with_each_failed_attempt(
    retry_attempt: int, expected_delay: int
) -> None:
    """Retries come quickly at first and then settle on the slowest interval."""
    mass = _mass_with_load_error(ProviderUnavailableError("Temporarily unavailable"))

    await mass.load_provider("test--instance", allow_retry=True, retry_attempt=retry_attempt)

    call = cast("MagicMock", mass.call_later).call_args
    assert call.args[0] == pytest.approx(expected_delay, abs=PROVIDER_RETRY_JITTER)
    assert call.kwargs["retry_attempt"] == retry_attempt + 1


async def test_retries_are_jittered() -> None:
    """Providers that failed together must not all retry in the same instant."""
    mass = _mass_with_load_error(ProviderUnavailableError("Temporarily unavailable"))

    for _ in range(10):
        await mass.load_provider("test--instance", allow_retry=True)

    delays = {call.args[0] for call in cast("MagicMock", mass.call_later).call_args_list}
    assert len(delays) > 1
    assert all(
        10 - PROVIDER_RETRY_JITTER <= delay <= 10 + PROVIDER_RETRY_JITTER for delay in delays
    )
