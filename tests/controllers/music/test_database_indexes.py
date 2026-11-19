"""Regression tests for library queries on large databases."""

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

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


@pytest.mark.parametrize(
    ("shared_title", "shared_artist"), [(True, False), (False, True), (True, True)]
)
async def test_reconciliation_bounds_work_on_a_large_shared_title_or_artist(
    controller: MusicController, shared_title: bool, shared_artist: bool
) -> None:
    """Bound SQLite work when thousands of albums share a title, an artist or both."""
    database = controller.database
    count = 3000
    albums = []
    album_artists = []
    for index in range(1, count + 1):
        name = "Greatest Hits" if shared_title else f"Album {index}"
        albums.append((index, name, name.lower(), name.lower(), name.lower()))
        album_artists.append((index, 1 if shared_artist else index))
    await database._db.executemany(
        "INSERT INTO albums(item_id,name,sort_name,album_type,metadata,search_name,"
        "search_sort_name) VALUES (?,?,?,'album','{}',?,?)",
        albums,
    )
    await database._db.executemany(
        "INSERT INTO album_artists(album_id,artist_id) VALUES (?,?)", album_artists
    )

    progress_calls = 0

    def progress() -> int:
        nonlocal progress_calls
        progress_calls += 1
        return int(progress_calls > 5000)

    # An instruction budget avoids timing assertions and interrupts a quadratic plan.
    await database._db.set_progress_handler(progress, 1000)
    try:
        rows = await database.get_rows_from_query(
            f"SELECT albums.item_id FROM albums WHERE {_duplicate_album_sibling_guard()} "
            "ORDER BY RANDOM()",
            limit=25,
        )
    finally:
        await database._db.set_progress_handler(progress, 0)
    # only albums sharing both the title and the artist are duplicate candidates
    assert len(rows) == (25 if shared_title and shared_artist else 0)
