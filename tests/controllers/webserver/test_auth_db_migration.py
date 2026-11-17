"""Tests for the schema version checks and migrations of the auth database."""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.auth import AuthProviderType, UserRole
from music_assistant_models.errors import SetupFailedError

from music_assistant.controllers.webserver.auth import DB_SCHEMA_VERSION, AuthenticationManager
from music_assistant.controllers.webserver.controller import WebserverController
from music_assistant.controllers.webserver.helpers.login_flow import ExternalIdentity

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

USER_TABLES = ("users", "user_auth_providers", "auth_tokens")
V5_USER_COLUMNS = (
    "user_id, username, role, enabled, created_at, display_name, avatar_url, preferences, "
    "player_filter, provider_filter"
)


@pytest.fixture
async def auth_manager(mass_minimal: MusicAssistant) -> AsyncGenerator[AuthenticationManager]:
    """
    Provide the authentication manager of a minimal server, set up on a fresh auth.db.

    :param mass_minimal: The minimal server to set up the authentication manager on.
    """
    webserver = WebserverController(mass_minimal)
    mass_minimal.webserver = webserver
    webserver.config = await mass_minimal.config.get_core_config("webserver")
    mass_minimal.music = MagicMock()
    mass_minimal.music.playlists.release_user_playlists = AsyncMock()
    await webserver.auth.setup()
    try:
        yield webserver.auth
    finally:
        await webserver.auth.close()


def _db_path(auth_manager: AuthenticationManager) -> Path:
    """Return the path of the auth database file."""
    return Path(auth_manager.database.db_path)


def _backup_path(auth_manager: AuthenticationManager) -> Path:
    """Return the path the auth database is backed up to before a migration."""
    return Path(f"{auth_manager.database.db_path}.backup")


def _counts(db_path: Path) -> dict[str, int]:
    """Return the row count of each user table, read straight from the database file."""
    conn = sqlite3.connect(db_path)
    try:
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in USER_TABLES
        }
    finally:
        conn.close()


async def _live_counts(auth_manager: AuthenticationManager) -> dict[str, int]:
    """Return the row count of each user table through the open (exclusive) connection."""
    return {table: await auth_manager.database.get_count(table) for table in USER_TABLES}


def _get_stored_version(db_path: Path) -> str:
    """Return the stored schema version, read straight from the database file."""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = 'schema_version'").fetchone()
        return str(row[0])
    finally:
        conn.close()


def _set_stored_version(db_path: Path, value: str) -> None:
    """Overwrite the stored schema version in the (closed) database file."""
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            conn.execute("UPDATE settings SET value = ? WHERE key = 'schema_version'", (value,))
    finally:
        conn.close()


async def _populate(auth_manager: AuthenticationManager) -> None:
    """Fill a current-schema database with a user, a provider link and a token."""
    user = await auth_manager.create_user(username="Admin", role=UserRole.ADMIN)
    await auth_manager.link_user_to_provider(user, AuthProviderType.HOME_ASSISTANT, "ha_user_1")
    await auth_manager.create_token(user, "Kitchen tablet", is_long_lived=True)


def _schema(db_path: Path) -> dict[str, Any]:
    """Return the columns and indexes of every table, read straight from the database file."""
    conn = sqlite3.connect(db_path)
    try:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ]
        schema: dict[str, Any] = {}
        for table in tables:
            columns = sorted(tuple(row[1:]) for row in conn.execute(f"PRAGMA table_info({table})"))
            indexes = sorted(
                (
                    row[1],
                    row[2],
                    row[3],
                    row[4],
                    tuple(info[2] for info in conn.execute(f"PRAGMA index_info('{row[1]}')")),
                )
                for row in conn.execute(f"PRAGMA index_list({table})")
            )
            schema[table] = {"columns": columns, "indexes": indexes}
        schema["index_sql"] = sorted(
            row
            for row in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL"
            )
        )
        return schema
    finally:
        conn.close()


def _rows(db_path: Path, table: str, columns: str = "*") -> list[tuple[Any, ...]]:
    """
    Return the rows of a table, read straight from the database file.

    :param db_path: The database file.
    :param table: The table to read.
    :param columns: The columns to read.
    """
    conn = sqlite3.connect(db_path)
    try:
        return sorted(conn.execute(f"SELECT {columns} FROM {table}").fetchall())
    finally:
        conn.close()


