"""Tests for the music library database migrations."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ExternalID
from music_assistant_models.errors import MusicAssistantError

from music_assistant.constants import (
    DB_TABLE_AUDIO_ANALYSIS,
    DB_TABLE_EXTERNAL_ID_LOOKUP,
    DB_TABLE_FAVORITES,
    DB_TABLE_GENRE_MEDIA_ITEM_MAPPING,
    DB_TABLE_GENRES,
    DB_TABLE_LEGACY_PLAYLOG,
    DB_TABLE_MEDIA_PROGRESS,
    DB_TABLE_PLAY_HISTORY,
    DB_TABLE_PROVIDER_MAPPINGS,
    DB_TABLE_SETTINGS,
)
from music_assistant.controllers.music import MusicController, migrations
from music_assistant.controllers.music.favorites import PENDING_USER_ID
from music_assistant.controllers.music.migrations import migrate_database
from music_assistant.helpers.database import DatabaseConnection
from music_assistant.helpers.json import serialize_to_json
from music_assistant.mass import MusicAssistant

from .helpers import ISRC, create_track

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

MEDIA_TABLES = (
    "artists",
    "albums",
    "tracks",
    "playlists",
    "radios",
    "audiobooks",
    "podcasts",
    "genres",
)


@pytest.fixture
async def database(tmp_path: Path) -> AsyncGenerator[DatabaseConnection]:
    """Return an initialized DatabaseConnection backed by a temp file."""
    db = DatabaseConnection(str(tmp_path / "library.db"))
    await db.setup()
    # minimal stand-ins for the tables that create_tables() would provide, so
    # migration steps other than the one under test can run against this bare db
    for table in MEDIA_TABLES:
        await db.execute(
            f"CREATE TABLE {table}([item_id] INTEGER PRIMARY KEY, "
            "[external_ids] json NOT NULL DEFAULT '[]'"
            # every playlists table at the schema versions under test carries this column
            + (
                ", [supported_mediatypes] json NOT NULL DEFAULT '[\"track\"]'"
                if table == "playlists"
                else ""
            )
            + ")"
        )
    await db.execute(
        f"CREATE TABLE {DB_TABLE_EXTERNAL_ID_LOOKUP}([media_type] TEXT NOT NULL, "
        "[external_id_type] TEXT NOT NULL, [external_id] TEXT NOT NULL, "
        "[item_id] INTEGER NOT NULL)"
    )
    # tests that exercise a specific playlog layout replace this stand-in
    await db.execute(
        f"CREATE TABLE {DB_TABLE_LEGACY_PLAYLOG}([id] INTEGER PRIMARY KEY, [userid] TEXT NOT NULL, "
        "[playback_speed] REAL NOT NULL DEFAULT 1.0, "
        "UNIQUE(userid))"
    )
    await db.commit()
    yield db
    await db.close()


# the exact upsert used by MusicController._credit_artist_plays - it targets the
# 4-column unique constraint, so it raises IntegrityError on databases that still
# carry the legacy 3-column constraint (issue #5754)
PLAYLOG_UPSERT = (
    f"INSERT INTO {DB_TABLE_LEGACY_PLAYLOG} "
    "(item_id, provider, media_type, name, image, fully_played, "
    "seconds_played, timestamp, queue_id, user_initiated, userid) "
    "VALUES (:item_id, :provider, :media_type, :name, :image, :fully_played, "
    ":seconds_played, :timestamp, :queue_id, :user_initiated, :userid) "
    "ON CONFLICT(item_id, provider, media_type, userid) DO UPDATE SET "
    "timestamp = excluded.timestamp"
)
MEDIA_PROGRESS_UPSERT = PLAYLOG_UPSERT.replace("playlog", "media_progress")


def _playlog_entry(userid: str, timestamp: int = 100) -> dict[str, object]:
    return {
        "item_id": "1",
        "provider": "library",
        "media_type": "track",
        "name": "Test Track",
        "image": None,
        "fully_played": 1,
        "seconds_played": 195,
        "timestamp": timestamp,
        "queue_id": "queue1",
        "user_initiated": 1,
        "userid": userid,
    }


async def _create_legacy_playlog_table(database: DatabaseConnection) -> None:
    """Create the playlog table as it exists on pre-userid installs."""
    await database.execute(f"DROP TABLE {DB_TABLE_LEGACY_PLAYLOG}")
    # original table layout (schema version <= 22) with the 3-column UNIQUE constraint
    await database.execute(
        f"""CREATE TABLE {DB_TABLE_LEGACY_PLAYLOG}(
            [id] INTEGER PRIMARY KEY AUTOINCREMENT,
            [item_id] TEXT NOT NULL,
            [provider] TEXT NOT NULL,
            [media_type] TEXT NOT NULL,
            [name] TEXT NOT NULL,
            [image] json,
            [timestamp] INTEGER DEFAULT 0,
            [fully_played] BOOLEAN,
            [seconds_played] INTEGER,
            UNIQUE(item_id, provider, media_type));"""
    )
    # columns + index added in-place by the later ALTER TABLE migrations
    await database.execute(f"ALTER TABLE {DB_TABLE_LEGACY_PLAYLOG} ADD COLUMN userid TEXT")
    await database.execute(f"ALTER TABLE {DB_TABLE_LEGACY_PLAYLOG} ADD COLUMN queue_id TEXT")
    await database.execute(
        f"ALTER TABLE {DB_TABLE_LEGACY_PLAYLOG} ADD COLUMN user_initiated BOOLEAN NOT NULL DEFAULT 1"
    )
    await database.execute(
        f"ALTER TABLE {DB_TABLE_LEGACY_PLAYLOG} ADD COLUMN playback_speed REAL NOT NULL DEFAULT 1.0"
    )
    await database.execute(f"ALTER TABLE {DB_TABLE_LEGACY_PLAYLOG} ADD COLUMN artists json")
    await database.execute(
        f"CREATE UNIQUE INDEX {DB_TABLE_LEGACY_PLAYLOG}_unique_idx "
        f"ON {DB_TABLE_LEGACY_PLAYLOG}(item_id,provider,media_type,userid)"
    )
    await database.commit()


async def _table_columns(database: DatabaseConnection, table: str) -> set[str]:
    """Return the column names of the given table."""
    return {
        column["name"]
        for column in await database.get_rows_from_query(f"PRAGMA table_info({table})", limit=0)
    }


async def test_migration_rebuilds_playlog_with_stale_unique_constraint(
    database: DatabaseConnection,
) -> None:
    """The legacy 3-column UNIQUE constraint on playlog is dropped by a table rebuild."""
    await _create_legacy_playlog_table(database)
    await database.execute(PLAYLOG_UPSERT, _playlog_entry("user1"))
    # a legacy row from before the userid column existed
    await database.execute(
        f"INSERT INTO {DB_TABLE_LEGACY_PLAYLOG} (item_id, provider, media_type, name) "
        "VALUES ('2', 'library', 'track', 'Legacy Track')"
    )
    await database.commit()

    mass = MagicMock()
    mass.cache.clear = AsyncMock()
    await migrate_database(
        mass,
        database,
        MagicMock(),
        prev_version=48,
        create_tables=AsyncMock(),
    )

    # replaying the same item for the same user updates the existing row in place
    await database.execute(MEDIA_PROGRESS_UPSERT, _playlog_entry("user1", timestamp=200))
    # another user playing the same item gets their own row
    await database.execute(MEDIA_PROGRESS_UPSERT, _playlog_entry("user2"))
    rows = await database.get_rows(DB_TABLE_MEDIA_PROGRESS, {"item_id": "1"})
    assert len(rows) == 2
    user1_row = next(row for row in rows if row["userid"] == "user1")
    assert user1_row["timestamp"] == 200
    assert user1_row["name"] == "Test Track"
    # legacy rows without a userid cannot be kept under the NOT NULL schema
    assert not await database.get_rows(DB_TABLE_MEDIA_PROGRESS, {"item_id": "2"})


async def test_migration_renames_current_playlog_and_keeps_unique_progress_state(
    database: DatabaseConnection,
) -> None:
    """A current playlog table is renamed without changing its progress constraint."""
    await database.execute(f"DROP TABLE {DB_TABLE_LEGACY_PLAYLOG}")
    await database.execute(
        f"""CREATE TABLE {DB_TABLE_LEGACY_PLAYLOG}(
            [id] INTEGER PRIMARY KEY AUTOINCREMENT,
            [item_id] TEXT NOT NULL,
            [provider] TEXT NOT NULL,
            [media_type] TEXT NOT NULL,
            [name] TEXT NOT NULL,
            [image] json,
            [artists] json,
            [timestamp] INTEGER DEFAULT 0,
            [fully_played] BOOLEAN,
            [seconds_played] INTEGER,
            [userid] TEXT NOT NULL,
            [queue_id] TEXT,
            [user_initiated] BOOLEAN NOT NULL DEFAULT 1,
            [playback_speed] REAL NOT NULL DEFAULT 1.0,
            UNIQUE(item_id, provider, media_type, userid));"""
    )
    await database.execute(PLAYLOG_UPSERT, _playlog_entry("user1"))
    await database.execute(
        f"CREATE UNIQUE INDEX {DB_TABLE_LEGACY_PLAYLOG}_unique_idx "
        f"ON {DB_TABLE_LEGACY_PLAYLOG}(item_id,provider,media_type,userid)"
    )
    await database.execute(
        f"CREATE INDEX {DB_TABLE_LEGACY_PLAYLOG}_userid_timestamp_idx "
        f"ON {DB_TABLE_LEGACY_PLAYLOG}(userid,timestamp)"
    )
    await database.execute(
        f"CREATE INDEX {DB_TABLE_LEGACY_PLAYLOG}_provider_media_type_idx "
        f"ON {DB_TABLE_LEGACY_PLAYLOG}(provider,media_type,userid,timestamp)"
    )
    await database.commit()
    table_sql_query = (
        f"SELECT sql FROM sqlite_master WHERE type = 'table' AND name = '{DB_TABLE_LEGACY_PLAYLOG}'"
    )
    table_sql_before = (await database.get_rows_from_query(table_sql_query))[0]["sql"]

    mass = MagicMock()
    mass.cache.clear = AsyncMock()
    await migrate_database(
        mass,
        database,
        MagicMock(),
        prev_version=65,
        create_tables=AsyncMock(),
    )

    progress_sql_query = (
        f"SELECT sql FROM sqlite_master WHERE type = 'table' AND name = '{DB_TABLE_MEDIA_PROGRESS}'"
    )
    progress_table_sql = (await database.get_rows_from_query(progress_sql_query))[0]["sql"]
    assert "UNIQUE(item_id, provider, media_type, userid)" in progress_table_sql
    assert DB_TABLE_LEGACY_PLAYLOG in table_sql_before
    rows = await database.get_rows(DB_TABLE_MEDIA_PROGRESS)
    assert len(rows) == 1
    assert not await database.get_row(
        "sqlite_master", {"type": "table", "name": DB_TABLE_LEGACY_PLAYLOG}
    )
    assert not await database.get_rows(DB_TABLE_PLAY_HISTORY)
    indexes = {
        row["name"]
        for row in await database.get_rows_from_query(
            "SELECT name FROM sqlite_master WHERE type = 'index'", limit=0
        )
    }
    assert not any(name.startswith(f"{DB_TABLE_LEGACY_PLAYLOG}_") for name in indexes)


async def test_migrate_database_rejects_too_old_schema() -> None:
    """Schema versions older than the minimum supported version are refused up-front."""
    create_tables = AsyncMock()
    with pytest.raises(MusicAssistantError):
        await migrate_database(
            MagicMock(),  # mass
            MagicMock(),  # database
            MagicMock(),  # logger
            prev_version=14,
            create_tables=create_tables,
        )
    # the guard fires before any schema work happens
    create_tables.assert_not_awaited()


async def test_migrate_database_backfills_external_id_lookup(
    mass_minimal: MusicAssistant,
) -> None:
    """A pre-lookup-table database with populated external_ids columns upgrades cleanly."""
    # populate a fresh library database with a track carrying external ids
    music = MusicController(mass_minimal)
    mass_minimal.music = music
    await music._setup_database()
    table_names = {
        row["name"]
        for row in await music.database.get_rows_from_query(
            "SELECT name FROM sqlite_master WHERE type = 'table'", limit=0
        )
    }
    assert DB_TABLE_MEDIA_PROGRESS in table_names
    assert DB_TABLE_PLAY_HISTORY in table_names
    assert DB_TABLE_LEGACY_PLAYLOG not in table_names
    library_track = await music.tracks.add_item_to_library(create_track("spotify_1", "track_abc"))
    db_id = int(library_track.item_id)
    # revert the database to its v49 state: no lookup table, external ids stored
    # in an (indexed) external_ids JSON column on every media item table
    await music.database.execute(f"DROP TABLE {DB_TABLE_EXTERNAL_ID_LOOKUP}")
    for table in MEDIA_TABLES:
        await music.database.execute(
            f"ALTER TABLE {table} ADD COLUMN external_ids json NOT NULL DEFAULT '[]'"
        )
    for table in ("tracks", "artists"):
        await music.database.execute(
            f"CREATE INDEX IF NOT EXISTS {table}_external_ids_idx on {table}(external_ids)"
        )
    await music.database.execute(
        "UPDATE tracks SET external_ids = :external_ids WHERE item_id = :item_id",
        {"external_ids": f'[["isrc","{ISRC}"]]', "item_id": db_id},
    )
    await music.database.execute(
        f"ALTER TABLE {DB_TABLE_MEDIA_PROGRESS} RENAME TO {DB_TABLE_LEGACY_PLAYLOG}"
    )
    await music.database.insert_or_replace(
        DB_TABLE_SETTINGS, {"key": "version", "value": "49", "type": "str"}
    )
    await music.database.commit()
    await music.database.close()

    # setting up the database again triggers the migration
    mass_minimal.cache.clear = AsyncMock()  # type: ignore[method-assign]
    await music._setup_database()

    # the lookup table is backfilled from the external_ids JSON columns
    lookup_rows = await music.database.get_rows(DB_TABLE_EXTERNAL_ID_LOOKUP)
    assert {
        (x["media_type"], x["external_id_type"], x["external_id"], x["item_id"])
        for x in lookup_rows
    } == {("track", str(ExternalID.ISRC), ISRC, db_id)}
    match = await music.tracks.get_library_item_by_external_id(ISRC, ExternalID.ISRC)
    assert match is not None
    assert int(match.item_id) == db_id
    # the external_ids columns (and their unusable indexes) are dropped;
    # the lookup table is now the single source of truth
    for table in MEDIA_TABLES:
        assert "external_ids" not in await _table_columns(music.database, table)
    old_indexes = await music.database.get_rows_from_query(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE '%_external_ids_idx'"
    )
    assert not old_indexes
    await music.database.close()


async def test_migration_repairs_null_smart_fades_centroids(
    database: DatabaseConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Null spectral centroid values in legacy Smart Fades analysis rows become 0.0."""
    # keep the repaired rows in library.db; moving them out is tested on its own
    monkeypatch.setattr(migrations, "_move_audio_analysis_out", AsyncMock())
    await database.execute(
        f"""CREATE TABLE {DB_TABLE_AUDIO_ANALYSIS}(
            [id] INTEGER PRIMARY KEY AUTOINCREMENT,
            [aa_provider_domain] TEXT NOT NULL,
            [analysis_data] json NOT NULL)"""
    )
    rows = {
        1: ("smart_fades", '{"spectral_centroid": [1.5, null, 2.5, null], "bpm": 120}'),
        2: ("smart_fades", '{"spectral_centroid": [1.0, 2.0], "bpm": 100}'),
        # null centroids from another analysis provider must not be touched
        3: ("other_domain", '{"spectral_centroid": [null], "bpm": 100}'),
        # a corrupt payload must not abort the migration
        4: ("smart_fades", '{"spectral_centroid": [null'),
        # a non-array centroid value must not be touched
        5: ("smart_fades", '{"spectral_centroid": null, "bpm": 90}'),
        # "null" appearing only inside a string value must not trigger a rewrite
        6: ("smart_fades", '{"spectral_centroid": [3.5], "key": "nullish"}'),
    }
    for row_id, (domain, analysis_data) in rows.items():
        await database.execute(
            f"INSERT INTO {DB_TABLE_AUDIO_ANALYSIS} (id, aa_provider_domain, analysis_data) "
            "VALUES (:id, :domain, :analysis_data)",
            {"id": row_id, "domain": domain, "analysis_data": analysis_data},
        )
    await database.commit()

    mass = MagicMock()
    mass.cache.clear = AsyncMock()
    await migrate_database(
        mass,
        database,
        MagicMock(),
        prev_version=52,
        create_tables=AsyncMock(),
    )

    repaired = {
        row["id"]: row["analysis_data"] for row in await database.get_rows(DB_TABLE_AUDIO_ANALYSIS)
    }
    assert json.loads(repaired[1]) == {"spectral_centroid": [1.5, 0.0, 2.5, 0.0], "bpm": 120}
    # untouched rows must not be rewritten at all, hence the exact-string compare
    for untouched_id in (2, 3, 4, 5, 6):
        assert repaired[untouched_id] == rows[untouched_id][1]


