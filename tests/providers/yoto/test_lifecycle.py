"""Lifecycle and synchronization tests for the Yoto provider."""
# ruff: noqa: D103

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType

import music_assistant.providers.yoto.provider as provider_module
from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.yoto.catalogue import Catalogue
from music_assistant.providers.yoto.client import YotoAdapter
from music_assistant.providers.yoto.provider import YotoProvider


def _provider() -> YotoProvider:
    provider = object.__new__(YotoProvider)
    cast("Any", provider).config = SimpleNamespace(instance_id="yoto-instance")
    provider.catalogue = Catalogue()
    return provider


@pytest.mark.asyncio
async def test_init_reads_setup_credentials_and_rotated_token_stays_in_setup_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Credentials belong to encrypted setup data, including refresh-token rotation."""
    provider = _provider()
    cast("Any", provider).get_setup_value = MagicMock(
        side_effect=lambda key: {"client_id": "client", "refresh_token": "refresh"}[key]
    )
    cast("Any", provider.config).get_value = MagicMock(
        side_effect=AssertionError("read runtime config")
    )
    update_setup_data = MagicMock()
    cast("Any", provider)._update_setup_data = update_setup_data
    cast("Any", provider).mass = SimpleNamespace(
        http_session=object(),
        streams=SimpleNamespace(register_dynamic_route=MagicMock(return_value=MagicMock())),
    )

    adapter = MagicMock()
    adapter.refresh_catalogue = AsyncMock(return_value=Catalogue())
    adapter_class = MagicMock(return_value=adapter)
    monkeypatch.setattr(provider_module, "YotoAdapter", adapter_class)
    monkeypatch.setattr(provider_module, "monotonic", lambda: 123.0)

    await provider.handle_async_init()
    await provider._persist_refresh_token("rotated")

    adapter_class.assert_called_once_with(
        "client",
        "refresh",
        session=provider.mass.http_session,
        token_callback=provider._persist_refresh_token,
    )
    update_setup_data.assert_called_once_with("refresh_token", "rotated", immediate=True)
    assert provider._last_sync_refresh == 123.0


@pytest.mark.asyncio
async def test_library_sync_refreshes_before_import_and_shares_burst_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider()
    refreshed = Catalogue()
    refresh_catalogue = AsyncMock(return_value=refreshed)
    provider.adapter = cast("YotoAdapter", SimpleNamespace(refresh_catalogue=refresh_catalogue))
    base_calls: list[MediaType] = []

    async def base_sync(_provider: YotoProvider, media_type: MediaType) -> None:
        base_calls.append(media_type)

    monkeypatch.setattr(MusicProvider, "sync_library", base_sync)

    await asyncio.gather(
        provider.sync_library(MediaType.ALBUM),
        provider.sync_library(MediaType.TRACK),
        provider.sync_library(MediaType.AUDIOBOOK),
    )

    refresh_catalogue.assert_awaited_once_with()
    assert provider.catalogue is refreshed
    assert set(base_calls) == {MediaType.ALBUM, MediaType.TRACK, MediaType.AUDIOBOOK}


@pytest.mark.asyncio
async def test_sync_refreshes_at_freshness_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider()
    provider._sync_lock = asyncio.Lock()
    provider._last_sync_refresh = 100.0
    refresh_catalogue = AsyncMock(return_value=Catalogue())
    provider.adapter = cast("YotoAdapter", SimpleNamespace(refresh_catalogue=refresh_catalogue))
    values = iter((129.9, 130.0, 130.1))
    monkeypatch.setattr(provider_module, "monotonic", lambda: next(values))
    monkeypatch.setattr(MusicProvider, "sync_library", AsyncMock())

    await provider.sync_library(MediaType.ALBUM)
    refresh_catalogue.assert_not_awaited()
    await provider.sync_library(MediaType.AUDIOBOOK)

    refresh_catalogue.assert_awaited_once_with()
    assert provider._last_sync_refresh == 130.1


@pytest.mark.asyncio
async def test_unload_revokes_sessions_unregisters_route_and_calls_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider()
    callback = MagicMock()
    cast("Any", provider)._audiobook_sessions = {"opaque": object()}
    provider._on_unload_callbacks = [callback]
    base_unload = AsyncMock()
    monkeypatch.setattr(MusicProvider, "unload", base_unload)

    await provider.unload()

    assert provider._audiobook_sessions == {}
    callback.assert_called_once_with()
    base_unload.assert_awaited_once_with(False)
