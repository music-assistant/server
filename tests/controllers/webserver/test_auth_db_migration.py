"""Tests for the schema version checks and migrations of the auth database."""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.auth import AuthProviderType, UserRole
from music_assistant_models.errors import SetupFailedError

from music_assistant.controllers.webserver.auth import DB_SCHEMA_VERSION, AuthenticationManager
from music_assistant.controllers.webserver.controller import WebserverController

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

USER_TABLES = ("users", "user_auth_providers", "auth_tokens")


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