async def test_migration_populates_fts_tables(database: DatabaseConnection) -> None:
    """Migrating a pre-FTS database builds and fills the FTS search tables."""
    await database.execute("DROP TABLE tracks")
    await database.execute(
        "CREATE TABLE tracks([item_id] INTEGER PRIMARY KEY, "
        "[external_ids] json NOT NULL DEFAULT '[]', [search_name] TEXT NOT NULL)"
    )
    await database.execute(
        "INSERT INTO tracks(item_id, search_name) VALUES (1, 'bohemianrhapsody')"
    )
    await database.execute("INSERT INTO tracks(item_id, search_name) VALUES (2, 'radiogaga')")
    await database.commit()

    mass = MagicMock()
    mass.cache.clear = AsyncMock()
    await migrate_database(
        mass,
        database,
        MagicMock(),
        prev_version=51,
        create_tables=AsyncMock(),
    )

    rows = await database.get_rows_from_query(
        "SELECT rowid FROM tracks_fts WHERE tracks_fts MATCH :term", {"term": '"rhapsody"'}
    )
    assert [row["rowid"] for row in rows] == [1]
    # tables without a search_name column (stand-ins in this bare test db) are skipped
    rows = await database.get_rows_from_query(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'albums_fts'"
    )
    assert not rows


