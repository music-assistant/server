"""Tests for the controller-owned audio_analysis.db (attach, schema version, relocation)."""

from __future__ import annotations

import logging
import pathlib
import sqlite3
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import ProviderUnavailableError
from music_assistant_models.media_items import Track

import music_assistant.controllers.streams.audio_analysis as audio_analysis_mod
from music_assistant.constants import (
    DB_TABLE_AUDIO_ANALYSIS,
    DB_TABLE_AUDIO_ANALYSIS_FAILURES,
    DB_TABLE_SETTINGS,
)
from music_assistant.controllers.streams.audio_analysis import (
    AA_DB_FILENAME,
    AA_DB_SCHEMA,
    AA_DB_SCHEMA_VERSION,
    AA_TABLE_ANALYSIS,
    AA_TABLE_FAILURES,
    AudioAnalysisController,
)
from music_assistant.helpers.database import DatabaseConnection
from music_assistant.models.audio_analysis import AudioAnalysisData
from music_assistant.models.audio_analysis_provider import AudioAnalysisProvider

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from typing import Any


@pytest.fixture
async def library_db(tmp_path: pathlib.Path) -> AsyncGenerator[DatabaseConnection]:
    """Return a real on-disk library.db connection with nothing but a provider_mappings table."""
    db = DatabaseConnection(str(tmp_path / "library.db"))
    await db.setup()
    await db.execute(
        "CREATE TABLE provider_mappings("
        "provider_item_id TEXT, provider_instance TEXT, provider_domain TEXT, media_type TEXT)"
    )
    await db.commit()
    yield db
    await db.close()


def _make_controller(
    library_db: DatabaseConnection, tmp_path: pathlib.Path
) -> AudioAnalysisController:
    streams = MagicMock()
    mass = MagicMock()
    streams.mass = mass
    mass.music.database = library_db
    mass.storage_path = str(tmp_path)
    # a real logger, so self.logger (mass.logger.getChild(...)) propagates to caplog
    mass.logger = logging.getLogger("test_audio_analysis_database")
    return AudioAnalysisController(streams)


async def _table_names(db: DatabaseConnection, schema: str) -> set[str]:
    rows = await db.get_rows_from_query(
        f"SELECT name FROM {schema}.sqlite_master WHERE type = 'table'", limit=0
    )
    return {r["name"] for r in rows}


@pytest.mark.asyncio
async def test_setup_database_attaches_file_and_creates_tables(
    library_db: DatabaseConnection, tmp_path: pathlib.Path
) -> None:
    """Attaching creates the db file, its tables under the aa schema, and a version row."""
    ctrl = _make_controller(library_db, tmp_path)
    await ctrl.setup_database()

    assert (tmp_path / AA_DB_FILENAME).exists()
    tables = await _table_names(library_db, AA_DB_SCHEMA)
    assert {DB_TABLE_AUDIO_ANALYSIS, DB_TABLE_AUDIO_ANALYSIS_FAILURES, DB_TABLE_SETTINGS} <= tables
    # nothing was created in the library database itself
    assert DB_TABLE_AUDIO_ANALYSIS not in await _table_names(library_db, "main")
    version = await library_db.get_row(f"{AA_DB_SCHEMA}.{DB_TABLE_SETTINGS}", {"key": "version"})
    assert version is not None
    assert int(version["value"]) == AA_DB_SCHEMA_VERSION


