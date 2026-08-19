"""Tests that the library sync repairs missing album/artist links on existing tracks."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import ProviderType

from music_assistant.constants import (
    CONF_LOG_LEVEL,
    DB_TABLE_ALBUM_TRACKS,
    DB_TABLE_TRACK_ARTISTS,
)
from music_assistant.controllers.music import MusicController
from music_assistant.providers.test import (
    CONF_KEY_NUM_ALBUMS,
    CONF_KEY_NUM_ARTISTS,
    CONF_KEY_NUM_TRACKS,
)
from music_assistant.providers.test import TestProvider as FakeMusicProvider

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant.mass import MusicAssistant

NUM_ARTISTS = 1
NUM_ALBUMS = 1
NUM_TRACKS = 2  # per album


@pytest.fixture
async def music(mass_minimal: MusicAssistant) -> AsyncGenerator[MusicController]:
    """Return a music controller with initialized database on the minimal mass instance."""
    controller = MusicController(mass_minimal)
    mass_minimal.music = controller
    await controller._setup_database()
    yield controller
    if controller._database:
        await controller._database.close()


@pytest.fixture
def provider(mass_minimal: MusicAssistant) -> FakeMusicProvider:
    """Return a fake music provider with a small deterministic library."""
    manifest = MagicMock()
    manifest.type = ProviderType.MUSIC
    manifest.domain = "test"
    config = MagicMock()
    config.instance_id = "test--1"
    config.domain = "test"
    values = {
        CONF_KEY_NUM_ARTISTS: NUM_ARTISTS,
        CONF_KEY_NUM_ALBUMS: NUM_ALBUMS,
        CONF_KEY_NUM_TRACKS: NUM_TRACKS,
        CONF_LOG_LEVEL: "GLOBAL",
    }
    config.get_value.side_effect = lambda key, default=None: values.get(key, default)
    return FakeMusicProvider(mass_minimal, manifest, config)


async def _relation_count(music: MusicController, table: str, track_id: int) -> int:
    """Return the number of relation rows the given table holds for a track."""
    rows = await music.database.get_rows_from_query(
        f"SELECT 1 FROM {table} WHERE track_id = :track_id", {"track_id": track_id}
    )
    return len(rows)


async def test_sync_backfills_missing_track_artists(
    music: MusicController, provider: FakeMusicProvider
) -> None:
    """
    A library track that lost its artist link is repaired by the next sync.

    A track row is written before its artist relations, so an interruption in between
    leaves a committed track with no artists at all. Nothing else can spot it: it reads
    back fine and the duplicate reconciliation pass needs exactly those rows.
    """
    synced_ids = await provider._sync_library_tracks()
    track_id = next(iter(synced_ids))
    assert await _relation_count(music, DB_TABLE_TRACK_ARTISTS, track_id) > 0

    # simulate the interrupted add: the track (and its provider mappings) survive
    # without any artist relations
    await music.database.delete(DB_TABLE_TRACK_ARTISTS, {"track_id": track_id})
    assert await _relation_count(music, DB_TABLE_TRACK_ARTISTS, track_id) == 0

    await provider._sync_library_tracks()

    assert await _relation_count(music, DB_TABLE_TRACK_ARTISTS, track_id) > 0


async def test_sync_backfills_missing_track_album(
    music: MusicController, provider: FakeMusicProvider
) -> None:
    """A library track that lost its album link is repaired by the next sync."""
    synced_ids = await provider._sync_library_tracks()
    track_id = next(iter(synced_ids))
    assert await _relation_count(music, DB_TABLE_ALBUM_TRACKS, track_id) > 0

    await music.database.delete(DB_TABLE_ALBUM_TRACKS, {"track_id": track_id})
    assert await _relation_count(music, DB_TABLE_ALBUM_TRACKS, track_id) == 0

    await provider._sync_library_tracks()

    assert await _relation_count(music, DB_TABLE_ALBUM_TRACKS, track_id) > 0