async def test_migration_rewrites_apple_music_artwork_to_tokens(
    database: DatabaseConnection,
) -> None:
    """Persisted (expired) blobstore artwork URLs are rewritten to resolvable tokens."""
    await database.execute("ALTER TABLE albums ADD COLUMN metadata json")
    await database.execute(
        "CREATE TABLE provider_mappings([media_type] TEXT, [item_id] INTEGER, "
        "[provider_domain] TEXT, [provider_instance] TEXT, [provider_item_id] TEXT)"
    )
    signed_url = "https://store-033.blobstore.apple.com/pic/image?X-Amz-Signature=dead"
    metadata = {
        "images": [
            {
                "type": "thumb",
                "path": signed_url,
                "provider": "apple_music--1",
                "remotely_accessible": True,
            },
            {
                "type": "fanart",
                "path": "https://tadb/fanart.jpg",
                "provider": "theaudiodb",
                "remotely_accessible": True,
            },
            {
                "type": "thumb",
                "path": signed_url,
                "provider": "apple_music--removed",
                "remotely_accessible": True,
            },
        ]
    }
    await database.execute(
        "INSERT INTO albums (item_id, metadata) VALUES (1, :metadata)",
        {"metadata": json.dumps(metadata)},
    )
    # an unrelated row without apple artwork must be left untouched
    await database.execute(
        "INSERT INTO albums (item_id, metadata) VALUES (2, :metadata)",
        {"metadata": json.dumps({"images": [{"path": "https://x/y.jpg", "provider": "spotify"}]})},
    )
    await database.execute(
        "INSERT INTO provider_mappings "
        "(media_type, item_id, provider_domain, provider_instance, provider_item_id) "
        "VALUES ('album', 1, 'apple_music', 'apple_music--1', 'l.abc123')"
    )
    await database.commit()

    mass = MagicMock()
    mass.cache.clear = AsyncMock()
    await migrate_database(
        mass,
        database,
        MagicMock(),
        prev_version=54,
        create_tables=AsyncMock(),
    )

    rows = await database.get_rows_from_query(
        "SELECT item_id, metadata FROM albums ORDER BY item_id"
    )
    images = json.loads(rows[0]["metadata"])["images"]
    # the mapped entry became a token, the metadata-provider entry survived and
    # the entry whose apple instance no longer exists was dropped
    assert [(img["path"], img["provider"], img["remotely_accessible"]) for img in images] == [
        ("album/l.abc123", "apple_music--1", False),
        ("https://tadb/fanart.jpg", "theaudiodb", True),
    ]
    assert json.loads(rows[1]["metadata"])["images"] == [
        {"path": "https://x/y.jpg", "provider": "spotify"}
    ]


