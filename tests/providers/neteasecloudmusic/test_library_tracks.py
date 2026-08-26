"""Tests for NetEase Cloud Music liked-track listing."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest
from music_assistant_models.enums import MediaType

from music_assistant.models.music_provider import SYNC_RUN_STATE, SyncRunState

if TYPE_CHECKING:
    from collections.abc import Iterator

    from music_assistant.providers.neteasecloudmusic import NeteaseCloudMusicProvider


@pytest.fixture
def sync_run() -> Iterator[SyncRunState]:
    """Run the library listing as part of a sync run, so its skips are recorded."""
    state = SyncRunState()
    token = SYNC_RUN_STATE.set(state)
    yield state
    SYNC_RUN_STATE.reset(token)


def _song(song_id: int) -> dict[str, Any]:
    """Return a minimal song payload NCM would hand back."""
    return {"id": song_id, "name": f"Track {song_id}", "dt": 1000}


async def test_liked_track_the_detail_call_left_out_is_reported(
    provider: NeteaseCloudMusicProvider, sync_run: SyncRunState
) -> None:
    """
    A liked track missing from the detail response is reported, not treated as removed.

    The liked list is what says the track is in the library, so a detail call that leaves
    it out must not make the deletion pass drop it.
    """
    provider._client = AsyncMock()
    provider._client.get = AsyncMock(return_value={"ids": [1, 2, 3]})
    provider._get_song_detail = AsyncMock(  # type: ignore[method-assign]
        return_value=[_song(1), _song(3)]
    )

    tracks = [track async for track in provider.get_library_tracks()]

    assert [track.item_id for track in tracks] == ["1", "3"]
    assert sync_run.skipped_item_ids == {MediaType.TRACK: {"2"}}
    assert not sync_run.incomplete_media_types


async def test_complete_detail_response_reports_nothing(
    provider: NeteaseCloudMusicProvider, sync_run: SyncRunState
) -> None:
    """A response holding every liked track reports no skips at all."""
    provider._client = AsyncMock()
    provider._client.get = AsyncMock(return_value={"ids": [1, 2]})
    provider._get_song_detail = AsyncMock(  # type: ignore[method-assign]
        return_value=[_song(1), _song(2)]
    )

    tracks = [track async for track in provider.get_library_tracks()]

    assert [track.item_id for track in tracks] == ["1", "2"]
    assert sync_run.skipped_item_ids == {}
