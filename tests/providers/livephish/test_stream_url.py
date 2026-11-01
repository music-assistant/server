"""Test Live Phish stream url building for regular, promo/trial and inactive subscriptions."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType, StreamType
from music_assistant_models.errors import AudioError, MediaNotFoundError

from music_assistant.providers.livephish import LivePhishProvider
from music_assistant.providers.livephish.constants import STREAM_API_URL
from tests.common import use_real_create_task

USER_ID = "user-1"
SUBSCRIPTION_BASE = {
    "startedAt": "08/01/2026 00:00:00",
    "endsAt": "09/01/2026 00:00:00",
    "legacySubscriptionId": "legacy-1",
}


def _stub_show(provider: LivePhishProvider, show_id: str = "435") -> None:
    """Stub the cached track so the stream request can read its show id."""
    provider.get_track = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(album=SimpleNamespace(item_id=show_id))
    )
    cache: Any = provider.mass.cache
    cache.get = AsyncMock(return_value=None)
    provider._sku_from_show = AsyncMock(return_value=None)  # type: ignore[method-assign]


def _stub_account_info(provider: LivePhishProvider, subscription: dict[str, Any]) -> None:
    """Stub the cached account accessors with the given subscription payload."""
    _stub_show(provider)
    provider._get_subscription_info = AsyncMock(  # type: ignore[method-assign]
        return_value=subscription
    )
    provider._get_user_id = AsyncMock(return_value=USER_ID)  # type: ignore[method-assign]


def _install_cache_miss(provider: LivePhishProvider) -> None:
    """Make the @use_cache decorator treat every call as a cache miss."""
    cache: Any = provider.mass.cache
    cache.get_with_freshness = AsyncMock(return_value=(None, False, False))
    cache.set = AsyncMock()
    use_real_create_task(provider.mass)


def _stub_http_session(provider: LivePhishProvider, *bodies: str) -> MagicMock:
    """Stub the http session so subplayer calls return the given bodies in order."""
    bodies = bodies or ('{"streamLink": "https://stream.test/track.m3u8"}',)
    contexts = []
    for body in bodies:
        response = AsyncMock()
        response.raise_for_status = lambda: None
        response.text = AsyncMock(return_value=body)
        get_ctx = AsyncMock()
        get_ctx.__aenter__ = AsyncMock(return_value=response)
        get_ctx.__aexit__ = AsyncMock(return_value=False)
        contexts.append(get_ctx)
    session_get = MagicMock(side_effect=contexts)
    mass: Any = provider.mass
    mass.http_session.get = session_get
    return session_get


def _assert_player_request(params: dict[str, Any], track_id: str = "123") -> None:
    """Assert the stream call matches the player's subplayer request."""
    assert params["skuID"] == track_id
    assert params["containerID"] == "435"
    assert params["app"] == 1
    assert params["v"] == 2
    assert params["tk"]
    assert params["lxp"].endswith(".000000")
    assert "platformID" not in params
    assert "trackId" not in params
    assert "method" not in params


@pytest.mark.asyncio
async def test_stream_url_fetches_account_info_on_cache_miss(provider: LivePhishProvider) -> None:
    """Without cached account details the subscription and user id are read from Live Phish."""
    _install_cache_miss(provider)
    _stub_show(provider)
    subscription = {**SUBSCRIPTION_BASE, "plan": {"planId": "plan-42", "isHighQuality": True}}

    async def _fake(api: str, _endpoint: str, **_kwargs: Any) -> Any:
        return subscription if api == "subscription" else {"userId": 123}

    provider._get_data = AsyncMock(side_effect=_fake)  # type: ignore[method-assign]
    session_get = _stub_http_session(provider)

    assert await provider._get_stream_url("123") == "https://stream.test/track.m3u8"
    params = session_get.call_args.kwargs["params"]
    assert params["subCostplanIDAccessList"] == "plan-42"
    assert params["nn_userID"] == "123"
    assert session_get.call_args.args[0] == STREAM_API_URL
    _assert_player_request(params)