async def test_migration_strips_sound_effect_from_playlists(
    database: DatabaseConnection,
) -> None:
    """The sound effect media type is removed from the stored playlists."""
    await database.execute(
        "INSERT INTO playlists (item_id, supported_mediatypes) VALUES "
        '(1, \'["track","sound_effect","radio"]\'), '
        "(2, '[\"track\"]'), "
        "(3, 'corrupt value naming sound_effect'), "
        "(4, '[\"sound_effect\"]')"
    )
    await database.commit()

    mass = MagicMock()
    mass.cache.clear = AsyncMock()
    await migrate_database(
        mass,
        database,
        MagicMock(),
        prev_version=55,
        create_tables=AsyncMock(),
    )

    rows = await database.get_rows_from_query(
        "SELECT item_id, supported_mediatypes FROM playlists ORDER BY item_id"
    )
    assert json.loads(rows[0]["supported_mediatypes"]) == ["track", "radio"]
    # playlists without the media type, and rows we cannot parse, are left alone
    assert json.loads(rows[1]["supported_mediatypes"]) == ["track"]
    assert rows[2]["supported_mediatypes"] == "corrupt value naming sound_effect"
    # a playlist left with nothing yields an empty list, not NULL (the column is NOT NULL)
    assert json.loads(rows[3]["supported_mediatypes"]) == []


