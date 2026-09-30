"""Tests for the Spotify session throttlers across (re)loads of the provider."""

from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.errors import RetriesExhausted

from music_assistant.helpers.throttle_retry import ThrottlerManager
from music_assistant.providers.spotify import provider as provider_module
from music_assistant.providers.spotify.constants import CONF_CLIENT_ID, CONF_REFRESH_TOKEN_DEV
from music_assistant.providers.spotify.provider import SpotifyProvider

INSTANCE_ID = "spotify--test"
SHARED_CLIENT_ID = "shared-app"


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


def _stored_throttler(
    session: str = "global", client_id: str = SHARED_CLIENT_ID
) -> ThrottlerManager:
    """
    Return the throttler kept for a session of the test instance, created on first use.

    :param session: Name of the session, global or dev.
    :param client_id: The Spotify app of the session.
    """
    return provider_module._THROTTLERS.setdefault(
        (INSTANCE_ID, session, client_id), ThrottlerManager(rate_limit=1, period=2)
    )


def _throttlers(provider: SpotifyProvider) -> tuple[ThrottlerManager, ThrottlerManager]:
    """Return the throttlers of the global and the dev session of the provider."""
    return provider._global_session.throttler, provider._dev_session.throttler


@pytest.fixture(autouse=True)
def clear_throttlers() -> Generator[None]:
    """Start and end every test without a stored throttler, on a known shared client id."""
    provider_module._THROTTLERS.clear()
    with patch.object(provider_module, "app_var", return_value=SHARED_CLIENT_ID):
        yield
    provider_module._THROTTLERS.clear()


async def test_reload_keeps_the_throttlers_and_their_cooldowns() -> None:
    """A new provider object of the same instance gets the same throttlers with their cooldowns."""
    first = _make_provider()
    await first.handle_async_init()
    first_global, first_dev = _throttlers(first)
    first_global.set_cooldown(3600)
    first_dev.set_cooldown(1800)

    second = _make_provider()
    await second.handle_async_init()
    second_global, second_dev = _throttlers(second)
    assert second_global is first_global
    assert second_dev is first_dev
    assert second_global.cooldown_remaining > 3500
    assert 1700 < second_dev.cooldown_remaining <= 1800


async def test_default_rate_limit_without_custom_client_id() -> None:
    """Without a custom Client ID the global throttler runs on 1 request per 2 seconds."""
    _stored_throttler().set_rate_limit(rate_limit=30, period=30)

    provider = _make_provider()
    await provider.handle_async_init()
    global_throttler, _ = _throttlers(provider)
    assert global_throttler.throttler.rate_limit == 1
    assert global_throttler.throttler.period == 2
    assert not provider.dev_session_active


async def test_loose_rate_limit_with_custom_client_id() -> None:
    """With a custom Client ID only the dev throttler runs on 30 requests per 30 seconds."""
    provider = _make_provider({CONF_CLIENT_ID: "client", CONF_REFRESH_TOKEN_DEV: "token"})
    provider._sp_user = {"id": "user"}
    provider._get_data = AsyncMock(return_value={"id": "user"})  # type: ignore[method-assign]

    await provider.handle_async_init()
    global_throttler, dev_throttler = _throttlers(provider)
    assert dev_throttler.throttler.rate_limit == 30
    assert dev_throttler.throttler.period == 30
    assert global_throttler.throttler.rate_limit == 1
    assert global_throttler.throttler.period == 2
    assert provider.dev_session_active


async def test_load_during_a_long_cooldown_fails_without_a_request() -> None:
    """A load during a long cooldown fails without sending a request to Spotify."""
    _stored_throttler().set_cooldown(3600)
    provider = _make_provider()
    # drop the stub, so the load goes through the real audiobook check and api call
    del provider._test_audiobook_support
    provider.mass.http_session.request = MagicMock()  # type: ignore[method-assign]

    with pytest.raises(RetriesExhausted):
        await provider.handle_async_init()
    provider.mass.http_session.request.assert_not_called()
    provider.backend.unload.assert_awaited_once()  # type: ignore[attr-defined]


async def test_changed_client_id_starts_without_the_old_cooldown() -> None:
    """A new custom Client ID is another Spotify app, so it does not inherit the old app's limit."""
    provider = _make_provider({CONF_CLIENT_ID: "client-a", CONF_REFRESH_TOKEN_DEV: "token"})
    provider._sp_user = {"id": "user"}
    provider._get_data = AsyncMock(return_value={"id": "user"})  # type: ignore[method-assign]
    await provider.handle_async_init()
    global_throttler, old_dev_throttler = _throttlers(provider)
    global_throttler.set_cooldown(3600)
    old_dev_throttler.set_cooldown(3600)

    reconfigured = _make_provider({CONF_CLIENT_ID: "client-b", CONF_REFRESH_TOKEN_DEV: "token"})
    reconfigured._sp_user = {"id": "user"}
    reconfigured._get_data = AsyncMock(return_value={"id": "user"})  # type: ignore[method-assign]
    await reconfigured.handle_async_init()
    new_global_throttler, new_dev_throttler = _throttlers(reconfigured)

    assert new_global_throttler is global_throttler
    assert new_dev_throttler is not old_dev_throttler
    assert new_dev_throttler.cooldown_remaining == 0
    # the previous app keeps its limit, a rollback of the reconfiguration lands on it again
    assert provider_module._THROTTLERS[(INSTANCE_ID, "dev", "client-a")] is old_dev_throttler


async def test_removed_instance_drops_its_throttlers() -> None:
    """Removing an instance drops both throttlers, so a new one starts without a cooldown."""
    provider = _make_provider()
    await provider.handle_async_init()
    for throttler in _throttlers(provider):
        throttler.set_cooldown(3600)

    await provider.unload(is_removed=True)
    assert not provider_module._THROTTLERS

    new_provider = _make_provider()
    await new_provider.handle_async_init()
    assert all(throttler.cooldown_remaining == 0 for throttler in _throttlers(new_provider))


async def test_unloaded_instance_keeps_its_throttlers() -> None:
    """Unloading an instance that is not removed keeps both throttlers and their cooldowns."""
    provider = _make_provider()
    await provider.handle_async_init()
    global_throttler, dev_throttler = _throttlers(provider)
    for throttler in (global_throttler, dev_throttler):
        throttler.set_cooldown(3600)

    await provider.unload(is_removed=False)
    assert (
        provider_module._THROTTLERS[(INSTANCE_ID, "global", SHARED_CLIENT_ID)] is global_throttler
    )
    assert provider_module._THROTTLERS[(INSTANCE_ID, "dev", "")] is dev_throttler

    new_provider = _make_provider()
    await new_provider.handle_async_init()
    assert all(throttler.cooldown_remaining > 3500 for throttler in _throttlers(new_provider))
