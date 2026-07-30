"""Tests that library sync batches database writes into few commits."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import ProviderType

from music_assistant.constants import CONF_LOG_LEVEL, DB_TABLE_ARTISTS, DB_TABLE_TRACKS
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

NUM_ARTISTS = 2
NUM_ALBUMS = 2
NUM_TRACKS = 3  # per album
TOTAL_TRACKS = NUM_ARTISTS * NUM_ALBUMS * NUM_TRACKS


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


async def test_track_sync_commits_once_per_item(
    music: MusicController, provider: FakeMusicProvider
) -> None:
    """
    Test that an initial track sync performs roughly one commit per synced item.

    Adding a single track involves many statements (track row, provider mappings,
    artists, album, album/track relations, genre mappings) which previously each
    committed individually (6-10 commits per track).
    """
    commits = 0
    original_commit = music.database._db.commit

    async def counting_commit() -> None:
        nonlocal commits
        commits += 1
        await original_commit()

    music.database._db.commit = counting_commit  # type: ignore[method-assign]

    cur_db_ids = await provider._sync_library_tracks()

    # all tracks (and their artists/albums) actually landed in the library
    assert len(cur_db_ids) == TOTAL_TRACKS
    assert await music.database.get_count(DB_TABLE_TRACKS) == TOTAL_TRACKS
    assert await music.database.get_count(DB_TABLE_ARTISTS) == NUM_ARTISTS
    # every synced item is batched into a single commit
    # (per-statement commits produce ~7x more here: 88 commits for these 12 tracks)
    assert 0 < commits <= TOTAL_TRACKS + 2