async def test_migration_adds_columns_leapfrogged_by_the_stable_schema_version(
    database: DatabaseConnection,
) -> None:
    """A stable database gets the columns its own schema version made it skip."""
    # the stable branch numbers its schema versions independently: its v43 already has the
    # 4-column playlog constraint, but never got playback_speed or the playlist translation
    # columns, which this branch gates behind steps a v43 database no longer runs
    await database.execute(f"DROP TABLE {DB_TABLE_LEGACY_PLAYLOG}")
    await database.execute(
        f"""CREATE TABLE {DB_TABLE_LEGACY_PLAYLOG}(
            [id] INTEGER PRIMARY KEY AUTOINCREMENT,
            [item_id] TEXT NOT NULL,
            [provider] TEXT NOT NULL,
            [media_type] TEXT NOT NULL,
            [name] TEXT NOT NULL,
            [image] json,
            [timestamp] INTEGER DEFAULT 0,
            [fully_played] BOOLEAN,
            [seconds_played] INTEGER,
            [userid] TEXT NOT NULL,
            [queue_id] TEXT,
            [user_initiated] BOOLEAN NOT NULL DEFAULT 1,
            UNIQUE(item_id, provider, media_type, userid));"""
    )
    await database.commit()

    mass = MagicMock()
    mass.cache.clear = AsyncMock()
    await migrate_database(
        mass,
        database,
        MagicMock(),
        prev_version=43,
        create_tables=AsyncMock(),
    )

    assert {"translation_key", "translation_params"} <= await _table_columns(database, "playlists")
    assert "playback_speed" in await _table_columns(database, DB_TABLE_MEDIA_PROGRESS)


async def test_migration_adds_is_dynamic_column_to_radios(database: DatabaseConnection) -> None:
    """A pre-58 database gets the radios.is_dynamic column, mirroring the playlist one."""
    assert "is_dynamic" not in await _table_columns(database, "radios")

    mass = MagicMock()
    mass.cache.clear = AsyncMock()
    await migrate_database(
        mass,
        database,
        MagicMock(),
        prev_version=57,
        create_tables=AsyncMock(),
    )

    assert "is_dynamic" in await _table_columns(database, "radios")


async def test_migration_adds_access_column_to_playlists(database: DatabaseConnection) -> None:
    """A pre-59 database gets the playlists.access column; running it twice is harmless."""
    assert "access" not in await _table_columns(database, "playlists")

    mass = MagicMock()
    mass.cache.clear = AsyncMock()
    for _ in range(2):
        await migrate_database(
            mass,
            database,
            MagicMock(),
            prev_version=58,
            create_tables=AsyncMock(),
        )

    assert "access" in await _table_columns(database, "playlists")


async def test_migration_drops_none_provider_mappings(database: DatabaseConnection) -> None:
    """A pre-60 database drops the bogus "None" self-mappings and keeps the real ones."""
    await database.execute(
        f"CREATE TABLE {DB_TABLE_PROVIDER_MAPPINGS}([media_type] TEXT, [item_id] INTEGER, "
        "[provider_domain] TEXT, [provider_instance] TEXT, [provider_item_id] TEXT)"
    )
    await database.execute(
        f"INSERT INTO {DB_TABLE_PROVIDER_MAPPINGS} "
        "(media_type, item_id, provider_domain, provider_instance, provider_item_id) VALUES "
        "('artist', 1, 'qobuz', 'qobuz--1', 'q1'), "
        "('artist', 1, 'None', 'None', '1'), "
        "('artist', 2, 'None', 'None', '2')"
    )
    await database.commit()

    mass = MagicMock()
    mass.cache.clear = AsyncMock()
    # a second pass must be a harmless no-op
    for _ in range(2):
        await migrate_database(
            mass,
            database,
            MagicMock(),
            prev_version=59,
            create_tables=AsyncMock(),
        )

    rows = await database.get_rows_from_query(
        f"SELECT item_id, provider_domain, provider_instance FROM {DB_TABLE_PROVIDER_MAPPINGS}"
    )
    assert [(r["provider_domain"], r["provider_instance"]) for r in rows] == [("qobuz", "qobuz--1")]


async def _create_pre_61_favorites(database: DatabaseConnection) -> None:
    """Give the tracks table the favorite column (and its index) a pre-61 database has."""
    await database.execute("ALTER TABLE tracks ADD COLUMN favorite BOOLEAN NOT NULL DEFAULT 0")
    await database.execute(
        "ALTER TABLE tracks ADD COLUMN timestamp_modified INTEGER NOT NULL DEFAULT 0"
    )
    await database.execute("CREATE INDEX tracks_favorite_idx on tracks(favorite)")
    await database.execute(
        "INSERT INTO tracks (item_id, favorite, timestamp_modified) VALUES "
        "(1, 1, 111), (2, 1, 222), (3, 0, 333)"
    )
    await database.commit()


async def _favorite_rows(database: DatabaseConnection) -> list[tuple[str, int, int, int]]:
    """Return the favorites table as (user_id, item_id, favorite, timestamp) tuples."""
    return [
        (row["user_id"], row["item_id"], row["favorite"], row["timestamp"])
        for row in await database.get_rows_from_query(
            f"SELECT * FROM {DB_TABLE_FAVORITES} WHERE media_type = 'track' "
            "ORDER BY user_id, item_id",
            limit=0,
        )
    ]


async def test_migration_parks_every_favorite_and_drops_the_column(
    database: DatabaseConnection,
) -> None:
    """Favorites wait under the placeholder user; a second pass over the database changes nothing."""
    await _create_pre_61_favorites(database)
    mass = MagicMock()
    mass.cache.clear = AsyncMock()

    for _ in range(2):
        await migrate_database(
            mass,
            database,
            MagicMock(),
            prev_version=60,
            create_tables=AsyncMock(),
        )

    # timestamped with the row's last change, the closest thing to the moment of the like
    assert await _favorite_rows(database) == [
        (PENDING_USER_ID, 1, 1, 111),
        (PENDING_USER_ID, 2, 1, 222),
    ]
    assert "favorite" not in await _table_columns(database, "tracks")
    assert not await database.get_rows_from_query(
        "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'tracks_favorite_idx'"
    )


