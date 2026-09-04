"""Tests for the music library database migrations."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ExternalID
from music_assistant_models.errors import MusicAssistantError

from music_assistant.constants import (
    DB_TABLE_ALBUMS,
    DB_TABLE_AUDIO_ANALYSIS,
    DB_TABLE_AUDIOBOOKS,
    DB_TABLE_EXTERNAL_ID_LOOKUP,
    DB_TABLE_PLAYLOG,
    DB_TABLE_PROVIDER_MAPPINGS,
    DB_TABLE_SETTINGS,
)
from music_assistant.controllers.music import MusicController
from music_assistant.controllers.music.migrations import migrate_database
from music_assistant.helpers.database import DatabaseConnection
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
        f"CREATE TABLE {DB_TABLE_PLAYLOG}([id] INTEGER PRIMARY KEY, [userid] TEXT NOT NULL, "
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
    f"INSERT INTO {DB_TABLE_PLAYLOG} "
    "(item_id, provider, media_type, name, image, fully_played, "
    "seconds_played, timestamp, queue_id, user_initiated, userid) "
    "VALUES (:item_id, :provider, :media_type, :name, :image, :fully_played, "
    ":seconds_played, :timestamp, :queue_id, :user_initiated, :userid) "
    "ON CONFLICT(item_id, provider, media_type, userid) DO UPDATE SET "
    "timestamp = excluded.timestamp"
)


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
    await database.execute(f"DROP TABLE {DB_TABLE_PLAYLOG}")
    # original table layout (schema version <= 22) with the 3-column UNIQUE constraint
    await database.execute(
        f"""CREATE TABLE {DB_TABLE_PLAYLOG}(
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
    await database.execute(f"ALTER TABLE {DB_TABLE_PLAYLOG} ADD COLUMN userid TEXT")
    await database.execute(f"ALTER TABLE {DB_TABLE_PLAYLOG} ADD COLUMN queue_id TEXT")
    await database.execute(
        f"ALTER TABLE {DB_TABLE_PLAYLOG} ADD COLUMN user_initiated BOOLEAN NOT NULL DEFAULT 1"
    )
    await database.execute(
        f"ALTER TABLE {DB_TABLE_PLAYLOG} ADD COLUMN playback_speed REAL NOT NULL DEFAULT 1.0"
    )
    await database.execute(f"ALTER TABLE {DB_TABLE_PLAYLOG} ADD COLUMN artists json")
    await database.execute(
        f"CREATE UNIQUE INDEX {DB_TABLE_PLAYLOG}_unique_idx "
        f"ON {DB_TABLE_PLAYLOG}(item_id,provider,media_type,userid)"
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
        f"INSERT INTO {DB_TABLE_PLAYLOG} (item_id, provider, media_type, name) "
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
    await database.execute(PLAYLOG_UPSERT, _playlog_entry("user1", timestamp=200))
    # another user playing the same item gets their own row
    await database.execute(PLAYLOG_UPSERT, _playlog_entry("user2"))
    rows = await database.get_rows(DB_TABLE_PLAYLOG, {"item_id": "1"})
    assert len(rows) == 2
    user1_row = next(row for row in rows if row["userid"] == "user1")
    assert user1_row["timestamp"] == 200
    assert user1_row["name"] == "Test Track"
    # legacy rows without a userid cannot be kept under the NOT NULL schema
    assert not await database.get_rows(DB_TABLE_PLAYLOG, {"item_id": "2"})


async def test_migration_leaves_correct_playlog_untouched(
    database: DatabaseConnection,
) -> None:
    """A playlog table that already has the 4-column constraint is not rebuilt."""
    await database.execute(f"DROP TABLE {DB_TABLE_PLAYLOG}")
    await database.execute(
        f"""CREATE TABLE {DB_TABLE_PLAYLOG}(
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
    await database.commit()
    table_sql_query = (
        f"SELECT sql FROM sqlite_master WHERE type = 'table' AND name = '{DB_TABLE_PLAYLOG}'"
    )
    table_sql_before = (await database.get_rows_from_query(table_sql_query))[0]["sql"]

    mass = MagicMock()
    mass.cache.clear = AsyncMock()
    await migrate_database(
        mass,
        database,
        MagicMock(),
        prev_version=48,
        create_tables=AsyncMock(),
    )

    assert (await database.get_rows_from_query(table_sql_query))[0]["sql"] == table_sql_before
    rows = await database.get_rows(DB_TABLE_PLAYLOG)
    assert len(rows) == 1


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
    database: DatabaseConnection,
) -> None:
    """Null spectral centroid values in legacy Smart Fades analysis rows become 0.0."""
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
    await database.execute(f"DROP TABLE {DB_TABLE_PLAYLOG}")
    await database.execute(
        f"""CREATE TABLE {DB_TABLE_PLAYLOG}(
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
    assert "playback_speed" in await _table_columns(database, DB_TABLE_PLAYLOG)


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


BIBI_COVER = "https://cdn-images.dzcdn.net/images/cover/bibi/264x264-000000-80-0-0.jpg"
ALBUM_COVER = "https://cdn-images.dzcdn.net/images/cover/endgame/264x264-000000-80-0-0.jpg"


async def _seed_cross_media_type_mappings(
    database: DatabaseConnection, *, album_owns_own_mapping: bool
) -> None:
    """Seed an album and an audiobook that share a library item id and a provider item."""
    for table in (DB_TABLE_ALBUMS, DB_TABLE_AUDIOBOOKS):
        await database.execute(f"ALTER TABLE {table} ADD COLUMN metadata json")
    await database.execute(
        f"CREATE TABLE {DB_TABLE_PROVIDER_MAPPINGS}([media_type] TEXT NOT NULL, "
        "[item_id] INTEGER NOT NULL, [provider_domain] TEXT NOT NULL, "
        "[provider_instance] TEXT NOT NULL, [provider_item_id] TEXT NOT NULL)"
    )
    await database.execute(
        f"INSERT INTO {DB_TABLE_ALBUMS} (item_id, metadata) VALUES (9, :metadata)",
        {"metadata": json.dumps({"images": [{"path": BIBI_COVER}, {"path": ALBUM_COVER}]})},
    )
    await database.execute(
        f"INSERT INTO {DB_TABLE_AUDIOBOOKS} (item_id, metadata) VALUES (9, :metadata)",
        {"metadata": json.dumps({"images": [{"path": BIBI_COVER}]})},
    )
    mappings = [("audiobook", "14001886"), ("album", "14001886")]
    if album_owns_own_mapping:
        mappings.append(("album", "908993"))
    for media_type, provider_item_id in mappings:
        await database.execute(
            f"INSERT INTO {DB_TABLE_PROVIDER_MAPPINGS} VALUES "
            "(:media_type, 9, 'deezer', 'deezer_1', :provider_item_id)",
            {"media_type": media_type, "provider_item_id": provider_item_id},
        )
    await database.commit()


async def _run_cross_media_type_migration(database: DatabaseConnection) -> None:
    """Run the migration step that cleans up cross-media-type provider mappings."""
    mass = MagicMock()
    mass.cache.clear = AsyncMock()
    await migrate_database(mass, database, MagicMock(), prev_version=58, create_tables=AsyncMock())


async def test_migration_drops_cross_media_type_provider_mappings(
    database: DatabaseConnection,
) -> None:
    """A mapping that landed on an item of another media type is dropped with its artwork."""
    await _seed_cross_media_type_mappings(database, album_owns_own_mapping=True)

    await _run_cross_media_type_migration(database)

    album_mappings = await database.get_rows(DB_TABLE_PROVIDER_MAPPINGS, {"media_type": "album"})
    assert [row["provider_item_id"] for row in album_mappings] == ["908993"]
    # the audiobook owns the provider item, so its own mapping and artwork stay untouched
    audiobook_mappings = await database.get_rows(
        DB_TABLE_PROVIDER_MAPPINGS, {"media_type": "audiobook"}
    )
    assert [row["provider_item_id"] for row in audiobook_mappings] == ["14001886"]
    album = await database.get_row(DB_TABLE_ALBUMS, {"item_id": 9})
    assert album is not None
    assert json.loads(album["metadata"])["images"] == [{"path": ALBUM_COVER}]
    audiobook = await database.get_row(DB_TABLE_AUDIOBOOKS, {"item_id": 9})
    assert audiobook is not None
    assert json.loads(audiobook["metadata"])["images"] == [{"path": BIBI_COVER}]


async def test_migration_keeps_ambiguous_cross_media_type_mappings(
    database: DatabaseConnection,
) -> None:
    """An item whose only mapping collides is left alone: without one it cannot be resolved."""
    await _seed_cross_media_type_mappings(database, album_owns_own_mapping=False)

    await _run_cross_media_type_migration(database)

    assert len(await database.get_rows(DB_TABLE_PROVIDER_MAPPINGS)) == 2
    album = await database.get_row(DB_TABLE_ALBUMS, {"item_id": 9})
    assert album is not None
    assert len(json.loads(album["metadata"])["images"]) == 2
