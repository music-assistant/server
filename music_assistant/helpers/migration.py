"""Logic to handle database/configuration changes and creation."""

import os
import shutil
import uuid

from pkg_resources import packaging

import aiosqlite
from music_assistant.constants import __version__ as app_version
from music_assistant.helpers.encryption import encrypt_string
from music_assistant.helpers.typing import MusicAssistant
from music_assistant.helpers.util import get_hostname


async def check_migrations(mass: MusicAssistant):
    """Check for any migrations that need to be done."""

    is_fresh_setup = len(mass.config.stored_config.keys()) == 0
    prev_version = packaging.version.parse(mass.config.stored_config.get("version", ""))

    # perform version specific migrations
    if not is_fresh_setup and prev_version < packaging.version.parse("0.1.1"):
        await run_migration_1(mass)

    # store version in config
    mass.config.stored_config["version"] = app_version
    # create unique server id from machine id
    if "server_id" not in mass.config.stored_config:
        mass.config.stored_config["server_id"] = str(uuid.getnode())
    if "jwt_key" not in mass.config.stored_config:
        mass.config.stored_config["jwt_key"] = await encrypt_string(str(uuid.uuid4()))
    if "initialized" not in mass.config.stored_config:
        mass.config.stored_config["initialized"] = False
    if "friendly_name" not in mass.config.stored_config:
        mass.config.stored_config["friendly_name"] = get_hostname()
    mass.config.save()

    # create default db tables (if needed)
    await create_db_tables(mass.database.db_file)


async def run_migration_1(mass: MusicAssistant):
    """Run migration for version 0.1.1."""
    # 0.1.0 introduced major changes to all data models and db structure
    # a full refresh of data is unavoidable
    data_path = mass.config.data_path
    tracks_loudness = []

    for dbname in ["mass.db", "database.db", "music_assistant.db"]:
        filename = os.path.join(data_path, dbname)
        if os.path.isfile(filename):
            # we try to backup the loudness measurements
            async with aiosqlite.connect(filename, timeout=120) as db_conn:
                db_conn.row_factory = aiosqlite.Row
                sql_query = "SELECT * FROM track_loudness"
                for db_row in await db_conn.execute_fetchall(sql_query, ()):
                    if "provider_track_id" in db_row.keys():
                        track_id = db_row["provider_track_id"]
                    else:
                        track_id = db_row["item_id"]
                    tracks_loudness.append(
                        (
                            track_id,
                            db_row["provider"],
                            db_row["loudness"],
                        )
                    )
            # remove old db file
            os.remove(filename)

    # remove old cache db
    for dbname in ["cache.db", ".cache.db"]:
        filename = os.path.join(data_path, dbname)
        if os.path.isfile(filename):
            os.remove(filename)

    # remove old thumbs db
    for dirname in ["thumbs", ".thumbs", ".thumbnails"]:
        dirname = os.path.join(data_path, dirname)
        if os.path.isdir(dirname):
            shutil.rmtree(dirname, True)

    # create default db tables (if needed)
    await create_db_tables(mass.database.db_file)

    # restore loudness measurements
    if tracks_loudness:
        async with aiosqlite.connect(mass.database.db_file, timeout=120) as db_conn:
            sql_query = """INSERT or REPLACE INTO track_loudness
                (item_id, provider, loudness) VALUES(?,?,?);"""
            for item in tracks_loudness:
                await db_conn.execute(sql_query, item)
            await db_conn.commit()


async def create_db_tables(db_file):
    """Async initialization."""
    async with aiosqlite.connect(db_file, timeout=120) as db_conn:

        await db_conn.execute(
            """CREATE TABLE IF NOT EXISTS provider_mappings(
                item_id INTEGER NOT NULL,
                media_type TEXT NOT NULL,
                prov_item_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                quality INTEGER NOT NULL,
                details TEXT NULL,
                UNIQUE(item_id, media_type, prov_item_id, provider, quality)
                );"""
        )

        await db_conn.execute(
            """CREATE TABLE IF NOT EXISTS artists(
                item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                sort_name TEXT,
                musicbrainz_id TEXT NOT NULL UNIQUE,
                in_library BOOLEAN DEFAULT 0,
                metadata json,
                provider_ids json
                );"""
        )

        await db_conn.execute(
            """CREATE TABLE IF NOT EXISTS albums(
                item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                sort_name TEXT,
                album_type TEXT,
                year INTEGER,
                version TEXT,
                in_library BOOLEAN DEFAULT 0,
                upc TEXT,
                artist json,
                metadata json,
                provider_ids json
            );"""
        )

        await db_conn.execute(
            """CREATE TABLE IF NOT EXISTS tracks(
                item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                sort_name TEXT,
                version TEXT,
                duration INTEGER,
                in_library BOOLEAN DEFAULT 0,
                isrc TEXT,
                albums json,
                artists json,
                metadata json,
                provider_ids json
            );"""
        )

        await db_conn.execute(
            """CREATE TABLE IF NOT EXISTS playlists(
                item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                sort_name TEXT,
                owner TEXT NOT NULL,
                is_editable BOOLEAN NOT NULL,
                checksum TEXT NOT NULL,
                in_library BOOLEAN DEFAULT 0,
                metadata json,
                provider_ids json,
                UNIQUE(name, owner)
                );"""
        )

        await db_conn.execute(
            """CREATE TABLE IF NOT EXISTS radios(
                item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                sort_name TEXT,
                in_library BOOLEAN DEFAULT 0,
                metadata json,
                provider_ids json
                );"""
        )

        await db_conn.execute(
            """CREATE TABLE IF NOT EXISTS track_loudness(
                item_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                loudness REAL,
                UNIQUE(item_id, provider));"""
        )

        await db_conn.execute(
            """CREATE TABLE IF NOT EXISTS playlog(
                item_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                timestamp REAL,
                UNIQUE(item_id, provider));"""
        )

        await db_conn.execute(
            """CREATE TABLE IF NOT EXISTS thumbs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                size INTEGER,
                UNIQUE(url, size));"""
        )

        await db_conn.commit()
        await db_conn.execute("VACUUM;")
        await db_conn.commit()
