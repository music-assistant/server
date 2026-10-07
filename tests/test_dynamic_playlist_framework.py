"""Tests for dynamic-playlist handling in the playlist metadata refresh."""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.media_items import ProviderMapping, Track

from music_assistant.constants import DB_TABLE_PLAYLISTS
from music_assistant.controllers.metadata import MetaDataController
from music_assistant.controllers.metadata.enrichment import MetadataEnrichmentMixin


def _controller() -> MetaDataController:
    """Create a bare MetaDataController without running __init__."""
    ctrl = MetaDataController.__new__(MetaDataController)
    ctrl._corrupt_metadata_rows = {}
    return ctrl


def _provider_mapping(item_id: str = "station_1") -> ProviderMapping:
    """Build a minimal ProviderMapping for a Pandora-style station."""
    return ProviderMapping(
        item_id=item_id,
        provider_domain="pandora",
        provider_instance="pandora_1",
        in_library=True,
    )


# --------------------------------------------------------------------------- #
#  _update_playlist_metadata processes all playlists when called directly      #
# --------------------------------------------------------------------------- #


async def test_update_playlist_metadata_processes_dynamic_playlists() -> None:
    """_update_playlist_metadata processes dynamic playlists; providers decide filtering."""
    mixin = MetadataEnrichmentMixin()
    mixin.mass = Mock()
    mixin.logger = Mock()
    mixin.providers = []  # type: ignore[misc]

    async def mock_tracks(
        item_id: str,  # noqa: ARG001
        provider: str,  # noqa: ARG001
    ) -> AsyncIterator[Track]:
        """Empty async generator."""
        if False:
            yield  # type: ignore[unreachable]  # pragma: no cover

    mixin.mass.music.playlists.tracks = mock_tracks
    mixin.mass.music.playlists.update_item_in_library = AsyncMock()

    playlist = Mock()
    playlist.is_dynamic = True
    playlist.provider_mappings = {_provider_mapping()}
    playlist.provider = "pandora"
    playlist.item_id = "test_dynamic"
    playlist.name = "Test Dynamic Playlist"
    playlist.metadata = Mock()
    playlist.metadata.last_refresh = 0
    playlist.metadata.genres = set()

    await mixin._update_playlist_metadata(playlist, force_refresh=True)

    # Track scanning should happen (providers decide whether to use this data)
    mixin.logger.debug.assert_called()


async def test_refresh_playlist_metadata_batch_query_excludes_dynamic_playlists() -> None:
    """Batch query filters is_dynamic playlists; single updates process all playlists."""
    ctrl = _controller()
    mass = Mock()
    mass.music.playlists.get_library_items_by_query = AsyncMock(return_value=[])
    ctrl.mass = mass

    await ctrl._refresh_playlist_metadata_batch()

    _, kwargs = mass.music.playlists.get_library_items_by_query.call_args
    query_parts = kwargs["extra_query_parts"]
    # Batch query should still filter dynamic playlists (automated refresh)
    assert any(f"{DB_TABLE_PLAYLISTS}.is_dynamic = 0" in part for part in query_parts)


async def test_refresh_playlist_metadata_batch_reports_corrupt_row_reactively() -> None:
    """A malformed-JSON row aborts the scan, gets reported, and the scan retries guarded."""
    ctrl = _controller()
    ctrl.logger = Mock()
    mass = Mock()
    # first (guard-free) query aborts on the corrupt row; guarded retry succeeds
    mass.music.playlists.get_library_items_by_query = AsyncMock(
        side_effect=[sqlite3.OperationalError("malformed JSON"), []]
    )
    mass.music.database.get_rows_from_query = AsyncMock(
        return_value=[{"item_id": 7, "name": "Broken"}]
    )
    ctrl.mass = mass

    await ctrl._refresh_playlist_metadata_batch()

    assert mass.music.playlists.get_library_items_by_query.await_count == 2
    retry_parts = mass.music.playlists.get_library_items_by_query.call_args_list[1].kwargs[
        "extra_query_parts"
    ]
    # only the retry carries the json_valid guard
    assert any("json_valid" in part for part in retry_parts)
    assert ctrl._corrupt_metadata_rows[DB_TABLE_PLAYLISTS] == [{"item_id": 7, "name": "Broken"}]


async def test_refresh_playlist_metadata_batch_clears_stale_corrupt_rows() -> None:
    """A clean scan clears corrupt rows recorded on a previous pass."""
    ctrl = _controller()
    ctrl._corrupt_metadata_rows = {DB_TABLE_PLAYLISTS: [{"item_id": 7, "name": "Broken"}]}
    mass = Mock()
    mass.music.playlists.get_library_items_by_query = AsyncMock(return_value=[])
    ctrl.mass = mass

    await ctrl._refresh_playlist_metadata_batch()

    assert DB_TABLE_PLAYLISTS not in ctrl._corrupt_metadata_rows


async def test_refresh_playlist_metadata_batch_reraises_unrelated_db_error() -> None:
    """A non-malformed OperationalError is not swallowed by the corrupt-row handling."""
    ctrl = _controller()
    mass = Mock()
    mass.music.playlists.get_library_items_by_query = AsyncMock(
        side_effect=sqlite3.OperationalError("database is locked")
    )
    ctrl.mass = mass

    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        await ctrl._refresh_playlist_metadata_batch()