def _execute(db_path: Path, *statements: str) -> None:
    """Run write statements on the (closed) database file."""
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            for statement in statements:
                conn.execute(statement)
    finally:
        conn.close()


def _downgrade_to_version_5(db_path: Path) -> None:
    """Turn a (closed) current-schema database into one as the version 5 schema left it."""
    _execute(
        db_path,
        "DROP INDEX idx_users_email",
        "DROP INDEX idx_user_identities_user",
        "DROP TABLE user_identities",
        "ALTER TABLE users DROP COLUMN email",
    )
    _set_stored_version(db_path, "5")


async def test_a_fresh_database_gets_the_current_schema_without_a_backup(
    auth_manager: AuthenticationManager,
) -> None:
    """A fresh install stores the current schema version and needs no backup."""
    row = await auth_manager.database.get_row("settings", {"key": "schema_version"})
    assert row is not None
    assert row["value"] == str(DB_SCHEMA_VERSION)
    assert not _backup_path(auth_manager).exists()


async def test_a_start_on_the_current_schema_keeps_every_row_and_makes_no_backup(
    auth_manager: AuthenticationManager,
) -> None:
    """A restart on a current database changes nothing and does not back it up."""
    await _populate(auth_manager)
    await auth_manager.close()
    before = _counts(_db_path(auth_manager))

    await auth_manager.setup()

    assert await _live_counts(auth_manager) == before == dict.fromkeys(USER_TABLES, 1)
    assert not _backup_path(auth_manager).exists()


async def test_an_upgrade_keeps_every_row_and_backs_up_the_database(
    auth_manager: AuthenticationManager,
) -> None:
    """A migration from an older version keeps every row and leaves a copy of the old file."""
    await _populate(auth_manager)
    await auth_manager.close()
    before = _counts(_db_path(auth_manager))
    _set_stored_version(_db_path(auth_manager), "3")

    await auth_manager.setup()

    assert await _live_counts(auth_manager) == before == dict.fromkeys(USER_TABLES, 1)
    assert await auth_manager.get_user_by_username("admin") is not None
    row = await auth_manager.database.get_row("settings", {"key": "schema_version"})
    assert row is not None
    assert row["value"] == str(DB_SCHEMA_VERSION)
    assert _counts(_backup_path(auth_manager)) == before
    assert _get_stored_version(_backup_path(auth_manager)) == "3"


async def test_running_the_migration_twice_changes_nothing(
    auth_manager: AuthenticationManager,
) -> None:
    """Each migration step is safe to run again on a database it already migrated."""
    await _populate(auth_manager)
    await auth_manager._migrate_database(2)
    await auth_manager.close()
    before = _counts(_db_path(auth_manager))
    await auth_manager.setup()

    await auth_manager._migrate_database(2)

    await auth_manager.close()
    assert _counts(_db_path(auth_manager)) == before == dict.fromkeys(USER_TABLES, 1)
    await auth_manager.setup()


@pytest.mark.parametrize(
    ("stored_version", "message"),
    [
        ("garbage", "not a valid schema version"),
        ("", "not a valid schema version"),
        ("0", "not a valid schema version"),
        (str(DB_SCHEMA_VERSION + 1), "newer than"),
    ],
    ids=["garbage", "empty", "zero", "newer"],
)
async def test_an_unusable_schema_version_aborts_without_touching_the_tables(
    auth_manager: AuthenticationManager, stored_version: str, message: str
) -> None:
    """
    A version that is unreadable or from a newer server aborts the start and drops nothing.

    :param stored_version: The schema version value stored in the database.
    :param message: Part of the error message the start is expected to fail with.
    """
    await _populate(auth_manager)
    await auth_manager.close()
    db_path = _db_path(auth_manager)
    before = _counts(db_path)
    _set_stored_version(db_path, stored_version)

    with pytest.raises(SetupFailedError, match=message) as excinfo:
        await auth_manager.setup()

    assert str(db_path) in str(excinfo.value)
    await auth_manager.close()
    assert _counts(db_path) == before == dict.fromkeys(USER_TABLES, 1)
    assert not _backup_path(auth_manager).exists()
    assert _get_stored_version(db_path) == stored_version
    _set_stored_version(db_path, str(DB_SCHEMA_VERSION))
    await auth_manager.setup()


