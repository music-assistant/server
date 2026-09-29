"""Tests for the Spotify throttler across (re)loads of the provider."""

from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.errors import RetriesExhausted

from music_assistant.helpers.throttle_retry import ThrottlerManager
from music_assistant.providers.spotify import provider as provider_module
from music_assistant.providers.spotify.constants import CONF_CLIENT_ID, CONF_REFRESH_TOKEN_DEV
from music_assistant.providers.spotify.provider import SpotifyProvider

INSTANCE_ID = "spotify--test"


def _make_provider(setup_values: dict[str, Any] | None = None) -> SpotifyProvider:
    """
    Return a Spotify provider with only what handle_async_init needs.

    :param setup_values: Values the setup flow collected, empty when omitted.
    """
    values = setup_values or {}
    provider = object.__new__(SpotifyProvider)
    provider.config = MagicMock(instance_id=INSTANCE_ID)
    provider.manifest = MagicMock(domain="spotify")
    provider.logger = MagicMock()
    provider.mass = MagicMock()
    provider.mass.cache_path = "/cache"
    provider._create_backend = MagicMock(  # type: ignore[method-assign]
        return_value=MagicMock(setup=AsyncMock(), unload=AsyncMock())
    )
    provider.login = AsyncMock(return_value={})  # type: ignore[method-assign]
    provider.login_dev = AsyncMock(return_value={})  # type: ignore[method-assign]
    provider.get_setup_value = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda key, default=None: values.get(key, default)
    )
    provider._test_audiobook_support = AsyncMock(return_value=True)  # type: ignore[method-assign]
    provider._remove_unused_playback_credentials = MagicMock()  # type: ignore[method-assign]
    provider._remove_login_material = MagicMock()  # type: ignore[method-assign]
    return provider


def _stored_throttler() -> ThrottlerManager:
    """Return the throttler kept for the test instance, created on first use."""
    return provider_module._THROTTLERS.setdefault(
        INSTANCE_ID, ThrottlerManager(rate_limit=1, period=2)
    )


@pytest.fixture(autouse=True)
def clear_throttlers() -> Generator[None]:
    """Start and end every test without a stored throttler."""
    provider_module._THROTTLERS.clear()
    yield
    provider_module._THROTTLERS.clear()


async def test_reload_keeps_the_throttler_and_its_cooldown() -> None:
    """A new provider object of the same instance gets the same throttler with its cooldown."""
    first = _make_provider()
    await first.handle_async_init()
    first.throttler.set_cooldown(3600)

    second = _make_provider()
    await second.handle_async_init()
    assert second.throttler is first.throttler
    assert second.throttler.cooldown_remaining > 3500


async def test_default_rate_limit_without_custom_client_id() -> None:
    """Without a custom Client ID the throttler runs on 1 request per 2 seconds."""
    _stored_throttler().set_rate_limit(rate_limit=30, period=30)

    provider = _make_provider()
    await provider.handle_async_init()
    assert provider.throttler.throttler.rate_limit == 1
    assert provider.throttler.throttler.period == 2
    assert not provider.dev_session_active


async def test_loose_rate_limit_with_custom_client_id() -> None:
    """With a custom Client ID the throttler runs on 30 requests per 30 seconds."""
    provider = _make_provider({CONF_CLIENT_ID: "client", CONF_REFRESH_TOKEN_DEV: "token"})
    provider._sp_user = {"id": "user"}
    provider._get_data = AsyncMock(return_value={"id": "user"})  # type: ignore[method-assign]

    await provider.handle_async_init()
    assert provider.throttler.throttler.rate_limit == 30
    assert provider.throttler.throttler.period == 30
    assert provider.dev_session_active


async def test_load_during_a_long_cooldown_fails_without_a_request() -> None:
    """A load during a long cooldown fails without sending a request to Spotify."""
    _stored_throttler().set_cooldown(3600)
    provider = _make_provider()
    # drop the stub, so the load goes through the real audiobook check and api call
    del provider._test_audiobook_support
    provider.mass.http_session.get = MagicMock()  # type: ignore[method-assign]

    with pytest.raises(RetriesExhausted):
        await provider.handle_async_init()
    provider.mass.http_session.get.assert_not_called()
    provider.backend.unload.assert_awaited_once()  # type: ignore[attr-defined]


async def test_removed_instance_drops_its_throttler() -> None:
    """Removing an instance drops its throttler, so a new one starts without a cooldown."""
    provider = _make_provider()
    await provider.handle_async_init()
    provider.throttler.set_cooldown(3600)

    await provider.unload(is_removed=True)
    assert INSTANCE_ID not in provider_module._THROTTLERS

    new_provider = _make_provider()
    await new_provider.handle_async_init()
    assert new_provider.throttler.cooldown_remaining == 0


async def test_unloaded_instance_keeps_its_throttler() -> None:
    """Unloading an instance that is not removed keeps its throttler and cooldown."""
    provider = _make_provider()
    await provider.handle_async_init()
    provider.throttler.set_cooldown(3600)

    await provider.unload(is_removed=False)
    assert provider_module._THROTTLERS[INSTANCE_ID] is provider.throttler

    new_provider = _make_provider()
    await new_provider.handle_async_init()
    assert new_provider.throttler.cooldown_remaining > 3500
