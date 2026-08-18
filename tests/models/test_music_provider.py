"""Tests for MusicProvider source-stream capacity."""

from __future__ import annotations

import asyncio
from typing import cast
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import ProviderType

from music_assistant.models.music_provider import (
    DEFAULT_MAX_CONCURRENT_STREAMS,
    MusicProvider,
    ProviderStreamLimitError,
)


class _StreamingProvider(MusicProvider):
    """Streaming provider using the default source limit."""


class _NonStreamingProvider(MusicProvider):
    """Non-streaming provider with no source limit."""

    @property
    def is_streaming_provider(self) -> bool:
        """Return False for this local provider."""
        return False


class _SingleStreamProvider(MusicProvider):
    """Streaming provider with one source slot."""

    @property
    def max_concurrent_streams(self) -> int:
        """Return one source slot."""
        return 1


def _make_provider[ProviderT: MusicProvider](provider_cls: type[ProviderT]) -> ProviderT:
    """Construct a minimal MusicProvider instance."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.type = ProviderType.MUSIC
    manifest.domain = "test_provider"
    manifest.name = "Test Provider"
    config = MagicMock()
    config.name = "Test Provider"
    config.instance_id = "test_provider--1"
    config.get_value.return_value = "GLOBAL"
    return provider_cls(mass, manifest, config)


def test_streaming_provider_has_conservative_default() -> None:
    """Unknown streaming providers default to five concurrent sources."""
    provider = _make_provider(_StreamingProvider)

    assert provider.max_concurrent_streams == DEFAULT_MAX_CONCURRENT_STREAMS == 5


def test_non_streaming_provider_is_unlimited() -> None:
    """Non-streaming providers do not allocate a source limiter."""
    provider = _make_provider(_NonStreamingProvider)

    assert provider.max_concurrent_streams is None
    assert provider.has_available_stream_slot


async def test_stream_slot_is_scoped_per_provider_instance() -> None:
    """One busy provider instance does not consume another instance's slot."""
    first = _make_provider(_SingleStreamProvider)
    second = _make_provider(_SingleStreamProvider)
    cast("MagicMock", second.config).instance_id = "test_provider--2"

    async with first.acquire_stream_slot(0.1):
        assert not first.has_available_stream_slot
        assert second.has_available_stream_slot
        with pytest.raises(ProviderStreamLimitError) as exc_info:
            async with first.acquire_stream_slot(0):
                pytest.fail("A second source slot was unexpectedly acquired")
        # the error must carry the localizable reason (provider name + limit) for API clients
        assert exc_info.value.translation_key == "provider_stream_limit"
        assert exc_info.value.translation_args == ["Test Provider", 1]

    assert first.has_available_stream_slot


async def test_stream_slot_is_granted_once_released() -> None:
    """A waiter within its timeout is served as soon as the active source releases."""
    provider = _make_provider(_SingleStreamProvider)
    acquired = asyncio.Event()

    async def _wait_for_slot() -> None:
        async with provider.acquire_stream_slot(5):
            acquired.set()

    async with provider.acquire_stream_slot(5):
        waiter = asyncio.create_task(_wait_for_slot())
        await asyncio.sleep(0)
        assert not acquired.is_set()

    await waiter

    assert acquired.is_set()
    assert provider.has_available_stream_slot


async def test_cancelled_stream_slot_wait_does_not_consume_capacity() -> None:
    """Cancelling a waiter leaves the active source as the only leased slot."""
    provider = _make_provider(_SingleStreamProvider)

    async with provider.acquire_stream_slot(0.1):
        waiter = asyncio.create_task(provider.acquire_stream_slot(None).__aenter__())
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not provider.has_available_stream_slot

    assert provider.has_available_stream_slot
