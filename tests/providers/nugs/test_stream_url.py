"""Test Nugs.net stream url building for regular, promo/trial and inactive subscriptions."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from music_assistant_models.errors import ProviderPermissionDenied

from music_assistant.providers.nugs import NugsProvider

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


def _stub_http_session(provider: NugsProvider, captured: dict[str, Any]) -> None:
    """Stub the http session so the subplayer call returns a fixed stream link."""
    response = AsyncMock()
    response.raise_for_status = lambda: None
    response.text = AsyncMock(return_value='{"streamLink": "https://stream.test/track.m3u8"}')
    get_ctx = AsyncMock()
    get_ctx.__aenter__ = AsyncMock(return_value=response)
    get_ctx.__aexit__ = AsyncMock(return_value=False)

    def _get(_url: str, **kwargs: Any) -> Any:
        captured.update(kwargs["params"])
        return get_ctx

    provider.mass.http_session.get = _get


@pytest.mark.asyncio
async def test_stream_url_uses_regular_plan(provider: NugsProvider) -> None:
    """A regular subscription passes its own plan id as the cost plan access list."""
    captured: dict[str, Any] = {}
    _stub_get_data(provider, {**SUBSCRIPTION_BASE, "plan": {"id": "plan-42"}})
    _stub_http_session(provider, captured)

    assert await provider._get_stream_url("123") == "https://stream.test/track.m3u8"
    assert captured["subCostplanIDAccessList"] == "plan-42"


@pytest.mark.asyncio
async def test_stream_url_falls_back_to_promo_plan(provider: NugsProvider) -> None:
    """A trial/promo subscription has plan set to None and its plan id under promo."""
    captured: dict[str, Any] = {}
    _stub_get_data(
        provider,
        {**SUBSCRIPTION_BASE, "plan": None, "promo": {"plan": {"id": "promo-7"}}},
    )
    _stub_http_session(provider, captured)

    assert await provider._get_stream_url("123") == "https://stream.test/track.m3u8"
    assert captured["subCostplanIDAccessList"] == "promo-7"


@pytest.mark.asyncio
async def test_stream_url_without_any_plan_raises(provider: NugsProvider) -> None:
    """Without a regular or promo plan a clear permission error is raised."""
    _stub_get_data(provider, {**SUBSCRIPTION_BASE, "plan": None, "promo": None})

    with pytest.raises(ProviderPermissionDenied):
        await provider._get_stream_url("123")