async def test_migration_survives_a_favorite_without_a_modification_timestamp(
    database: DatabaseConnection,
) -> None:
    """A table without timestamp_modified still keeps its favorites."""
    await database.execute("ALTER TABLE tracks ADD COLUMN favorite BOOLEAN NOT NULL DEFAULT 0")
    await database.execute("INSERT INTO tracks (item_id, favorite) VALUES (1, 1), (2, 0)")
    await database.commit()
    mass = MagicMock()
    mass.cache.clear = AsyncMock()

    await migrate_database(mass, database, MagicMock(), prev_version=60, create_tables=AsyncMock())

    assert await _favorite_rows(database) == [(PENDING_USER_ID, 1, 1, 0)]
    assert "favorite" not in await _table_columns(database, "tracks")


def _image(image_type: str, path: str, provider: str) -> dict[str, object]:
    """Return a stored playlist image."""
    return {"type": image_type, "path": path, "provider": provider, "remotely_accessible": False}


async def _playlist_metadata(database: DatabaseConnection) -> dict[int, Any]:
    """Return the raw stored metadata of every playlist, by item id."""
    return {
        row["item_id"]: row["metadata"]
        for row in await database.get_rows_from_query(
            "SELECT item_id, metadata FROM playlists", limit=0
        )
    }


async def test_migration_drops_playlist_collages_and_system_playlist_artwork(
    database: DatabaseConnection, tmp_path: Path
) -> None:
    """
    Collages leave every playlist, generated artwork leaves the builtin system playlists.

    A playlist that lost its collage cover also loses its refresh timestamp, rows that can
    not be parsed are left alone and a second pass over the database changes nothing.
    """
    await database.execute("ALTER TABLE playlists ADD COLUMN metadata json")
    await database.execute(
        f"CREATE TABLE {DB_TABLE_PROVIDER_MAPPINGS}([media_type] TEXT, [item_id] INTEGER, "
        "[provider_domain] TEXT, [provider_instance] TEXT, [provider_item_id] TEXT)"
    )
    collage_thumb = _image("thumb", "/collage/abc_thumb.jpg", "builtin")
    collage_fanart = _image("fanart", "/collage/abc_fanart.jpg", "builtin")
    # a remote url and another provider's path that merely contain /collage/ are no collages
    remote_thumb = _image("thumb", "https://cdn.example.com/collage/abc.jpg", "spotify")
    foreign_fanart = _image("fanart", "/collage/cover.jpg", "filesystem_local")
    generated_thumb = _image("thumb", "/playlist_metadata_images/1_thumb.jpg", "playlist_metadata")
    generated_fanart = _image(
        "fanart", "/playlist_metadata_images/1_fanart.jpg", "playlist_metadata"
    )
    logo = _image("thumb", "logo.png", "builtin")
    fanart = _image("fanart", "fanart.jpg", "builtin")
    stored_metadata = {
        1: json.dumps(
            {
                "images": [
                    collage_thumb,
                    remote_thumb,
                    "garbage",
                    collage_fanart,
                    foreign_fanart,
                    generated_thumb,
                ],
                "last_refresh": 1,
            }
        ),
        2: json.dumps({"images": [remote_thumb, collage_fanart], "last_refresh": 1}),
        3: "not json /collage/",
        4: '["/collage/abc_thumb.jpg"]',
        5: None,
        # the builtin "All favorited tracks" playlist and a user-created builtin playlist
        6: json.dumps(
            {"images": [logo, generated_thumb, collage_fanart, generated_fanart], "last_refresh": 1}
        ),
        7: json.dumps({"images": [generated_thumb], "last_refresh": 1}),
        # the builtin "Random artist" playlist, which never had a collage
        8: json.dumps(
            {"images": [logo, fanart, generated_thumb, generated_fanart], "last_refresh": 1}
        ),
        9: 42,
    }
    for item_id, metadata in stored_metadata.items():
        await database.execute(
            "INSERT INTO playlists (item_id, metadata) VALUES (:item_id, :metadata)",
            {"item_id": item_id, "metadata": metadata},
        )
    await database.execute(
        f"INSERT INTO {DB_TABLE_PROVIDER_MAPPINGS} "
        "(media_type, item_id, provider_domain, provider_instance, provider_item_id) VALUES "
        "('playlist', 6, 'builtin', 'builtin', 'all_favorite_tracks'), "
        "('playlist', 7, 'builtin', 'builtin', 'my_playlist'), "
        "('playlist', 8, 'builtin', 'builtin', 'random_artist')"
    )
    await database.commit()
    collage_file = tmp_path / "collage_images" / "abc_thumb.jpg"
    collage_file.parent.mkdir()
    collage_file.write_bytes(b"jpg")
    mass = MagicMock()
    mass.cache.clear = AsyncMock()
    mass.cache_path = str(tmp_path)

    await migrate_database(mass, database, MagicMock(), prev_version=61, create_tables=AsyncMock())
    migrated = await _playlist_metadata(database)
    await migrate_database(mass, database, MagicMock(), prev_version=61, create_tables=AsyncMock())

    assert await _playlist_metadata(database) == migrated
    assert json.loads(migrated[1]) == {
        "images": [remote_thumb, "garbage", foreign_fanart, generated_thumb]
    }
    # only a lost collage cover asks for a new one
    assert json.loads(migrated[2]) == {"images": [remote_thumb], "last_refresh": 1}
    for item_id in (6, 8):
        assert json.loads(migrated[item_id]) == {"images": [logo, fanart], "last_refresh": 1}
    for item_id in (3, 4, 5, 7, 9):
        assert migrated[item_id] == stored_metadata[item_id]
    assert not collage_file.parent.exists()