@pytest.mark.asyncio
async def test_setup_database_is_idempotent(
    library_db: DatabaseConnection, tmp_path: pathlib.Path
) -> None:
    """Calling setup_database twice does not re-attach or duplicate the version row."""
    ctrl = _make_controller(library_db, tmp_path)
    await ctrl.setup_database()
    await ctrl.setup_database()
    rows = await library_db.get_rows(f"{AA_DB_SCHEMA}.{DB_TABLE_SETTINGS}", {"key": "version"})
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_no_vacuum_when_nothing_to_relocate(
    library_db: DatabaseConnection, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No legacy tables to relocate means no vacuum is run."""
    ctrl = _make_controller(library_db, tmp_path)
    mock_vacuum = AsyncMock()
    monkeypatch.setattr(library_db, "vacuum", mock_vacuum)
    await ctrl.setup_database()
    mock_vacuum.assert_not_awaited()


@pytest.mark.asyncio
async def test_attached_db_uses_wal_and_normal_locking(
    library_db: DatabaseConnection, tmp_path: pathlib.Path
) -> None:
    """The attached analysis file uses WAL journaling and normal (non-exclusive) locking."""
    ctrl = _make_controller(library_db, tmp_path)
    await ctrl.setup_database()
    journal = await library_db.get_rows_from_query(f"PRAGMA {AA_DB_SCHEMA}.journal_mode", limit=0)
    assert journal[0]["journal_mode"] == "wal"
    locking = await library_db.get_rows_from_query(f"PRAGMA {AA_DB_SCHEMA}.locking_mode", limit=0)
    assert locking[0]["locking_mode"] == "normal"
    limit_rows = await library_db.get_rows_from_query(
        f"PRAGMA {AA_DB_SCHEMA}.journal_size_limit", limit=0
    )
    assert limit_rows[0]["journal_size_limit"] == 6144000
    sync_rows = await library_db.get_rows_from_query(f"PRAGMA {AA_DB_SCHEMA}.synchronous", limit=0)
    assert sync_rows[0]["synchronous"] == 1


@pytest.mark.asyncio
async def test_unreadable_database_is_quarantined_and_recreated(
    library_db: DatabaseConnection, tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An unreadable analysis file is moved aside and replaced with a fresh one."""
    garbage = b"this is definitely not a sqlite database" * 8
    (tmp_path / AA_DB_FILENAME).write_bytes(garbage)
    ctrl = _make_controller(library_db, tmp_path)
    with caplog.at_level(logging.ERROR):
        await ctrl.setup_database()

    assert (tmp_path / f"{AA_DB_FILENAME}.corrupt").read_bytes() == garbage
    tables = await _table_names(library_db, AA_DB_SCHEMA)
    assert {DB_TABLE_AUDIO_ANALYSIS, DB_TABLE_AUDIO_ANALYSIS_FAILURES, DB_TABLE_SETTINGS} <= tables
    assert any(
        record.levelno == logging.ERROR and "unusable" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_setup_database_after_quarantine_is_idempotent(
    library_db: DatabaseConnection, tmp_path: pathlib.Path
) -> None:
    """A second setup_database call on the replacement file changes nothing."""
    garbage = b"this is definitely not a sqlite database" * 8
    (tmp_path / AA_DB_FILENAME).write_bytes(garbage)
    ctrl = _make_controller(library_db, tmp_path)
    await ctrl.setup_database()
    await ctrl.setup_database()

    assert (tmp_path / f"{AA_DB_FILENAME}.corrupt").read_bytes() == garbage
    rows = await library_db.get_rows(f"{AA_DB_SCHEMA}.{DB_TABLE_SETTINGS}", {"key": "version"})
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_delete_audio_analysis_removes_only_that_provider_key(
    library_db: DatabaseConnection, tmp_path: pathlib.Path
) -> None:
    """delete_audio_analysis removes only the rows matching the given item/provider key."""
    ctrl = _make_controller(library_db, tmp_path)
    await ctrl.setup_database()
    for provider, domain in (
        ("fs--a", "loudness_analysis"),
        ("fs--a", "smart_fades"),
        ("fs--b", "loudness_analysis"),
    ):
        await library_db.insert(
            AA_TABLE_ANALYSIS,
            {
                "media_type": "track",
                "item_id": "t1",
                "provider": provider,
                "aa_provider_domain": domain,
                "analysis_data": "{}",
                "analysis_version": 1,
            },
        )
    await ctrl.delete_audio_analysis("t1", "fs--a", MediaType.TRACK)
    rows = await library_db.get_rows(AA_TABLE_ANALYSIS, limit=0)
    assert [(r["provider"], r["aa_provider_domain"]) for r in rows] == [
        ("fs--b", "loudness_analysis")
    ]


LEGACY_ANALYSIS_DDL = (
    f"CREATE TABLE {DB_TABLE_AUDIO_ANALYSIS}("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, media_type TEXT NOT NULL, item_id TEXT NOT NULL, "
    "provider TEXT NOT NULL, aa_provider_domain TEXT NOT NULL, analysis_data json NOT NULL, "
    "analysis_version INTEGER DEFAULT 1, "
    "timestamp_created INTEGER DEFAULT (cast(strftime('%s','now') as int)), "
    "UNIQUE(item_id,provider,aa_provider_domain,media_type))"
)
LEGACY_FAILURES_DDL = (
    f"CREATE TABLE {DB_TABLE_AUDIO_ANALYSIS_FAILURES}("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, media_type TEXT NOT NULL, item_id TEXT NOT NULL, "
    "provider TEXT NOT NULL, aa_provider_domain TEXT NOT NULL, reason TEXT NOT NULL, "
    "analysis_version INTEGER NOT NULL DEFAULT 1, next_retry INTEGER, "
    "timestamp_created INTEGER DEFAULT (cast(strftime('%s','now') as int)), "
    "UNIQUE(item_id,provider,aa_provider_domain,media_type))"
)


async def _seed_legacy(db: DatabaseConnection, n_analysis: int, n_failures: int) -> None:
    await db.execute(LEGACY_ANALYSIS_DDL)
    await db.execute(LEGACY_FAILURES_DDL)
    for i in range(n_analysis):
        # explicit ids with a gap, and explicit timestamps, so preservation is observable
        await db.execute(
            f"INSERT INTO {DB_TABLE_AUDIO_ANALYSIS}"
            "(id, media_type, item_id, provider, aa_provider_domain, analysis_data, "
            " analysis_version, timestamp_created) VALUES "
            "(:id, 'track', :item, 'fs--a', 'loudness_analysis', :data, 2, :ts)",
            {
                "id": i * 3 + 1,
                "item": f"t{i}",
                "data": f'{{"loudness_integrated": {-i}}}',
                "ts": 1000 + i,
            },
        )
    for i in range(n_failures):
        await db.execute(
            f"INSERT INTO {DB_TABLE_AUDIO_ANALYSIS_FAILURES}"
            "(media_type, item_id, provider, aa_provider_domain, reason, analysis_version, next_retry)"
            " VALUES ('track', :item, 'fs--a', 'sonic_analysis', 'boom', 1, NULL)",
            {"item": f"f{i}"},
        )
    await db.commit()


@pytest.mark.asyncio
async def test_relocates_legacy_rows_and_drops_legacy_tables(
    library_db: DatabaseConnection, tmp_path: pathlib.Path
) -> None:
    """Legacy rows are copied over with fresh ids, timestamps preserved, source table dropped."""
    await _seed_legacy(library_db, n_analysis=4, n_failures=2)
    ctrl = _make_controller(library_db, tmp_path)
    await ctrl.setup_database()

    moved = await library_db.get_rows(AA_TABLE_ANALYSIS, order_by="timestamp_created", limit=0)
    assert [r["id"] for r in moved] == [1, 2, 3, 4]  # fresh aa-assigned ids, not the legacy ones
    assert [r["item_id"] for r in moved] == ["t0", "t1", "t2", "t3"]
    assert [r["timestamp_created"] for r in moved] == [1000, 1001, 1002, 1003]
    assert moved[2]["analysis_data"] == '{"loudness_integrated": -2}'
    assert moved[2]["analysis_version"] == 2
    failures = await library_db.get_rows(AA_TABLE_FAILURES, limit=0)
    assert {r["item_id"] for r in failures} == {"f0", "f1"}
    assert failures[0]["next_retry"] is None
    main_tables = await _table_names(library_db, "main")
    assert DB_TABLE_AUDIO_ANALYSIS not in main_tables
    assert DB_TABLE_AUDIO_ANALYSIS_FAILURES not in main_tables
    # dropping the populated legacy tables must be compacted away immediately, not left
    # for the next restart's compaction gate
    freelist = await library_db.get_rows_from_query("PRAGMA main.freelist_count", limit=0)
    assert freelist[0]["freelist_count"] == 0


@pytest.mark.asyncio
async def test_vacuum_failure_after_relocation_is_logged_not_raised(
    library_db: DatabaseConnection,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A vacuum failure after a successful relocation is logged, not raised."""
    await _seed_legacy(library_db, n_analysis=2, n_failures=0)
    ctrl = _make_controller(library_db, tmp_path)
    monkeypatch.setattr(
        library_db, "vacuum", AsyncMock(side_effect=sqlite3.OperationalError("disk I/O error"))
    )
    with caplog.at_level(logging.WARNING):
        await ctrl.setup_database()

    moved = await library_db.get_rows(AA_TABLE_ANALYSIS, limit=0)
    assert {r["item_id"] for r in moved} == {"t0", "t1"}
    assert DB_TABLE_AUDIO_ANALYSIS not in await _table_names(library_db, "main")
    assert any(
        record.levelno == logging.WARNING and "disk I/O error" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_relocation_walks_id_ranges_in_batches(
    library_db: DatabaseConnection, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Relocation completes across multiple id-range batches, not just the first one."""
    monkeypatch.setattr(audio_analysis_mod, "RELOCATE_BATCH_SIZE", 4)
    await _seed_legacy(library_db, n_analysis=7, n_failures=0)  # ids 1..19 span 5 batches
    ctrl = _make_controller(library_db, tmp_path)
    await ctrl.setup_database()
    moved = await library_db.get_rows(AA_TABLE_ANALYSIS, limit=0)
    assert len(moved) == 7


@pytest.mark.asyncio
async def test_relocation_failure_keeps_legacy_table(
    library_db: DatabaseConnection,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A mid-copy failure logs at ERROR and leaves the legacy table for the next start."""
    await _seed_legacy(library_db, n_analysis=2, n_failures=0)
    ctrl = _make_controller(library_db, tmp_path)
    real_execute = library_db.execute

    async def failing_execute(query: str, values: dict[str, Any] | None = None) -> Any:
        if query.lstrip().upper().startswith("INSERT OR IGNORE INTO AA."):
            raise sqlite3.OperationalError("disk I/O error")
        return await real_execute(query, values)

    monkeypatch.setattr(library_db, "execute", failing_execute)
    with caplog.at_level(logging.ERROR):
        await ctrl.setup_database()

    assert DB_TABLE_AUDIO_ANALYSIS in await _table_names(library_db, "main")
    assert "disk I/O error" in caplog.text


@pytest.mark.asyncio
async def test_relocation_failure_skips_vacuum(
    library_db: DatabaseConnection,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A mid-copy failure that keeps the legacy table must not trigger a vacuum either."""
    await _seed_legacy(library_db, n_analysis=2, n_failures=0)
    ctrl = _make_controller(library_db, tmp_path)
    real_execute = library_db.execute

    async def failing_execute(query: str, values: dict[str, Any] | None = None) -> Any:
        if query.lstrip().upper().startswith("INSERT OR IGNORE INTO AA."):
            raise sqlite3.OperationalError("disk I/O error")
        return await real_execute(query, values)

    monkeypatch.setattr(library_db, "execute", failing_execute)
    mock_vacuum = AsyncMock()
    monkeypatch.setattr(library_db, "vacuum", mock_vacuum)
    with caplog.at_level(logging.ERROR):
        await ctrl.setup_database()

    mock_vacuum.assert_not_awaited()


@pytest.mark.asyncio
async def test_relocation_is_resumable_after_partial_copy(
    library_db: DatabaseConnection, tmp_path: pathlib.Path
) -> None:
    """A retry after a crash mid-copy finishes the job instead of re-copying from scratch."""
    ctrl = _make_controller(library_db, tmp_path)
    await ctrl.setup_database()  # attaches and creates empty aa tables
    await _seed_legacy(library_db, n_analysis=3, n_failures=0)
    # seed the partial copy by natural key: t0 already relocated (with a fresh aa id),
    # matching what a real partial run leaves behind
    await library_db.execute(
        f"INSERT INTO {AA_TABLE_ANALYSIS}"
        "(media_type, item_id, provider, aa_provider_domain, analysis_data, "
        " analysis_version, timestamp_created) "
        f"SELECT media_type, item_id, provider, aa_provider_domain, analysis_data, "
        f"analysis_version, timestamp_created FROM main.{DB_TABLE_AUDIO_ANALYSIS} "
        "WHERE item_id = 't0'"
    )
    await library_db.commit()
    await ctrl.setup_database()
    moved = await library_db.get_rows(AA_TABLE_ANALYSIS, limit=0)
    assert {r["item_id"] for r in moved} == {"t0", "t1", "t2"}
    assert DB_TABLE_AUDIO_ANALYSIS not in await _table_names(library_db, "main")


@pytest.mark.asyncio
async def test_relocation_survives_live_writes_after_failed_attempt(
    library_db: DatabaseConnection, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live write claiming aa's next id after a failed attempt must not mask missing rows."""
    await _seed_legacy(library_db, n_analysis=3, n_failures=0)
    ctrl = _make_controller(library_db, tmp_path)
    real_execute = library_db.execute

    async def failing_execute(query: str, values: dict[str, Any] | None = None) -> Any:
        if query.lstrip().upper().startswith("INSERT OR IGNORE INTO AA."):
            raise sqlite3.OperationalError("disk I/O error")
        return await real_execute(query, values)

    monkeypatch.setattr(library_db, "execute", failing_execute)
    await ctrl.setup_database()
    assert DB_TABLE_AUDIO_ANALYSIS in await _table_names(library_db, "main")

    monkeypatch.setattr(library_db, "execute", real_execute)
    # a live analysis write lands in aa in the meantime, consuming aa's own AUTOINCREMENT id
    await library_db.insert_or_replace(
        AA_TABLE_ANALYSIS,
        {
            "media_type": "track",
            "item_id": "live1",
            "provider": "fs--a",
            "aa_provider_domain": "loudness_analysis",
            "analysis_data": "{}",
            "analysis_version": 1,
        },
    )

    await ctrl.setup_database()
    moved = await library_db.get_rows(AA_TABLE_ANALYSIS, limit=0)
    assert {r["item_id"] for r in moved} == {"t0", "t1", "t2", "live1"}
    assert DB_TABLE_AUDIO_ANALYSIS not in await _table_names(library_db, "main")


async def _assert_analysis_unavailable(ctrl: AudioAnalysisController) -> None:
    """Playback degrades without touching the database, while management reports failure."""
    provider = MagicMock(spec=AudioAnalysisProvider)
    provider.available = True
    provider.start_analysis = AsyncMock()
    ctrl.mass.get_providers = MagicMock(return_value=[provider])  # type: ignore[method-assign]
    assert not ctrl._database_ready
    await ctrl.start_analysis(MagicMock(), MagicMock())
    await ctrl._run_background_scan()
    provider.start_analysis.assert_not_awaited()
    assert await ctrl.get_audio_analysis("t0", "fs--a") is None
    assert await ctrl.get_wave_form("t0", "fs--a") is None
    track = Track(item_id="t0", provider="fs--a", name="Test", provider_mappings=set())
    assert await ctrl.get_track_audio_metadata(track) is None
    await ctrl.set_track_loudness("t0", "fs--a", -12.0)
    await ctrl.delete_audio_analysis("t0", "fs--a")
    assert await ctrl.get_extra_data_for_album_tracks(["t0"], "fs--a", "sonic_analysis") == []
    for operation in (
        ctrl.get_audio_analysis_count("sonic_analysis"),
        ctrl.get_audio_analysis_version("t0", "fs--a", "sonic_analysis"),
        ctrl.get_coverage("sonic_analysis"),
        ctrl.get_failures(),
        ctrl.clear_failures(provider="fs--a"),
        ctrl.set_audio_analysis("t0", "fs--a", "sonic_analysis", AudioAnalysisData()),
        ctrl.record_analysis_failure("t0", "fs--a", "sonic_analysis", "failure"),
        ctrl.clear_analysis_failure("t0", "fs--a", "sonic_analysis"),
    ):
        with pytest.raises(ProviderUnavailableError, match="unavailable"):
            await operation
    with pytest.raises(ProviderUnavailableError, match="unavailable"):
        await anext(ctrl.iter_audio_analysis_rows("sonic_analysis"))
    with pytest.raises(ProviderUnavailableError, match="unavailable"):
        await anext(ctrl.iter_merged_audio_analysis_rows("sonic_analysis"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_code",
    [sqlite3.SQLITE_FULL, sqlite3.SQLITE_READONLY, sqlite3.SQLITE_BUSY, sqlite3.SQLITE_IOERR],
)
async def test_operational_errors_preserve_database(
    library_db: DatabaseConnection,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    error_code: int,
) -> None:
    """A readable but unavailable database must never be quarantined or block playback."""
    ctrl = _make_controller(library_db, tmp_path)
    await ctrl.setup_database()
    real_attach = ctrl._attach_and_create
    err = sqlite3.OperationalError("storage unavailable")
    err.sqlite_errorcode = error_code
    monkeypatch.setattr(ctrl, "_attach_and_create", AsyncMock(side_effect=err))
    quarantine = AsyncMock()
    monkeypatch.setattr(ctrl, "_quarantine_database", quarantine)
    await ctrl.setup_database()
    quarantine.assert_not_awaited()
    assert (tmp_path / AA_DB_FILENAME).exists()
    assert not (tmp_path / f"{AA_DB_FILENAME}.corrupt").exists()
    await _assert_analysis_unavailable(ctrl)
    monkeypatch.setattr(ctrl, "_attach_and_create", real_attach)
    await ctrl.setup_database()
    assert ctrl._database_ready


@pytest.mark.asyncio
async def test_newer_schema_preserves_version_and_packed_rows(
    library_db: DatabaseConnection, tmp_path: pathlib.Path
) -> None:
    """A downgrade leaves newer analysis rows and their schema version untouched."""
    with sqlite3.connect(tmp_path / AA_DB_FILENAME) as db:
        db.execute("CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT, type TEXT)")
        db.execute(
            "INSERT INTO settings VALUES ('version', ?, 'str')",
            (str(AA_DB_SCHEMA_VERSION + 1),),
        )
        db.execute("CREATE TABLE audio_analysis(id INTEGER PRIMARY KEY, header BLOB, payload BLOB)")
        db.execute("INSERT INTO audio_analysis VALUES (1, ?, ?)", (b"header", b"payload"))
    ctrl = _make_controller(library_db, tmp_path)
    await ctrl.setup_database()
    await _assert_analysis_unavailable(ctrl)
    version = await library_db.get_row(f"{AA_DB_SCHEMA}.settings", {"key": "version"})
    assert version is not None
    assert int(version["value"]) == AA_DB_SCHEMA_VERSION + 1
    rows = await library_db.get_rows(AA_TABLE_ANALYSIS)
    assert [(row["header"], row["payload"]) for row in rows] == [(b"header", b"payload")]
    assert not (tmp_path / f"{AA_DB_FILENAME}.corrupt").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failed_table", [DB_TABLE_AUDIO_ANALYSIS, DB_TABLE_AUDIO_ANALYSIS_FAILURES]
)
async def test_failed_relocation_disables_analysis_until_restart(
    library_db: DatabaseConnection,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_table: str,
) -> None:
    """Either incomplete migration keeps its source and blocks all analysis until retried."""
    await _seed_legacy(library_db, n_analysis=2, n_failures=2)
    ctrl = _make_controller(library_db, tmp_path)
    real_execute = library_db.execute

    async def failing_execute(query: str, values: dict[str, Any] | None = None) -> Any:
        if query.startswith(f"INSERT OR IGNORE INTO aa.{failed_table} "):
            raise sqlite3.OperationalError("disk full")
        return await real_execute(query, values)

    monkeypatch.setattr(library_db, "execute", failing_execute)
    await ctrl.setup_database()
    await _assert_analysis_unavailable(ctrl)
    assert len(await library_db.get_rows(f"main.{failed_table}")) == 2
    monkeypatch.setattr(library_db, "execute", real_execute)
    restarted = _make_controller(library_db, tmp_path)
    await restarted.setup_database()
    assert restarted._database_ready
    assert len(await library_db.get_rows(AA_TABLE_ANALYSIS)) == 2
    assert len(await restarted.get_failures()) == 2
    assert failed_table not in await _table_names(library_db, "main")
