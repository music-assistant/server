"""Test Live Phish playback urls for quality, plan entitlement, and rate limits."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType, StreamType
from music_assistant_models.errors import AudioError, MediaNotFoundError, RateLimited

from music_assistant.providers.livephish import CONF_QUALITY, QUALITY_LOSSY, LivePhishProvider
from music_assistant.providers.livephish.constants import PLAYBACK_API
from tests.common import use_real_create_task

SUBSCRIPTION_BASE = {
    "startedAt": "08/01/2026 00:00:00",
    "endsAt": "09/01/2026 00:00:00",
    "legacySubscriptionId": "legacy-1",
}


def _logged_in(provider: LivePhishProvider) -> None:
    """Give the provider a token so playback does not start a login."""
    provider._auth_token = "test-token"
    provider._token_expiry = 10**12


def _stub_account_info(provider: LivePhishProvider, subscription: dict[str, Any]) -> None:
    """Stub the cached account accessor with the given subscription payload."""
    _logged_in(provider)
    provider._get_subscription_info = AsyncMock(  # type: ignore[method-assign]
        return_value=subscription
    )


def _install_cache_miss(provider: LivePhishProvider) -> None:
    """Make the @use_cache decorator treat every call as a cache miss."""
    cache: Any = provider.mass.cache
    cache.get_with_freshness = AsyncMock(return_value=(None, False, False))
    cache.set = AsyncMock()
    use_real_create_task(provider.mass)


def _stub_http_session(
    provider: LivePhishProvider,
    *,
    status: int = 200,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> MagicMock:
    """Stub the http session so the playback call returns one response."""
    response = AsyncMock()
    response.status = status
    response.headers = headers or {}
    response.json = AsyncMock(
        return_value=body if body is not None else {"url": "https://stream.test/track.m4a"}
    )
    get_ctx = AsyncMock()
    get_ctx.__aenter__ = AsyncMock(return_value=response)
    get_ctx.__aexit__ = AsyncMock(return_value=False)
    session_get = MagicMock(return_value=get_ctx)
    mass: Any = provider.mass
    mass.http_session.get = session_get
    return session_get


def _set_quality(provider: LivePhishProvider, quality: str) -> None:
    """Point the provider config at one stream quality."""
    config: Any = provider.config
    config.get_value.side_effect = lambda key, default=None: {
        "log_level": "GLOBAL",
        CONF_QUALITY: quality,
    }.get(key, default)


@pytest.mark.asyncio
async def test_stream_url_requests_the_playback_api(provider: LivePhishProvider) -> None:
    """A HiFi plan requests lossless and lossy together, by track id."""
    _stub_account_info(
        provider, {**SUBSCRIPTION_BASE, "plan": {"planId": "plan-42", "isHighQuality": True}}
    )
    session_get = _stub_http_session(provider)

    assert await provider._get_stream_url("7284") == "https://stream.test/track.m4a"

    assert session_get.call_args.args[0] == f"{PLAYBACK_API}/tracks/7284/url"
    assert session_get.call_args.kwargs["params"] == {
        "formats": "lossless,lossy",
        "forceFlac": "true",
    }
    assert session_get.call_args.kwargs["headers"]["Authorization"] == "Bearer test-token"
    assert session_get.call_count == 1


@pytest.mark.asyncio
async def test_stream_url_fetches_the_subscription_on_a_cache_miss(
    provider: LivePhishProvider,
) -> None:
    """Without a cached subscription the account is read before the playback url."""
    _install_cache_miss(provider)
    _logged_in(provider)
    subscription = {**SUBSCRIPTION_BASE, "plan": {"planId": "plan-42", "isHighQuality": True}}

    async def _fake(api: str, _endpoint: str, **_kwargs: Any) -> Any:
        return subscription if api == "subscription" else {"userId": 123}

    provider._get_data = AsyncMock(side_effect=_fake)  # type: ignore[method-assign]
    session_get = _stub_http_session(provider)

    assert await provider._get_stream_url("123") == "https://stream.test/track.m4a"
    assert session_get.call_args.kwargs["params"]["formats"] == "lossless,lossy"


@pytest.mark.asyncio
async def test_stream_url_reuses_cached_subscription(provider: LivePhishProvider) -> None:
    """A cached subscription is reused without asking Live Phish for the account again."""
    cached = {
        "_get_subscription_info": {
            **SUBSCRIPTION_BASE,
            "plan": {"planId": "plan-42", "isHighQuality": True},
        },
    }

    async def _cache_hit(key: str, **_kwargs: Any) -> tuple[Any, bool, bool]:
        return cached[key], True, True

    cache: Any = provider.mass.cache
    cache.get_with_freshness = AsyncMock(side_effect=_cache_hit)
    _logged_in(provider)
    api_mock = AsyncMock()
    provider._get_data = api_mock  # type: ignore[method-assign]
    _stub_http_session(provider)

    assert await provider._get_stream_url("123") == "https://stream.test/track.m4a"
    api_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_lossy_setting_requests_only_lossy(provider: LivePhishProvider) -> None:
    """The lossy setting does not ask for a lossless file."""
    _set_quality(provider, QUALITY_LOSSY)
    _stub_account_info(
        provider, {**SUBSCRIPTION_BASE, "plan": {"id": "plan-42", "isHighQuality": True}}
    )
    session_get = _stub_http_session(provider)

    await provider._get_stream_url("123")

    assert session_get.call_args.kwargs["params"]["formats"] == "lossy"
    assert session_get.call_count == 1


@pytest.mark.asyncio
async def test_lossless_without_hifi_requests_only_lossy(provider: LivePhishProvider) -> None:
    """A plan that is not HiFi is requested as lossy even when lossless is selected."""
    _stub_account_info(
        provider, {**SUBSCRIPTION_BASE, "plan": {"id": "plan-42", "isHighQuality": False}}
    )
    session_get = _stub_http_session(provider)

    await provider._get_stream_url("123")

    assert session_get.call_args.kwargs["params"]["formats"] == "lossy"


@pytest.mark.asyncio
async def test_stream_url_falls_back_to_promo_plan(provider: LivePhishProvider) -> None:
    """A trial subscription has plan set to None and its plan under promo."""
    _stub_account_info(
        provider,
        {
            **SUBSCRIPTION_BASE,
            "plan": None,
            "promo": {"plan": {"id": "promo-7", "isHighQuality": True}},
        },
    )
    session_get = _stub_http_session(provider)

    await provider._get_stream_url("123")

    assert session_get.call_args.kwargs["params"]["formats"] == "lossless,lossy"


@pytest.mark.asyncio
async def test_stream_url_without_any_plan_raises(provider: LivePhishProvider) -> None:
    """Without a regular or promo plan a clear audio error is raised."""
    _stub_account_info(provider, {**SUBSCRIPTION_BASE, "plan": None, "promo": None})

    with pytest.raises(AudioError):
        await provider._get_stream_url("123")


@pytest.mark.asyncio
async def test_subscription_list_uses_the_accessible_plan(provider: LivePhishProvider) -> None:
    """A v3 subscription list is reduced to the content-accessible plan before playback."""
    _install_cache_miss(provider)
    _logged_in(provider)
    accessible = {
        **SUBSCRIPTION_BASE,
        "plan": {"planId": "plan-42", "isHighQuality": True},
        "isContentAccessible": True,
    }
    other = {
        **SUBSCRIPTION_BASE,
        "legacySubscriptionId": "other",
        "plan": {"planId": "other", "isHighQuality": False},
        "isContentAccessible": False,
    }

    async def _fake(api: str, _endpoint: str, **_kwargs: Any) -> Any:
        return [other, accessible] if api == "subscription" else {"userId": 123}

    provider._get_data = AsyncMock(side_effect=_fake)  # type: ignore[method-assign]
    session_get = _stub_http_session(provider)

    await provider._get_stream_url("7284")

    assert session_get.call_args.kwargs["params"]["formats"] == "lossless,lossy"


@pytest.mark.asyncio
async def test_rate_limit_stops_playback(provider: LivePhishProvider) -> None:
    """A 429 raises RateLimited and does not send another playback request."""
    _stub_account_info(provider, {**SUBSCRIPTION_BASE, "plan": {"id": "p", "isHighQuality": True}})
    session_get = _stub_http_session(provider, status=429, body={}, headers={"Retry-After": "7"})

    with pytest.raises(RateLimited) as raised:
        await provider._get_stream_url("123")

    assert raised.value.backoff_time == 7
    assert session_get.call_count == 1


@pytest.mark.asyncio
async def test_forbidden_playback_is_not_retried(provider: LivePhishProvider) -> None:
    """A 403 is a missing stream, not a reason to ask for another format."""
    _stub_account_info(provider, {**SUBSCRIPTION_BASE, "plan": {"id": "p", "isHighQuality": True}})
    session_get = _stub_http_session(provider, status=403, body={"url": None, "access": False})

    with pytest.raises(MediaNotFoundError):
        await provider._get_stream_url("123")

    assert session_get.call_count == 1


@pytest.mark.asyncio
async def test_stream_url_without_any_stream_raises(provider: LivePhishProvider) -> None:
    """A playback response without a url raises media not found."""
    _stub_account_info(provider, {**SUBSCRIPTION_BASE, "plan": {"id": "p", "isHighQuality": True}})
    _stub_http_session(provider, body={"url": ""})

    with pytest.raises(MediaNotFoundError):
        await provider._get_stream_url("123")


@pytest.mark.asyncio
async def test_stream_details_keep_the_audio_address_on_the_stream(
    provider: LivePhishProvider,
) -> None:
    """The playable address is returned only as the HTTP stream path."""
    _stub_account_info(provider, {**SUBSCRIPTION_BASE, "plan": {"id": "p", "isHighQuality": True}})
    _stub_http_session(provider)

    details = await provider.get_stream_details("123", MediaType.TRACK)

    assert details.stream_type == StreamType.HTTP
    assert details.path == "https://stream.test/track.m4a"
    assert details.item_id == "123"
    assert details.provider == provider.instance_id


@pytest.mark.asyncio
async def test_stream_url_rewrites_the_player_cdn_host(provider: LivePhishProvider) -> None:
    """An edgesuite stream link is rewritten to the Akamai host before playback."""
    _stub_account_info(provider, {**SUBSCRIPTION_BASE, "plan": {"id": "p", "isHighQuality": True}})
    _stub_http_session(
        provider,
        body={"url": "http://nugs2.edgesuite.net/shows/504/8869/master.m3u8?hdnea=1"},
    )

    assert (
        await provider._get_stream_url("8869")
        == "https://securestream02.akamaized.net/shows/504/8869/master.m3u8?hdnea=1"
    )