async def test_migration_clears_playlist_collages_from_the_playlog(
    database: DatabaseConnection, tmp_path: Path
) -> None:
    """The playlog forgets the collage of a played playlist, every other image stays."""
    await database.execute(f"ALTER TABLE {DB_TABLE_LEGACY_PLAYLOG} ADD COLUMN media_type TEXT")
    await database.execute(f"ALTER TABLE {DB_TABLE_LEGACY_PLAYLOG} ADD COLUMN image json")
    collage = _image("thumb", "/collage/abc_thumb.jpg", "builtin")
    remote = serialize_to_json(_image("thumb", "https://cdn.example.com/collage/a.jpg", "spotify"))
    foreign = serialize_to_json(_image("thumb", "/collage/cover.jpg", "filesystem_local"))
    stored_images = {
        "user1": ("playlist", serialize_to_json(collage)),
        "user2": ("playlist", json.dumps(collage)),
        "user3": ("playlist", remote),
        "user4": ("track", serialize_to_json(collage)),
        "user5": ("playlist", foreign),
    }
    for userid, (media_type, image) in stored_images.items():
        await database.execute(
            f"INSERT INTO {DB_TABLE_LEGACY_PLAYLOG} (userid, media_type, image) "
            "VALUES (:userid, :media_type, :image)",
            {"userid": userid, "media_type": media_type, "image": image},
        )
    await database.commit()
    mass = MagicMock()
    mass.cache.clear = AsyncMock()
    mass.cache_path = str(tmp_path)

    await migrate_database(mass, database, MagicMock(), prev_version=61, create_tables=AsyncMock())

    rows = await database.get_rows_from_query(
        f"SELECT userid, image FROM {DB_TABLE_MEDIA_PROGRESS}", limit=0
    )
    assert {row["userid"]: row["image"] for row in rows} == {
        "user1": None,
        "user2": None,
        "user3": remote,
        "user4": serialize_to_json(collage),
        "user5": foreign,
    }


async def test_migration_drops_images_without_a_path(
    database: DatabaseConnection, tmp_path: Path
) -> None:
    """
    Images with an empty path leave the library, so stations stop sharing one proxy id.

    Every other image stays, rows that can not be parsed are left alone and the playlog
    forgets an empty-path image of a played item.
    """
    for table in ("radios", "tracks"):
        await database.execute(f"ALTER TABLE {table} ADD COLUMN metadata json")
    await database.execute(f"ALTER TABLE {DB_TABLE_LEGACY_PLAYLOG} ADD COLUMN image json")
    empty = _image("thumb", "", "radiobrowser--abc")
    tunein = _image("thumb", "https://cdn-radiotime-logos.tunein.com/s1.png", "tunein")
    stored_radios = {
        1: serialize_to_json({"images": [empty, tunein], "last_refresh": 1}),
        2: json.dumps({"images": [empty]}),
        3: serialize_to_json({"images": [tunein, empty]}),
        4: serialize_to_json({"images": [tunein], "description": ""}),
        5: 'not json ""',
        6: None,
    }
    for item_id, metadata in stored_radios.items():
        await database.execute(
            "INSERT INTO radios (item_id, metadata) VALUES (:item_id, :metadata)",
            {"item_id": item_id, "metadata": metadata},
        )
    await database.execute(
        "INSERT INTO tracks (item_id, metadata) VALUES (1, :metadata)",
        {"metadata": serialize_to_json({"images": [empty, tunein]})},
    )
    for userid, image in (
        ("user1", serialize_to_json(empty)),
        ("user2", serialize_to_json(tunein)),
    ):
        await database.execute(
            f"INSERT INTO {DB_TABLE_LEGACY_PLAYLOG} (userid, image) VALUES (:userid, :image)",
            {"userid": userid, "image": image},
        )
    await database.commit()
    mass = MagicMock()
    mass.cache.clear = AsyncMock()
    mass.cache_path = str(tmp_path)

    await migrate_database(mass, database, MagicMock(), prev_version=62, create_tables=AsyncMock())

    radios = {
        row["item_id"]: row["metadata"]
        for row in await database.get_rows_from_query(
            "SELECT item_id, metadata FROM radios", limit=0
        )
    }
    assert json.loads(radios[1]) == {"images": [tunein], "last_refresh": 1}
    assert json.loads(radios[2]) == {"images": []}
    assert json.loads(radios[3]) == {"images": [tunein]}
    for item_id in (4, 5, 6):
        assert radios[item_id] == stored_radios[item_id]
    track_rows = await database.get_rows_from_query("SELECT metadata FROM tracks", limit=0)
    assert json.loads(track_rows[0]["metadata"]) == {"images": [tunein]}
    playlog_rows = await database.get_rows_from_query(
        f"SELECT userid, image FROM {DB_TABLE_MEDIA_PROGRESS}", limit=0
    )
    assert {row["userid"]: row["image"] for row in playlog_rows} == {
        "user1": None,
        "user2": serialize_to_json(tunein),
    }


async def _create_genre_tables(database: DatabaseConnection) -> None:
    """Replace the genres stand-in with the genre tables as they exist at schema 64."""
    await database.execute(f"DROP TABLE {DB_TABLE_GENRES}")
    await database.execute(
        f"CREATE TABLE {DB_TABLE_GENRES}([item_id] INTEGER PRIMARY KEY, "
        "[translation_key] TEXT, [genre_aliases] json NOT NULL DEFAULT '[]', "
        "[content_type] TEXT)"
    )
    await database.execute(
        f"CREATE TABLE {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING}([genre_id] INTEGER NOT NULL, "
        "[media_id] INTEGER NOT NULL, [media_type] TEXT NOT NULL, [alias] TEXT, "
        "[is_derived] BOOLEAN NOT NULL DEFAULT 0, [is_manual] BOOLEAN NOT NULL DEFAULT 0, "
        "UNIQUE(genre_id, media_id, media_type))"
    )


