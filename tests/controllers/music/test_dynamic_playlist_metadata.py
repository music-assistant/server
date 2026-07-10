"""Tests for dynamic playlist metadata refresh on track regeneration."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from music_assistant.controllers.music.media.playlists import PlaylistController


def _make_ctrl(*, is_dynamic: bool) -> tuple[MagicMock, MagicMock]:
    """Create a mock PlaylistController self plus its library playlist item."""
    library_item = MagicMock()
    library_item.is_dynamic = is_dynamic
    ctrl = MagicMock()
    ctrl.get_library_item = AsyncMock(return_value=library_item)
    ctrl._select_provider_id = MagicMock(return_value=("builtin", "random_album"))
    ctrl._get_provider_playlist_tracks = AsyncMock(side_effect=[[MagicMock(), MagicMock()], []])
    ctrl.mass = MagicMock()
    # close the scheduled coroutine to avoid "never awaited" warnings
    ctrl.mass.create_task = MagicMock(side_effect=lambda coro: coro.close())
    ctrl.mass.metadata.update_metadata = AsyncMock()
    return ctrl, library_item


async def _consume(ctrl: MagicMock, **kwargs: bool) -> None:
    """Fully consume the tracks generator for a library playlist."""
    async for _ in PlaylistController.tracks(ctrl, "1", "library", **kwargs):
        pass


@pytest.mark.asyncio
async def test_force_refresh_dynamic_playlist_updates_metadata() -> None:
    """A user-initiated refresh of a dynamic playlist must recompute its metadata."""
    ctrl, library_item = _make_ctrl(is_dynamic=True)

    await _consume(ctrl, force_refresh=True)

    ctrl.mass.create_task.assert_called_once()
    # recompute is requested by clearing last_refresh; force_refresh is not passed
    assert library_item.metadata.last_refresh is None
    ctrl.mass.metadata.update_metadata.assert_called_once_with(library_item)


@pytest.mark.asyncio
async def test_browse_dynamic_playlist_keeps_metadata() -> None:
    """A plain browse request (no refresh) must not touch playlist metadata."""
    ctrl, _ = _make_ctrl(is_dynamic=True)

    await _consume(ctrl)

    ctrl.mass.create_task.assert_not_called()


@pytest.mark.asyncio
async def test_playback_refill_dynamic_playlist_keeps_metadata() -> None:
    """A playback refill (allow_dynamic_tracks) must not trigger a metadata update."""
    ctrl, _ = _make_ctrl(is_dynamic=True)

    await _consume(ctrl, allow_dynamic_tracks=True)

    ctrl.mass.create_task.assert_not_called()


@pytest.mark.asyncio
async def test_force_refresh_static_playlist_keeps_metadata() -> None:
    """A forced refresh of a regular playlist must not trigger a metadata update."""
    ctrl, _ = _make_ctrl(is_dynamic=False)

    await _consume(ctrl, force_refresh=True)

    ctrl.mass.create_task.assert_not_called()
