"""
Regression tests for automatic provider (re)load retries.

``load_provider`` scheduled a retry 120 seconds later for any handled
``MusicAssistantError``, including authentication failures. Those require the user to
reconfigure the provider, so retrying on a timer never resolved them and only spammed
the log every 120 seconds. Authentication-class errors must not be retried, while other
handled errors keep retrying as before.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from music_assistant_models.config_entries import ProviderConfig
from music_assistant_models.enums import ProviderType
from music_assistant_models.errors import (
    AuthenticationFailed,
    AuthenticationRequired,
    InvalidToken,
    LoginFailed,
    ProviderUnavailableError,
)

from music_assistant.mass import MusicAssistant

INSTANCE_ID = "spotify--test"


@pytest.mark.parametrize(
    "exc",
    [
        AuthenticationRequired("token expired"),
        AuthenticationFailed("bad credentials"),
        LoginFailed("login failed"),
        InvalidToken("token invalid"),
    ],
)
async def test_authentication_errors_are_not_retried(
    mass_minimal: MusicAssistant, exc: Exception
) -> None:
    """An authentication-class load failure does not schedule a retry timer."""
    await _load_provider_and_raise(mass_minimal, exc)
    assert f"load_provider_{INSTANCE_ID}" not in mass_minimal._tracked_timers


async def test_transient_error_still_retries(mass_minimal: MusicAssistant) -> None:
    """A genuinely transient handled error still schedules a retry timer."""
    await _load_provider_and_raise(mass_minimal, ProviderUnavailableError("temporarily down"))
    assert f"load_provider_{INSTANCE_ID}" in mass_minimal._tracked_timers


async def _load_provider_and_raise(mass_minimal: MusicAssistant, exc: Exception) -> None:
    """Run load_provider(allow_retry=True) with load_provider_config raising the given error."""
    prov_conf = ProviderConfig(
        values={},
        type=ProviderType.MUSIC,
        domain="spotify",
        instance_id=INSTANCE_ID,
        enabled=True,
    )
    with (
        patch.object(mass_minimal.config, "get_provider_config", AsyncMock(return_value=prov_conf)),
        patch.object(mass_minimal, "load_provider_config", AsyncMock(side_effect=exc)),
    ):
        await mass_minimal.load_provider(INSTANCE_ID, allow_retry=True)
