"""Tests for how the Spotify provider picks the session of an api request."""

from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.errors import LoginFailed, RetriesExhausted

from music_assistant.helpers.throttle_retry import RequestPriority, request_priority
from music_assistant.providers.spotify import provider as provider_module
from music_assistant.providers.spotify.constants import CONF_CLIENT_ID, CONF_REFRESH_TOKEN_DEV
from music_assistant.providers.spotify.provider import SpotifyProvider

INSTANCE_ID = "spotify--test"
GLOBAL_TOKEN = {"access_token": "global"}
DEV_TOKEN = {"access_token": "dev"}


@pytest.fixture(autouse=True)
def clear_throttlers() -> Generator[None]:
    """Start and end every test without a stored throttler, on a known shared client id."""
    provider_module._THROTTLERS.clear()
    with patch.object(provider_module, "app_var", return_value="shared-app"):
        yield
    provider_module._THROTTLERS.clear()


async def _make_provider() -> SpotifyProvider:
    """Return a loaded Spotify provider with a custom Client ID and a mocked http layer."""
    values = {CONF_CLIENT_ID: "client", CONF_REFRESH_TOKEN_DEV: "token"}
    provider = object.__new__(SpotifyProvider)
    provider.config = MagicMock(instance_id=INSTANCE_ID)
    provider.manifest = MagicMock(domain="spotify")
    provider.logger = MagicMock()
    provider.mass = MagicMock()
    provider.mass.cache_path = "/cache"
    provider.mass.metadata.locale = "en_US"
    provider._create_backend = MagicMock(  # type: ignore[method-assign]
        return_value=MagicMock(setup=AsyncMock(), unload=AsyncMock())
    )
    provider.login = AsyncMock(return_value=GLOBAL_TOKEN)  # type: ignore[method-assign]
    provider.login_dev = AsyncMock(return_value=DEV_TOKEN)  # type: ignore[method-assign]
    provider.get_setup_value = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda key, default=None: values.get(key, default)
    )
    provider._test_audiobook_support = AsyncMock(return_value=True)  # type: ignore[method-assign]
    provider._remove_unused_playback_credentials = MagicMock()  # type: ignore[method-assign]
    provider._sp_user = {"id": "user"}
    _stub_http(provider, {"id": "user"})
    await provider.handle_async_init()
    assert provider.dev_session_active
    # lift the rate limits, so the tests do not wait for a free slot
    for session in (provider._global_session, provider._dev_session):
        session.throttler.set_rate_limit(rate_limit=100, period=1)
    provider.logger.reset_mock()
    return provider


def _stub_http(provider: SpotifyProvider, payload: Any, status: int = 200) -> MagicMock:
    """
    Answer every api request of the provider with a canned response, return the request mock.

    :param provider: The provider whose requests to answer.
    :param payload: JSON body of the response.
    :param status: HTTP status of the response.
    """
    response = MagicMock(status=status, headers={})
    response.json = AsyncMock(return_value=payload)
    request = MagicMock(
        return_value=MagicMock(
            __aenter__=AsyncMock(return_value=response), __aexit__=AsyncMock(return_value=None)
        )
    )
    provider.mass.http_session.request = request  # type: ignore[method-assign]
    return request


def _tokens_used(request: MagicMock) -> list[str]:
    """Return the access token of every request made, in order."""
    return [call.kwargs["headers"]["Authorization"].split()[1] for call in request.call_args_list]


def _cached_tokens(provider: SpotifyProvider) -> tuple[Any, Any]:
    """Return the cached auth of the global and the dev session."""
    return provider._auth_info_global, provider._auth_info_dev


def _fallback_logs(provider: SpotifyProvider) -> int:
    """Return how often the fallback to the global session was logged."""
    info = provider.logger.info
    return sum("custom Client ID" in call.args[0] for call in info.call_args_list)  # type: ignore[attr-defined]