async def test_a_version_5_database_migrates_to_the_fresh_schema(
    auth_manager: AuthenticationManager,
) -> None:
    """A version 5 database ends up with the schema of a fresh install and keeps its rows."""
    await _populate(auth_manager)
    await auth_manager.close()
    db_path = _db_path(auth_manager)
    fresh_schema = _schema(db_path)
    _downgrade_to_version_5(db_path)
    users = _rows(db_path, "users")
    links = _rows(db_path, "user_auth_providers")
    tokens = _rows(db_path, "auth_tokens")

    await auth_manager.setup()
    await auth_manager.close()

    assert _get_stored_version(db_path) == "6"
    assert _schema(db_path) == fresh_schema
    assert _rows(db_path, "users", V5_USER_COLUMNS) == users
    assert _rows(db_path, "users", "email") == [(None,)]
    assert _rows(db_path, "user_auth_providers") == links
    assert _rows(db_path, "auth_tokens") == tokens
    assert _rows(db_path, "user_identities") == []
    await auth_manager.setup()


async def test_running_the_version_6_step_twice_changes_nothing(
    auth_manager: AuthenticationManager,
) -> None:
    """The version 6 step is safe to run again on a database that already has everything."""
    await _populate(auth_manager)
    await auth_manager.close()
    db_path = _db_path(auth_manager)
    fresh_schema = _schema(db_path)
    rows = {table: _rows(db_path, table) for table in USER_TABLES}
    await auth_manager.setup()

    await auth_manager._migrate_database(5)
    await auth_manager._migrate_database(5)

    await auth_manager.close()
    assert _schema(db_path) == fresh_schema
    assert {table: _rows(db_path, table) for table in USER_TABLES} == rows
    await auth_manager.setup()


@pytest.mark.parametrize(
    "statements",
    [
        ("DROP INDEX idx_user_identities_user", "DROP TABLE user_identities"),
        ("DROP INDEX idx_user_identities_user", "DROP INDEX idx_users_email"),
    ],
    ids=["column-without-table", "table-without-indexes"],
)
async def test_a_half_applied_version_6_step_is_completed(
    auth_manager: AuthenticationManager, statements: tuple[str, ...]
) -> None:
    """
    A version 5 database that already holds part of the version 6 schema is completed.

    :param statements: The statements that remove part of the version 6 schema.
    """
    await _populate(auth_manager)
    await auth_manager.close()
    db_path = _db_path(auth_manager)
    fresh_schema = _schema(db_path)
    _execute(db_path, *statements)
    _set_stored_version(db_path, "5")

    await auth_manager.setup()
    await auth_manager.close()

    assert _get_stored_version(db_path) == "6"
    assert _schema(db_path) == fresh_schema
    await auth_manager.setup()


async def test_a_version_6_database_stamped_back_to_5_upgrades_again(
    auth_manager: AuthenticationManager,
) -> None:
    """An older server that stamps version 5 on a version 6 database loses nothing on upgrade."""
    await _populate(auth_manager)
    user = await auth_manager.get_user_by_username("admin")
    assert user is not None
    await auth_manager.update_user(user, email="admin@example.com")
    await auth_manager.add_identity(
        user.user_id, "oidc", ExternalIdentity(AuthProviderType.OIDC, "https://idp", "sub-1")
    )
    await auth_manager.close()
    db_path = _db_path(auth_manager)
    fresh_schema = _schema(db_path)
    identities = _rows(db_path, "user_identities")
    _set_stored_version(db_path, "5")

    await auth_manager.setup()

    assert await auth_manager.get_identity("https://idp", "sub-1") is not None
    migrated = await auth_manager.get_user(user.user_id)
    assert migrated is not None
    assert migrated.email == "admin@example.com"
    await auth_manager.close()
    assert _get_stored_version(db_path) == "6"
    assert _schema(db_path) == fresh_schema
    assert _rows(db_path, "user_identities") == identities
    await auth_manager.setup()
