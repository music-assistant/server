"""Tests for the MusicProvider base model."""

from __future__ import annotations

import asyncio
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType, ProviderFeature, ProviderType

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


def _make_provider[ProviderT: MusicProvider](
    provider_cls: type[ProviderT], supported_features: set[ProviderFeature] | None = None
) -> ProviderT:
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
    return provider_cls(mass, manifest, config, supported_features)


def test_supported_media_types_default_follows_library_features() -> None:
    """Without an override, media type support is derived from the library features."""
    provider = _make_provider(
        _StreamingProvider,
        supported_features={
            ProviderFeature.SEARCH,
            ProviderFeature.LIBRARY_TRACKS,
            ProviderFeature.LIBRARY_PLAYLISTS,
        },
    )

    assert provider.supported_media_types == {MediaType.TRACK, MediaType.PLAYLIST}


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


def _listing(*positions: int) -> list[MagicMock]:
    """Return a mocked listing page whose items carry the given playlist positions."""
    return [MagicMock(position=position) for position in positions]


async def test_move_playlist_tracks_replays_single_moves() -> None:
    """Without an override, a block move becomes single moves over the counted playlist."""
    provider = _make_provider(_StreamingProvider)
    pages = {0: _listing(1, 2, 3), 1: _listing(4, 5)}
    provider.get_playlist_tracks = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda _id, page=0: pages.get(page, [])
    )
    provider.move_playlist_track = AsyncMock()  # type: ignore[method-assign]

    await provider.move_playlist_tracks("playlist_1", [4, 5], 1)

    assert [call.args for call in provider.move_playlist_track.await_args_list] == [
        ("playlist_1", 4, 1),
        ("playlist_1", 5, 2),
    ]


@pytest.mark.parametrize(
    ("listed", "positions", "to_position", "expected"),
    [
        pytest.param((1, 3), [3], 1, [("playlist_1", 3, 1)], id="skipped-middle-entry"),
        pytest.param((1, 3), [1], None, [("playlist_1", 1, 3)], id="to-end-past-skipped"),
        pytest.param((1, 2), [1], None, [("playlist_1", 1, 2)], id="to-end-of-listing"),
    ],
)
async def test_move_playlist_tracks_counts_entries_the_listing_leaves_out(
    listed: tuple[int, ...],
    positions: list[int],
    to_position: int | None,
    expected: list[tuple[str, int, int]],
) -> None:
    """Positions past the listed count are valid, and None moves the tracks to the end."""
    provider = _make_provider(_StreamingProvider)
    provider.get_playlist_tracks = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda _id, page=0: _listing(*listed) if page == 0 else []
    )
    provider.move_playlist_track = AsyncMock()  # type: ignore[method-assign]

    await provider.move_playlist_tracks("playlist_1", positions, to_position)

    assert [call.args for call in provider.move_playlist_track.await_args_list] == expected
