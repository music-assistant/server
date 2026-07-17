"""Test MusicMe recommendations() row filtering via the `wanted` parameter."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from music_assistant.providers.musicme.provider import MusicMeProvider

HOME_DATA = {"results": {"items": [{"id": 1, "name": "Home Radio"}]}}
NEWS_DATA = {"results": {"albums": [{"barcode": "b1", "name": "Album", "streamable": 2}]}}
TOPS_DATA = {"results": {"artists": [{"id": 2, "name": "Artist"}]}}
RADIOS_DATA = {"results": {"theme-airplays": [{"id": 3, "name": "Radio"}]}}


def _stub_api_get(provider: MusicMeProvider) -> AsyncMock:
    """Attach an _api_get stub that returns canned data keyed by endpoint prefix."""

    async def _fake(endpoint: str) -> dict[str, Any] | None:
        if endpoint.startswith("/home"):
            return HOME_DATA
        if endpoint.startswith("/news"):
            return NEWS_DATA
        if endpoint.startswith("/tops"):
            return TOPS_DATA
        if endpoint.startswith("/radios"):
            return RADIOS_DATA
        return None

    mock = AsyncMock(side_effect=_fake)
    provider._api_get = mock  # type: ignore[method-assign]
    return mock


def _install_cache_mocks(provider: MusicMeProvider) -> None:
    """Make the @use_cache decorator treat every call as a cache miss."""
    provider.mass.cache.get_with_freshness = AsyncMock(  # type: ignore[method-assign]
        return_value=(None, False, False)
    )
    provider.mass.cache.set = AsyncMock()  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_recommendations_wanted_none_fetches_all_rows(provider: MusicMeProvider) -> None:
    """wanted=None (default) fetches and builds all four rows — unchanged behavior."""
    _install_cache_mocks(provider)
    api_mock = _stub_api_get(provider)

    result = await provider.recommendations()

    called_paths = {call.args[0].split("?", 1)[0] for call in api_mock.call_args_list}
    assert called_paths == {"/home", "/news/0", "/tops", "/radios"}
    assert {f.item_id for f in result} == {
        f"{provider.instance_id}_home",
        f"{provider.instance_id}_news",
        f"{provider.instance_id}_tops",
        f"{provider.instance_id}_radios",
    }


@pytest.mark.asyncio
async def test_recommendations_wanted_home_only_fetches_home(provider: MusicMeProvider) -> None:
    """wanted={<instance>_home} fetches only /home and returns only the Featured folder."""
    _install_cache_mocks(provider)
    api_mock = _stub_api_get(provider)

    result = await provider.recommendations(wanted={f"{provider.instance_id}_home"})

    api_mock.assert_awaited_once()
    assert api_mock.call_args.args[0] == "/home"
    assert len(result) == 1
    assert result[0].item_id == f"{provider.instance_id}_home"
    assert result[0].name == "Featured"
