"""Regression tests for library indexes on upgraded databases."""

from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from music_assistant.constants import DB_TABLE_EXTERNAL_ID_LOOKUP, MEDIA_ITEM_DB_TABLES
from music_assistant.controllers.music import MusicController
from music_assistant.controllers.music.migrations import migrate_database
from music_assistant.helpers.database import DatabaseConnection


@pytest.fixture
async def controller(tmp_path: Path) -> AsyncGenerator[MusicController]:
    """Return a library controller with only its database initialized."""
    database = DatabaseConnection(str(tmp_path / "library.db"))
    await database.setup()
    controller = MusicController.__new__(MusicController)
    controller._database = database
    await controller._MusicDatabaseSetupMixin__create_database_tables()  # type: ignore[attr-defined]
    try:
        yield controller
    finally:
        await database.close()


async def test_item_id_index_of_the_id_lookup_exists_with_its_table(
    controller: MusicController,
) -> None:
    """The index that library queries name exists before an upgrade step can read the library."""
    indexes = await controller.database.get_rows_from_query(
        f"PRAGMA index_list({DB_TABLE_EXTERNAL_ID_LOOKUP})", limit=0
    )

    assert f"{DB_TABLE_EXTERNAL_ID_LOOKUP}_item_id_idx" in {index["name"] for index in indexes}


@pytest.mark.parametrize("legacy_column", [None, "name COLLATE NOCASE", "search_name"])
@pytest.mark.parametrize("search_index_exists", [False, True])
async def test_migration_replaces_legacy_name_index(
    controller: MusicController, legacy_column: str | None, search_index_exists: bool
) -> None:
    """Migrate both legacy index layouts without leaving redundant indexes."""
    database = controller.database
    if legacy_column is not None:
        for table in MEDIA_ITEM_DB_TABLES:
            await database.execute(
                f"CREATE INDEX {table}_name_nocase_idx ON {table}({legacy_column})"
            )

    if search_index_exists:
        for table in MEDIA_ITEM_DB_TABLES:
            await database.execute(f"CREATE INDEX {table}_search_name_idx ON {table}(search_name)")

    mass = MagicMock()
    mass.cache.clear = AsyncMock()
    for _ in range(2):
        await migrate_database(
            mass, database, MagicMock(), prev_version=72, create_tables=AsyncMock()
        )
        await controller._MusicDatabaseSetupMixin__create_database_indexes()  # type: ignore[attr-defined]
        for table in MEDIA_ITEM_DB_TABLES:
            indexes = await database.get_rows_from_query(f"PRAGMA index_list({table})", limit=0)
            assert f"{table}_name_nocase_idx" not in {index["name"] for index in indexes}
            indexed_columns = []
            for index in indexes:
                columns = await database.get_rows_from_query(
                    f"PRAGMA index_info({index['name']})", limit=0
                )
                indexed_columns.append([column["name"] for column in columns])
            assert indexed_columns.count(["search_name"]) == 1