async def test_request_uses_the_dev_session() -> None:
    """With the dev session clear, a request goes to the custom Client ID."""
    provider = await _make_provider()
    request = _stub_http(provider, {})

    await provider._get_data("me/tracks")
    assert _tokens_used(request) == ["dev"]


async def test_request_forced_on_the_global_session() -> None:
    """A request that needs the global session goes to the shared client."""
    provider = await _make_provider()
    request = _stub_http(provider, {})

    await provider._get_data("playlists/abc", use_global_session=True)
    assert _tokens_used(request) == ["global"]


@pytest.mark.parametrize("priority", [RequestPriority.NORMAL, RequestPriority.HIGH])
async def test_long_dev_cooldown_moves_user_actions_to_global(priority: RequestPriority) -> None:
    """During a long ban of the custom Client ID user actions use the shared client."""
    provider = await _make_provider()
    provider._dev_session.throttler.set_cooldown(3600)
    request = _stub_http(provider, {})

    with request_priority(priority):
        await provider._get_data("me/tracks")
        await provider._put_data("me/tracks", {"ids": ["abc"]})
    assert _tokens_used(request) == ["global", "global"]
    assert _fallback_logs(provider) == 1


async def test_long_dev_cooldown_keeps_background_work_on_dev() -> None:
    """Background work stays on the banned custom Client ID and fails without a request."""
    provider = await _make_provider()
    provider._dev_session.throttler.set_cooldown(3600)
    request = _stub_http(provider, {})

    with request_priority(RequestPriority.LOW), pytest.raises(RetriesExhausted):
        await provider._get_data("me/tracks")
    request.assert_not_called()
    assert _fallback_logs(provider) == 0


async def test_short_dev_cooldown_keeps_the_dev_session() -> None:
    """A cooldown short enough to wait out does not move requests to the shared client."""
    provider = await _make_provider()
    provider._dev_session.throttler.set_cooldown(30)

    assert provider._session_for(use_global_session=False) is provider._dev_session
    assert _fallback_logs(provider) == 0


def _stub_http_per_token(provider: SpotifyProvider, statuses: dict[str, int]) -> MagicMock:
    """
    Answer requests with a status per access token, return the request mock.

    :param provider: The provider whose requests to answer.
    :param statuses: HTTP status per access token, 429 answers carry an hour long Retry-After.
    """

    def _respond(_method: str, _url: str, headers: dict[str, str], **_kwargs: Any) -> MagicMock:
        status = statuses[headers["Authorization"].split()[1]]
        response = MagicMock(
            status=status, headers={"Retry-After": "3600"} if status == 429 else {}
        )
        response.json = AsyncMock(return_value={})
        return MagicMock(
            __aenter__=AsyncMock(return_value=response), __aexit__=AsyncMock(return_value=None)
        )

    request = MagicMock(side_effect=_respond)
    provider.mass.http_session.request = request  # type: ignore[method-assign]
    return request


@pytest.mark.parametrize("priority", [RequestPriority.NORMAL, RequestPriority.HIGH])
async def test_request_that_runs_into_a_long_limit_falls_back(priority: RequestPriority) -> None:
    """The user action that meets the ban of the custom Client ID is served by the shared client."""
    provider = await _make_provider()
    request = _stub_http_per_token(provider, {"dev": 429, "global": 200})

    with request_priority(priority):
        await provider._get_data("me/tracks")
    assert _tokens_used(request) == ["dev", "global"]
    assert provider._dev_session.throttler.cooldown_remaining > 0
    assert _fallback_logs(provider) == 1


async def test_background_work_that_runs_into_a_long_limit_fails() -> None:
    """Background work that meets the ban fails, the shared client is not for it."""
    provider = await _make_provider()
    request = _stub_http_per_token(provider, {"dev": 429, "global": 200})

    with request_priority(RequestPriority.LOW), pytest.raises(RetriesExhausted):
        await provider._get_data("me/tracks")
    assert _tokens_used(request) == ["dev"]