@pytest.mark.asyncio
async def test_stream_url_reuses_cached_account_info(provider: LivePhishProvider) -> None:
    """Cached subscription and user details are reused without asking Live Phish again."""
    cached = {
        "_get_subscription_info": {
            **SUBSCRIPTION_BASE,
            "plan": {"planId": "plan-42", "isHighQuality": True},
        },
        "_get_user_id": USER_ID,
    }

    async def _cache_hit(key: str, **_kwargs: Any) -> tuple[Any, bool, bool]:
        return cached[key], True, True

    cache: Any = provider.mass.cache
    cache.get_with_freshness = AsyncMock(side_effect=_cache_hit)
    _stub_show(provider)
    api_mock = AsyncMock()
    provider._get_data = api_mock  # type: ignore[method-assign]
    session_get = _stub_http_session(provider)

    assert await provider._get_stream_url("123") == "https://stream.test/track.m3u8"
    api_mock.assert_not_awaited()
    params = session_get.call_args.kwargs["params"]
    assert params["nn_userID"] == USER_ID
    assert params["subscriptionID"] == "legacy-1"


@pytest.mark.asyncio
async def test_stream_url_prefers_plan_id_field(provider: LivePhishProvider) -> None:
    """The player sends plan.planId when the subscription payload includes it."""
    _stub_account_info(
        provider,
        {**SUBSCRIPTION_BASE, "plan": {"id": "other", "planId": "plan-42", "isHighQuality": True}},
    )
    session_get = _stub_http_session(provider)

    assert await provider._get_stream_url("123") == "https://stream.test/track.m3u8"
    assert session_get.call_args.kwargs["params"]["subCostplanIDAccessList"] == "plan-42"


@pytest.mark.asyncio
async def test_stream_url_uses_regular_plan_id(provider: LivePhishProvider) -> None:
    """A subscription that only has plan.id passes that id as the cost plan access list."""
    _stub_account_info(provider, {**SUBSCRIPTION_BASE, "plan": {"id": "plan-42"}})
    session_get = _stub_http_session(provider)

    assert await provider._get_stream_url("123") == "https://stream.test/track.m3u8"
    assert session_get.call_args.kwargs["params"]["subCostplanIDAccessList"] == "plan-42"


@pytest.mark.asyncio
async def test_stream_url_falls_back_to_promo_plan(provider: LivePhishProvider) -> None:
    """A trial/promo subscription has plan set to None and its plan id under promo."""
    _stub_account_info(
        provider,
        {**SUBSCRIPTION_BASE, "plan": None, "promo": {"plan": {"id": "promo-7"}}},
    )
    session_get = _stub_http_session(provider)

    assert await provider._get_stream_url("123") == "https://stream.test/track.m3u8"
    assert session_get.call_args.kwargs["params"]["subCostplanIDAccessList"] == "promo-7"


@pytest.mark.asyncio
async def test_stream_url_without_any_plan_raises(provider: LivePhishProvider) -> None:
    """Without a regular or promo plan a clear audio error is raised."""
    _stub_account_info(provider, {**SUBSCRIPTION_BASE, "plan": None, "promo": None})

    with pytest.raises(AudioError):
        await provider._get_stream_url("123")


@pytest.mark.asyncio
async def test_stream_url_uses_the_player_request(provider: LivePhishProvider) -> None:
    """Playback calls subplayer with the show id, track id, and player proof."""
    _stub_account_info(provider, {**SUBSCRIPTION_BASE, "plan": {"id": "p", "isHighQuality": True}})
    session_get = _stub_http_session(provider)

    await provider._get_stream_url("7284")
    _assert_player_request(session_get.call_args.kwargs["params"], "7284")


