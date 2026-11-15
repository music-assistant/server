"""Tests that a library migration on a (nearly) full disk keeps the library."""

from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

from music_assistant.constants import DB_TABLE_AUDIO_ANALYSIS, DB_TABLE_SETTINGS
from music_assistant.controllers.music import MusicController, migrations
from music_assistant.controllers.music.constants import DB_SCHEMA_VERSION
from music_assistant.mass import MusicAssistant

from .helpers import create_track

if TYPE_CHECKING:
    import pytest

PREV_VERSION = DB_SCHEMA_VERSION - 1
LEGACY_ANALYSIS_DDL = (
    f"CREATE TABLE {DB_TABLE_AUDIO_ANALYSIS}("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, media_type TEXT NOT NULL, item_id TEXT NOT NULL, "
    "provider TEXT NOT NULL, aa_provider_domain TEXT NOT NULL, analysis_data json NOT NULL, "
    "analysis_version INTEGER DEFAULT 1, "
    "timestamp_created INTEGER DEFAULT (cast(strftime('%s','now') as int)), "
    "UNIQUE(item_id,provider,aa_provider_domain,media_type))"
)


def _disk_full_error() -> sqlite3.OperationalError:
    err = sqlite3.OperationalError("database or disk is full")
    err.sqlite_errorcode = sqlite3.SQLITE_FULL
    return err


async def _library_at_previous_version(mass: MusicAssistant) -> MusicController:
    """Return a closed music controller whose library.db holds one track at the previous schema."""
    music = MusicController(mass)
    mass.music = music
    await music._setup_database()
    await music.tracks.add_item_to_library(create_track("spotify_1", "track_abc"))
    await music.database.insert_or_replace(
        DB_TABLE_SETTINGS, {"key": "version", "value": str(PREV_VERSION), "type": "str"}
    )
    await music.database.commit()
    await music.database.close()
    return music


def _library_state(mass: MusicAssistant) -> tuple[str, int]:
    """Return the stored schema version and the track count of library.db."""
    with closing(sqlite3.connect(os.path.join(mass.storage_path, "library.db"))) as db:
        version = db.execute(
            f"SELECT value FROM {DB_TABLE_SETTINGS} WHERE key = 'version'"
        ).fetchone()[0]
        tracks = db.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    return version, tracks


async def test_cache_clear_failure_after_migration_keeps_library(
    mass_minimal: MusicAssistant,
) -> None:
    """A cache clear that fails after a successful migration leaves the migrated library."""
    music = await _library_at_previous_version(mass_minimal)
    mass_minimal.cache.clear = AsyncMock(side_effect=_disk_full_error())  # type: ignore[method-assign]

    await music._setup_database()
    await music.database.close()

    assert _library_state(mass_minimal) == (str(DB_SCHEMA_VERSION), 1)


async def test_analysis_move_waits_for_space_and_retries_on_next_start(
    mass_minimal: MusicAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Leftover legacy analysis rows move on a later start once there is room for them."""
    music = MusicController(mass_minimal)
    mass_minimal.music = music
    await music._setup_database()
    await music.database.execute(LEGACY_ANALYSIS_DDL)
    await music.database.execute(
        f"INSERT INTO {DB_TABLE_AUDIO_ANALYSIS}"
        "(media_type, item_id, provider, aa_provider_domain, analysis_data) "
        "VALUES ('track', 't0', 'fs--a', 'loudness_analysis', '{\"bpm\": 120.0}')"
    )
    await music.database.commit()
    await music.database.close()

    monkeypatch.setattr(migrations, "get_free_space", AsyncMock(return_value=0.0))
    with caplog.at_level(logging.WARNING):
        await music._setup_database()
    await music.database.close()
    assert "Not enough disk space to move audio analysis" in caplog.text
    with closing(sqlite3.connect(os.path.join(mass_minimal.storage_path, "library.db"))) as db:
        assert db.execute(f"SELECT COUNT(*) FROM {DB_TABLE_AUDIO_ANALYSIS}").fetchone()[0] == 1

    monkeypatch.undo()
    await music._setup_database()
    await music.database.close()
    with closing(sqlite3.connect(os.path.join(mass_minimal.storage_path, "library.db"))) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert DB_TABLE_AUDIO_ANALYSIS not in tables


async def test_failed_analysis_size_probe_keeps_library(
    mass_minimal: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A size probe that fails on a damaged analysis table skips the move and keeps the library."""
    music = await _library_at_previous_version(mass_minimal)
    await music._setup_database()
    await music.database.execute(LEGACY_ANALYSIS_DDL)
    await music.database.insert_or_replace(
        DB_TABLE_SETTINGS, {"key": "version", "value": str(PREV_VERSION), "type": "str"}
    )
    await music.database.commit()
    await music.database.close()
    monkeypatch.setattr(
        migrations, "_tables_size", AsyncMock(side_effect=sqlite3.DatabaseError("malformed"))
    )
    mass_minimal.cache.clear = AsyncMock()  # type: ignore[method-assign]

    await music._setup_database()
    await music.database.close()

    assert _library_state(mass_minimal) == (str(DB_SCHEMA_VERSION), 1)