def _stub_http_sequence(provider: SpotifyProvider, dev_statuses: list[int]) -> MagicMock:
    """
    Answer dev requests with a sequence of statuses and shared requests with 200.

    :param provider: The provider whose requests to answer.
    :param dev_statuses: HTTP status of each dev request in order, 429 carries a 5 s Retry-After.
    """
    remaining = list(dev_statuses)

    def _respond(_method: str, _url: str, headers: dict[str, str], **_kwargs: Any) -> MagicMock:
        status = remaining.pop(0) if headers["Authorization"].endswith("dev") else 200
        response = MagicMock(status=status, headers={"Retry-After": "5"} if status == 429 else {})
        response.json = AsyncMock(return_value={})
        return MagicMock(
            __aenter__=AsyncMock(return_value=response), __aexit__=AsyncMock(return_value=None)
        )

    request = MagicMock(side_effect=_respond)
    provider.mass.http_session.request = request  # type: ignore[method-assign]
    return request


async def test_playback_that_meets_a_short_limit_switches_at_once() -> None:
    """Playback that meets even a short limit of the custom Client ID switches without waiting."""
    provider = await _make_provider()
    request = _stub_http_sequence(provider, [429])

    with (
        patch("music_assistant.helpers.throttle_retry.asyncio.sleep", AsyncMock()) as sleep,
        request_priority(RequestPriority.HIGH),
    ):
        await provider._get_data("tracks/abc")
    assert _tokens_used(request) == ["dev", "global"]
    assert provider._dev_session.throttler.cooldown_remaining > 0
    sleep.assert_not_awaited()


async def test_user_action_that_meets_a_short_limit_waits_and_retries() -> None:
    """A user action sits out a short limit of the custom Client ID and retries there."""
    provider = await _make_provider()
    request = _stub_http_sequence(provider, [429, 200])

    with patch("music_assistant.helpers.throttle_retry.asyncio.sleep", AsyncMock()) as sleep:
        await provider._get_data("me/tracks")
    assert _tokens_used(request) == ["dev", "dev"]
    assert sleep.await_count == 1
    assert 5 <= sleep.await_args_list[0].args[0] <= 5.5


async def test_playback_leaves_a_limited_dev_session_at_once() -> None:
    """Playback takes the shared client during any limit of the custom Client ID, however short."""
    provider = await _make_provider()
    provider._dev_session.throttler.set_cooldown(30)
    request = _stub_http(provider, {})

    with request_priority(RequestPriority.HIGH):
        await provider._get_data("tracks/abc")
    assert _tokens_used(request) == ["global"]


async def test_failed_dev_login_falls_back_to_global() -> None:
    """A request whose dev login fails is served by the global session instead."""
    provider = await _make_provider()
    request = _stub_http(provider, {"id": "abc"})

    async def failing_login_dev(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        provider.dev_session_active = False
        raise LoginFailed("revoked")

    provider.login_dev.side_effect = failing_login_dev  # type: ignore[attr-defined]

    assert (await provider._get_data("me/tracks"))["id"] == "abc"
    assert _tokens_used(request) == ["global"]
    assert not provider.dev_session_active


@pytest.mark.parametrize("use_global_session", [False, True])
async def test_unauthorized_clears_only_its_own_token(use_global_session: bool) -> None:
    """A rejected token drops the cached auth of the session that used it, not the other."""
    provider = await _make_provider()
    provider._auth_info_global = GLOBAL_TOKEN
    provider._auth_info_dev = DEV_TOKEN
    for session in (provider._global_session, provider._dev_session):
        # a single attempt, so the test does not sit out the retry backoff
        session.throttler.retry_attempts = 1
    _stub_http(provider, {}, status=401)

    with pytest.raises(RetriesExhausted):
        await provider._get_data("me/tracks", use_global_session=use_global_session)
    expected = (None, DEV_TOKEN) if use_global_session else (GLOBAL_TOKEN, None)
    assert _cached_tokens(provider) == expected
