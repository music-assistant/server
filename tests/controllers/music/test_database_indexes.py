"""Regression tests for library indexes on upgraded databases."""

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from music_assistant.constants import MEDIA_ITEM_DB_TABLES
from music_assistant.controllers.metadata.controller import _duplicate_album_sibling_guard
from music_assistant.controllers.music import MusicController
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


@pytest.mark.parametrize("legacy_column", [None, "name COLLATE NOCASE", "search_name"])
async def test_search_name_index_survives_legacy_index_name(
    controller: MusicController, legacy_column: str | None
) -> None:
    """Create the normalized-name index on fresh and both historical database layouts."""
    database = controller.database
    if legacy_column is not None:
        for table in MEDIA_ITEM_DB_TABLES:
            await database.execute(
                f"CREATE INDEX {table}_name_nocase_idx ON {table}({legacy_column})"
            )

    for _ in range(2):
        await controller._MusicDatabaseSetupMixin__create_database_indexes()  # type: ignore[attr-defined]
        for table in MEDIA_ITEM_DB_TABLES:
            indexes = await database.get_rows_from_query(f"PRAGMA index_list({table})", limit=0)
            indexed_columns = []
            for index in indexes:
                columns = await database.get_rows_from_query(
                    f"PRAGMA index_info({index['name']})", limit=0
                )
                indexed_columns.append([column["name"] for column in columns])
            assert ["search_name"] in indexed_columns
            if legacy_column is not None:
                legacy_columns = await database.get_rows_from_query(
                    f"PRAGMA index_info({table}_name_nocase_idx)", limit=0
                )
                assert [column["name"] for column in legacy_columns] == [
                    "name" if legacy_column == "name COLLATE NOCASE" else "search_name"
                ]


async def test_reconciliation_after_upgrade_avoids_quadratic_album_scan(
    controller: MusicController,
) -> None:
    """Bound SQLite work for duplicate detection in an upgraded library of distinct albums."""
    database = controller.database
    await database.execute("CREATE INDEX albums_name_nocase_idx ON albums(name COLLATE NOCASE)")
    await database._db.executemany(
        "INSERT INTO albums(name,sort_name,album_type,metadata,search_name,search_sort_name) "
        "VALUES (?,?,'album','{}',?,?)",
        [
            (f"Album {index}", f"album{index}", f"album{index}", f"album{index}")
            for index in range(2000)
        ],
    )
    await controller._MusicDatabaseSetupMixin__create_database_indexes()  # type: ignore[attr-defined]

    progress_calls = 0

    def progress() -> int:
        nonlocal progress_calls
        progress_calls += 1
        return int(progress_calls > 500)

    # An instruction budget avoids timing assertions and interrupts the old quadratic plan.
    await database._db.set_progress_handler(progress, 1000)
    try:
        rows = await database.get_rows_from_query(
            f"SELECT albums.item_id FROM albums WHERE {_duplicate_album_sibling_guard()} "
            "ORDER BY RANDOM()",
            limit=25,
        )
    finally:
        await database._db.set_progress_handler(None, 0)
    assert rows == []