@pytest.mark.asyncio
async def test_subscription_list_uses_the_accessible_plan(provider: LivePhishProvider) -> None:
    """A v3 subscription list is reduced to the content-accessible plan before playback."""
    _install_cache_miss(provider)
    _stub_show(provider)
    accessible = {
        **SUBSCRIPTION_BASE,
        "plan": {"planId": "plan-42"},
        "isContentAccessible": True,
    }
    other = {
        **SUBSCRIPTION_BASE,
        "legacySubscriptionId": "other",
        "plan": {"planId": "other"},
        "isContentAccessible": False,
    }

    async def _fake(api: str, _endpoint: str, **_kwargs: Any) -> Any:
        return [other, accessible] if api == "subscription" else {"userId": 123}

    provider._get_data = AsyncMock(side_effect=_fake)  # type: ignore[method-assign]
    session_get = _stub_http_session(provider)

    await provider._get_stream_url("7284")
    params = session_get.call_args.kwargs["params"]
    assert params["subCostplanIDAccessList"] == "plan-42"
    assert params["subscriptionID"] == "legacy-1"


@pytest.mark.asyncio
async def test_stream_url_reads_iso_subscription_dates(provider: LivePhishProvider) -> None:
    """Subscription dates in ISO form are sent as unix stamps."""
    _stub_account_info(
        provider,
        {
            "startedAt": "2026-08-01T00:00:00Z",
            "endsAt": "2026-09-01T00:00:00+00:00",
            "legacySubscriptionId": "legacy-1",
            "plan": {"id": "plan-42"},
        },
    )
    session_get = _stub_http_session(provider)

    await provider._get_stream_url("123")
    params = session_get.call_args.kwargs["params"]
    assert params["startDateStamp"] == 1785542400
    assert params["endDateStamp"] == 1788220800


@pytest.mark.asyncio
async def test_stream_url_without_any_stream_raises(provider: LivePhishProvider) -> None:
    """A track without a stream in any quality raises a media not found error."""
    _stub_account_info(provider, {**SUBSCRIPTION_BASE, "plan": {"id": "p", "isHighQuality": True}})
    _stub_http_session(provider, '{"streamLink": ""}', '{"streamLink": ""}')

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
    assert details.path == "https://stream.test/track.m3u8"
    assert details.item_id == "123"
    assert details.provider == provider.instance_id


@pytest.mark.asyncio
async def test_stream_url_rewrites_the_player_cdn_host(provider: LivePhishProvider) -> None:
    """An edgesuite stream link is rewritten to the Akamai host before playback."""
    _stub_account_info(provider, {**SUBSCRIPTION_BASE, "plan": {"id": "p", "isHighQuality": True}})
    _stub_http_session(
        provider,
        '{"streamLink": "http://nugs2.edgesuite.net/shows/504/8869/master.m3u8?hdnea=1"}',
    )

    assert (
        await provider._get_stream_url("8869")
        == "https://securestream02.akamaized.net/shows/504/8869/master.m3u8?hdnea=1"
    )


@pytest.mark.asyncio
async def test_empty_media_path_retries_with_the_product_sku(provider: LivePhishProvider) -> None:
    """A link with no media directory is discarded and the product sku is tried."""
    _stub_account_info(provider, {**SUBSCRIPTION_BASE, "plan": {"id": "p"}})
    cache: Any = provider.mass.cache
    cache.get = AsyncMock(return_value="9612")
    session_get = _stub_http_session(
        provider,
        '{"streamLink": "https://securestream02.akamaized.net//master.m3u8?hdnea=1"}',
        '{"streamLink": "https://securestream02.akamaized.net/shows/504/9612/master.m3u8?hdnea=1"}',
    )

    url = await provider._get_stream_url("8869")

    assert url.endswith("/shows/504/9612/master.m3u8?hdnea=1")
    assert [call.kwargs["params"]["skuID"] for call in session_get.call_args_list] == [
        "8869",
        "9612",
    ]