async def _genre_aliases(database: DatabaseConnection) -> dict[int, Any]:
    rows = await database.get_rows_from_query(
        f"SELECT item_id, genre_aliases FROM {DB_TABLE_GENRES}", limit=0
    )
    return {row["item_id"]: json.loads(row["genre_aliases"]) for row in rows}


async def test_migration_moves_misplaced_classical_genre_aliases(
    database: DatabaseConnection,
) -> None:
    """
    Aliases that are not classical leave the classical genre and its mappings.

    Moved aliases land on their new genre, other genres and manual mappings stay untouched
    and running the step twice changes nothing.
    """
    await _create_genre_tables(database)
    genres = {
        # music classical genre
        1: (
            "classical",
            None,
            ["classical", "Opera", "gamelan", "K-Pop", "Electronic", "Christian/Gospel"],
        ),
        2: ("asian_music", None, ["asian music", "K-Pop"]),
        3: ("marching_band", None, ["marching band", "Brass Band"]),
        # a classical genre in another taxonomy is left alone
        4: ("classical", "audiobook", ["classical", "K-Pop"]),
        5: ("pop", None, ["pop", "K-Pop"]),
    }
    for item_id, (translation_key, content_type, stored_aliases) in genres.items():
        await database.execute(
            f"INSERT INTO {DB_TABLE_GENRES} VALUES "
            "(:item_id, :translation_key, :genre_aliases, :content_type)",
            {
                "item_id": item_id,
                "translation_key": translation_key,
                "genre_aliases": serialize_to_json(stored_aliases),
                "content_type": content_type,
            },
        )
    mappings = [
        (1, 10, "Opera", 0),
        (1, 11, "Gamelan", 0),
        (1, 12, "K-Pop", 0),
        (1, 13, "electronic", 1),
        # raw tag variants the scanner matched to a removed alias in normalized form
        (1, 14, "Christian/Gospel", 0),
        (1, 15, " k-pop ", 0),
        (5, 12, "K-Pop", 0),
    ]
    for genre_id, media_id, alias, is_manual in mappings:
        await database.execute(
            f"INSERT INTO {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} "
            "(genre_id, media_id, media_type, alias, is_manual) "
            "VALUES (:genre_id, :media_id, 'track', :alias, :is_manual)",
            {"genre_id": genre_id, "media_id": media_id, "alias": alias, "is_manual": is_manual},
        )
    await database.commit()
    mass = MagicMock()
    mass.cache.clear = AsyncMock()

    for _ in range(2):
        await migrate_database(
            mass, database, MagicMock(), prev_version=64, create_tables=AsyncMock()
        )

    aliases = await _genre_aliases(database)
    assert aliases[1] == ["classical", "Opera"]
    assert aliases[2][:2] == ["asian music", "K-Pop"]
    assert "Gamelan" in aliases[2]
    assert "Thai Classical" in aliases[2]
    assert aliases[2].count("Gamelan") == 1
    assert aliases[3][:2] == ["marching band", "Brass Band"]
    assert aliases[3].count("Brass Band") == 1
    assert "Circus March" in aliases[3]
    assert aliases[4] == ["classical", "K-Pop"]
    assert aliases[5] == ["pop", "K-Pop"]
    mapping_rows = await database.get_rows_from_query(
        f"SELECT genre_id, media_id FROM {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING}", limit=0
    )
    assert {(row["genre_id"], row["media_id"]) for row in mapping_rows} == {
        (1, 10),
        (1, 13),
        (5, 12),
    }


async def test_migration_survives_unparsable_classical_genre_aliases(
    database: DatabaseConnection,
) -> None:
    """A classical genre with broken alias data is skipped instead of failing the migration."""
    await _create_genre_tables(database)
    await database.execute(
        f"INSERT INTO {DB_TABLE_GENRES} VALUES (1, 'classical', 'not json', NULL)"
    )
    await database.commit()
    mass = MagicMock()
    mass.cache.clear = AsyncMock()

    await migrate_database(mass, database, MagicMock(), prev_version=64, create_tables=AsyncMock())

    rows = await database.get_rows_from_query(f"SELECT genre_aliases FROM {DB_TABLE_GENRES}")
    assert rows[0]["genre_aliases"] == "not json"


async def test_migration_drops_stale_mappings_of_a_clean_classical_genre(
    database: DatabaseConnection,
) -> None:
    """Mappings made through a removed alias go, even when the alias list is already clean."""
    await _create_genre_tables(database)
    await database.execute(
        f"INSERT INTO {DB_TABLE_GENRES} VALUES (1, 'classical', :aliases, NULL)",
        {"aliases": serialize_to_json(["classical", "Opera"])},
    )
    # a second classical genre whose alias data can not be read
    await database.execute(f"INSERT INTO {DB_TABLE_GENRES} VALUES (2, 'classical', '42', NULL)")
    for genre_id, media_id, alias in ((1, 10, "Opera"), (1, 11, "K-Pop"), (2, 12, "Gamelan")):
        await database.execute(
            f"INSERT INTO {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} "
            "(genre_id, media_id, media_type, alias) "
            "VALUES (:genre_id, :media_id, 'track', :alias)",
            {"genre_id": genre_id, "media_id": media_id, "alias": alias},
        )
    await database.commit()
    mass = MagicMock()
    mass.cache.clear = AsyncMock()

    await migrate_database(mass, database, MagicMock(), prev_version=64, create_tables=AsyncMock())

    genre_rows = await database.get_rows_from_query(
        f"SELECT item_id, genre_aliases FROM {DB_TABLE_GENRES}", limit=0
    )
    assert {row["item_id"]: row["genre_aliases"] for row in genre_rows} == {
        1: serialize_to_json(["classical", "Opera"]),
        2: 42,
    }
    mapping_rows = await database.get_rows_from_query(
        f"SELECT media_id FROM {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING}", limit=0
    )
    assert [row["media_id"] for row in mapping_rows] == [10]
