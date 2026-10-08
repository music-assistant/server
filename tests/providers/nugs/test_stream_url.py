"""Test Nugs.net stream url building for regular, promo/trial and inactive subscriptions."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.errors import AudioError

from music_assistant.providers.nugs import CONF_QUALITY, NugsProvider

USER_DATA = {"userId": "user-1"}
SUBSCRIPTION_BASE = {
    "startedAt": "08/01/2026 00:00:00",
    "endsAt": "09/01/2026 00:00:00",
    "legacySubscriptionId": "legacy-1",
}


def _stub_get_data(provider: NugsProvider, subscription: dict[str, Any]) -> None:
    """Attach a _get_data stub returning the given subscription payload."""

    async def _fake(nugs_api: str, _endpoint: str, **_kwargs: Any) -> Any:
        if nugs_api == "subscription":
            return subscription
        return USER_DATA

    provider._get_data = AsyncMock(side_effect=_fake)  # type: ignore[method-assign]


def _stub_http_session(provider: NugsProvider, *bodies: str) -> MagicMock:
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


@pytest.mark.asyncio
async def test_stream_url_uses_regular_plan(provider: NugsProvider) -> None:
    """A regular subscription passes its own plan id as the cost plan access list."""
    _stub_get_data(provider, {**SUBSCRIPTION_BASE, "plan": {"id": "plan-42"}})
    session_get = _stub_http_session(provider)

    assert await provider._get_stream_url("123") == "https://stream.test/track.m3u8"
    assert session_get.call_args.kwargs["params"]["subCostplanIDAccessList"] == "plan-42"


@pytest.mark.asyncio
async def test_stream_url_falls_back_to_promo_plan(provider: NugsProvider) -> None:
    """A trial/promo subscription has plan set to None and its plan id under promo."""
    _stub_get_data(
        provider,
        {**SUBSCRIPTION_BASE, "plan": None, "promo": {"plan": {"id": "promo-7"}}},
    )
    session_get = _stub_http_session(provider)

    assert await provider._get_stream_url("123") == "https://stream.test/track.m3u8"
    assert session_get.call_args.kwargs["params"]["subCostplanIDAccessList"] == "promo-7"


@pytest.mark.asyncio
async def test_stream_url_without_any_plan_raises(provider: NugsProvider) -> None:
    """Without a regular or promo plan a clear audio error is raised."""
    _stub_get_data(provider, {**SUBSCRIPTION_BASE, "plan": None, "promo": None})

    with pytest.raises(AudioError):
        await provider._get_stream_url("123")


def _set_quality(provider: NugsProvider, quality: str) -> None:
    """Configure the stream quality setting on the provider."""
    config: Any = provider.config
    config.get_value.side_effect = lambda key, default=None: {
        "log_level": "GLOBAL",
        CONF_QUALITY: quality,
    }.get(key, default)


@pytest.mark.asyncio
@pytest.mark.parametrize(("quality", "platform_id"), [("lossless", 2), ("mqa", 5), ("lossy", -1)])
async def test_stream_url_uses_quality_on_high_quality_plan(
    provider: NugsProvider, quality: str, platform_id: int
) -> None:
    """A plan with high quality streaming requests the configured quality."""
    _set_quality(provider, quality)
    _stub_get_data(provider, {**SUBSCRIPTION_BASE, "plan": {"id": "p", "isHighQuality": True}})
    session_get = _stub_http_session(provider)

    await provider._get_stream_url("123")
    assert session_get.call_args.kwargs["params"]["platformID"] == platform_id


@pytest.mark.asyncio
async def test_stream_url_caps_quality_to_plan(provider: NugsProvider) -> None:
    """A plan without high quality streaming only requests the lossy stream."""
    _set_quality(provider, "mqa")
    _stub_get_data(provider, {**SUBSCRIPTION_BASE, "plan": {"id": "p", "isHighQuality": False}})
    session_get = _stub_http_session(provider)

    await provider._get_stream_url("123")
    assert session_get.call_count == 1
    assert session_get.call_args.kwargs["params"]["platformID"] == -1


@pytest.mark.asyncio
async def test_stream_url_falls_back_to_lossy(provider: NugsProvider) -> None:
    """A track without a stream in the requested quality falls back to lossy."""
    _stub_get_data(provider, {**SUBSCRIPTION_BASE, "plan": {"id": "p", "isHighQuality": True}})
    session_get = _stub_http_session(
        provider, '{"streamLink": ""}', '{"streamLink": "https://stream.test/lossy.m3u8"}'
    )

    assert await provider._get_stream_url("123") == "https://stream.test/lossy.m3u8"
    assert [c.kwargs["params"]["platformID"] for c in session_get.call_args_list] == [2, -1]
