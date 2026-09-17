"""Tests for the controller-owned audio_analysis.db (attach, schema version, relocation)."""

from __future__ import annotations

import logging
import os
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
    AA_TABLE_ANALYSIS_V1,
    AA_TABLE_FAILURES,
    AA_TABLE_SETTINGS,
    PROVIDER_LOUDNESS_DOMAIN,
    AudioAnalysisController,
)
from music_assistant.controllers.streams.audio_analysis_codec import decode, encode
from music_assistant.helpers.database import DatabaseConnection
from music_assistant.helpers.json import json_dumps
from music_assistant.models.audio_analysis import AudioAnalysisData
from music_assistant.models.audio_analysis_provider import AudioAnalysisProvider
from music_assistant.models.music_provider import MusicProvider

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
@pytest.mark.parametrize("failure", ["commit", "detach"])
async def test_quarantine_failure_preserves_attached_database(
    library_db: DatabaseConnection,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """Failure to commit or detach must abort quarantine before any file is renamed."""
    ctrl = _make_controller(library_db, tmp_path)
    await ctrl.setup_database()
    corruption = sqlite3.DatabaseError("database disk image is malformed")
    corruption.sqlite_errorcode = sqlite3.SQLITE_CORRUPT
    attach = AsyncMock(side_effect=corruption)
    monkeypatch.setattr(ctrl, "_attach_and_create", attach)
    replace = MagicMock()
    monkeypatch.setattr(os, "replace", replace)

    with monkeypatch.context() as failing:
        if failure == "commit":
            failing.setattr(
                library_db,
                "commit",
                AsyncMock(side_effect=sqlite3.OperationalError("disk I/O error")),
            )
        else:
            real_execute = library_db.execute

            async def failing_execute(query: str, values: dict[str, Any] | None = None) -> Any:
                if query == f"DETACH DATABASE {AA_DB_SCHEMA}":
                    raise sqlite3.OperationalError("database aa is locked")
                return await real_execute(query, values)

            failing.setattr(library_db, "execute", failing_execute)
        await ctrl.setup_database()

    replace.assert_not_called()
    attach.assert_awaited_once()
    assert (tmp_path / AA_DB_FILENAME).exists()
    assert not (tmp_path / f"{AA_DB_FILENAME}.corrupt").exists()
    attached = await library_db.get_rows_from_query("PRAGMA database_list", limit=0)
    assert AA_DB_SCHEMA in {row["name"] for row in attached}
    await _assert_analysis_unavailable(ctrl)


@pytest.mark.asyncio
async def test_quarantine_detaches_before_replacing_database(
    library_db: DatabaseConnection, tmp_path: pathlib.Path
) -> None:
    """An attached file is detached and replaced rather than reused under its old schema name."""
    ctrl = _make_controller(library_db, tmp_path)
    await ctrl.setup_database()
    settings = f"{AA_DB_SCHEMA}.{DB_TABLE_SETTINGS}"
    await library_db.insert_or_replace(
        settings, {"key": "sentinel", "value": "original", "type": "str"}
    )

    await ctrl._quarantine_database(str(tmp_path / AA_DB_FILENAME))

    attached = await library_db.get_rows_from_query("PRAGMA database_list", limit=0)
    assert AA_DB_SCHEMA not in {row["name"] for row in attached}
    assert not (tmp_path / AA_DB_FILENAME).exists()
    assert (tmp_path / f"{AA_DB_FILENAME}.corrupt").exists()
    await ctrl.setup_database()
    assert ctrl.database_ready
    assert await library_db.get_row(settings, {"key": "sentinel"}) is None


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
    """Deletion removes successes and failures for only the given item/provider key."""
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
                "analysis_version": 1,
                "header": "{}",
                "payload": b"",
            },
        )
        await library_db.insert(
            AA_TABLE_FAILURES,
            {
                "media_type": "track",
                "item_id": "t1",
                "provider": provider,
                "aa_provider_domain": domain,
                "reason": "never retry",
                "next_retry": None,
            },
        )
    await ctrl.delete_audio_analysis("t1", "fs--a", MediaType.TRACK)
    for table in (AA_TABLE_ANALYSIS, AA_TABLE_FAILURES):
        rows = await library_db.get_rows(table, limit=0)
        assert [(r["provider"], r["aa_provider_domain"]) for r in rows] == [
            ("fs--b", "loudness_analysis")
        ]


@pytest.mark.asyncio
async def test_newer_schema_version_disables_analysis(
    library_db: DatabaseConnection, tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A file written by a newer build is refused instead of being used or rewritten."""
    ctrl = _make_controller(library_db, tmp_path)
    await ctrl.setup_database()
    await library_db.insert_or_replace(
        f"{AA_DB_SCHEMA}.{DB_TABLE_SETTINGS}", {"key": "version", "value": "99", "type": "str"}
    )
    with caplog.at_level(logging.ERROR):
        await ctrl.setup_database()
    await _assert_analysis_unavailable(ctrl)
    assert any(
        record.levelno == logging.ERROR
        and "newer than this build supports" in record.getMessage()
        and "upgrade Music Assistant" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_round_trip_through_controller(
    library_db: DatabaseConnection, tmp_path: pathlib.Path
) -> None:
    """A record written through the controller reads back with its scalars and arrays."""
    ctrl = _make_controller(library_db, tmp_path)
    await ctrl.setup_database()
    music_prov = MagicMock(spec=MusicProvider)
    music_prov.is_streaming_provider = False
    music_prov.instance_id = "fs--a"
    ctrl.mass.get_provider = MagicMock(return_value=music_prov)  # type: ignore[method-assign]
    ctrl.mass.get_providers = MagicMock(return_value=[])  # type: ignore[method-assign]
    analysis = AudioAnalysisData(
        bpm=123.5,
        key="F#",
        loudness_integrated=-9.25,
        beats=[0.5, 1.0, 1.5],
        rms_energy=[i / 1800 for i in range(1800)],
    )

    await ctrl.set_audio_analysis("t1", "fs--a", PROVIDER_LOUDNESS_DOMAIN, analysis)
    stored = await ctrl.get_audio_analysis("t1", "fs--a")

    assert stored is not None
    assert stored.bpm == 123.5
    assert stored.key == "F#"
    assert stored.loudness_integrated == -9.25
    assert stored.beats == [0.5, 1.0, 1.5]
    assert stored.rms_energy is not None
    assert stored.rms_energy == pytest.approx(analysis.rms_energy, abs=1e-3)


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
async def test_legacy_library_rows_are_converted(
    library_db: DatabaseConnection, tmp_path: pathlib.Path
) -> None:
    """Legacy JSON rows land packed with fresh ids, timestamps preserved, source dropped."""
    await _seed_legacy(library_db, n_analysis=4, n_failures=2)
    ctrl = _make_controller(library_db, tmp_path)
    await ctrl.setup_database()

    moved = await library_db.get_rows(AA_TABLE_ANALYSIS, order_by="timestamp_created", limit=0)
    assert [r["id"] for r in moved] == [1, 2, 3, 4]  # fresh aa-assigned ids, not the legacy ones
    assert [r["item_id"] for r in moved] == ["t0", "t1", "t2", "t3"]
    assert [r["timestamp_created"] for r in moved] == [1000, 1001, 1002, 1003]
    assert decode(moved[2]["header"], moved[2]["payload"]).loudness_integrated == -2
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
async def test_conversion_walks_the_source_in_batches(
    library_db: DatabaseConnection, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Conversion completes across multiple cursor batches, not just the first one."""
    monkeypatch.setattr(audio_analysis_mod, "MIGRATE_BATCH_SIZE", 2)
    monkeypatch.setattr(audio_analysis_mod, "RELOCATE_BATCH_SIZE", 4)
    await _seed_legacy(library_db, n_analysis=7, n_failures=9)  # ids 1..19 span 5 batches
    ctrl = _make_controller(library_db, tmp_path)
    await ctrl.setup_database()
    moved = await library_db.get_rows(AA_TABLE_ANALYSIS, limit=0)
    assert len(moved) == 7
    assert len(await library_db.get_rows(AA_TABLE_FAILURES, limit=0)) == 9


@pytest.mark.asyncio
async def test_relocation_failure_keeps_legacy_table(
    library_db: DatabaseConnection,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A mid-conversion failure logs at ERROR and leaves the legacy table for the next start."""
    await _seed_legacy(library_db, n_analysis=2, n_failures=0)
    ctrl = _make_controller(library_db, tmp_path)
    real_execute = library_db.execute

    async def failing_execute(query: str, values: dict[str, Any] | None = None) -> Any:
        if query.lstrip().upper().startswith("INSERT INTO AA."):
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
    """A mid-conversion failure that keeps the legacy table must not trigger a vacuum."""
    await _seed_legacy(library_db, n_analysis=2, n_failures=0)
    ctrl = _make_controller(library_db, tmp_path)
    real_execute = library_db.execute

    async def failing_execute(query: str, values: dict[str, Any] | None = None) -> Any:
        if query.lstrip().upper().startswith("INSERT INTO AA."):
            raise sqlite3.OperationalError("disk I/O error")
        return await real_execute(query, values)

    monkeypatch.setattr(library_db, "execute", failing_execute)
    mock_vacuum = AsyncMock()
    monkeypatch.setattr(library_db, "vacuum", mock_vacuum)
    with caplog.at_level(logging.ERROR):
        await ctrl.setup_database()

    mock_vacuum.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("table", [DB_TABLE_AUDIO_ANALYSIS, DB_TABLE_AUDIO_ANALYSIS_FAILURES])
async def test_completed_copy_is_compacted_after_restart(
    library_db: DatabaseConnection,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    table: str,
) -> None:
    """Dropping a populated source still compacts the library when every row was already copied."""
    ctrl = _make_controller(library_db, tmp_path)
    await ctrl.setup_database()
    await _seed_legacy(
        library_db,
        n_analysis=2 if table == DB_TABLE_AUDIO_ANALYSIS else 0,
        n_failures=2 if table == DB_TABLE_AUDIO_ANALYSIS_FAILURES else 0,
    )
    real_execute = library_db.execute

    async def failing_execute(query: str, values: dict[str, Any] | None = None) -> Any:
        if query == f"DROP TABLE main.{table}":
            raise sqlite3.OperationalError("database table is locked")
        return await real_execute(query, values)

    monkeypatch.setattr(library_db, "execute", failing_execute)
    vacuum = AsyncMock()
    monkeypatch.setattr(library_db, "vacuum", vacuum)
    await ctrl.setup_database()
    assert len(await library_db.get_rows(f"aa.{table}")) == 2
    vacuum.assert_not_awaited()

    monkeypatch.setattr(library_db, "execute", real_execute)
    await ctrl.setup_database()
    assert table not in await _table_names(library_db, "main")
    vacuum.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("source_timestamp", [900, 1000, 1100])
async def test_failure_relocation_keeps_newest_same_key_record(
    library_db: DatabaseConnection,
    tmp_path: pathlib.Path,
    source_timestamp: int,
) -> None:
    """Newer legacy writes win after rollback, while equal or newer destination rows survive."""
    table = DB_TABLE_AUDIO_ANALYSIS_FAILURES
    ctrl = _make_controller(library_db, tmp_path)
    await ctrl.setup_database()
    await _seed_legacy(library_db, n_analysis=0, n_failures=1)
    await library_db.execute(
        f"UPDATE main.{table} SET timestamp_created = :timestamp",
        {"timestamp": source_timestamp},
    )
    source = dict((await library_db.get_rows(f"main.{table}"))[0])
    destination = {**source, "id": 42, "timestamp_created": 1000, "analysis_version": 9}
    destination["reason"] = "destination failure"
    destination["next_retry"] = 2000
    await library_db.insert(f"aa.{table}", destination)

    await ctrl.setup_database()

    expected = source if source_timestamp > 1000 else destination
    result = dict((await library_db.get_rows(f"aa.{table}"))[0])
    assert result == {**expected, "id": 42}
    assert table not in await _table_names(library_db, "main")


@pytest.mark.asyncio
async def test_conversion_survives_live_writes_after_failed_attempt(
    library_db: DatabaseConnection, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live write claiming aa's next id after a failed attempt must not mask missing rows."""
    await _seed_legacy(library_db, n_analysis=3, n_failures=0)
    ctrl = _make_controller(library_db, tmp_path)
    real_execute = library_db.execute

    async def failing_execute(query: str, values: dict[str, Any] | None = None) -> Any:
        if query.lstrip().upper().startswith("INSERT INTO AA."):
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
            "analysis_version": 1,
            "header": "{}",
            "payload": b"",
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
    assert await ctrl.get_extra_data_for_album_tracks(["t0"], "fs--a", "sonic_analysis") == []
    for operation in (
        ctrl.delete_audio_analysis("t0", "fs--a"),
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
async def test_null_schema_version_disables_analysis_without_blocking_playback(
    library_db: DatabaseConnection, tmp_path: pathlib.Path
) -> None:
    """Malformed version metadata stays untouched and uses the normal unavailable state."""
    ctrl = _make_controller(library_db, tmp_path)
    await ctrl.setup_database()
    await library_db.insert_or_replace(
        f"{AA_DB_SCHEMA}.{DB_TABLE_SETTINGS}", {"key": "version", "value": None, "type": "str"}
    )

    await ctrl.setup_database()

    await _assert_analysis_unavailable(ctrl)
    version = await library_db.get_row(f"{AA_DB_SCHEMA}.{DB_TABLE_SETTINGS}", {"key": "version"})
    assert version is not None
    assert version["value"] is None
    assert not (tmp_path / f"{AA_DB_FILENAME}.corrupt").exists()


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
        if query.startswith(f"INSERT INTO aa.{failed_table} "):
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


V1_TABLE_NAME = f"{DB_TABLE_AUDIO_ANALYSIS}_v1"
V1_ANALYSIS_DDL = (
    f"CREATE TABLE {AA_TABLE_ANALYSIS}("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, media_type TEXT NOT NULL, item_id TEXT NOT NULL, "
    "provider TEXT NOT NULL, aa_provider_domain TEXT NOT NULL, analysis_data json NOT NULL, "
    "analysis_version INTEGER DEFAULT 1, "
    "timestamp_created INTEGER DEFAULT (cast(strftime('%s','now') as int)), "
    "UNIQUE(item_id,provider,aa_provider_domain,media_type))"
)


async def _seed_v1_table(
    db: DatabaseConnection, ctrl: AudioAnalysisController, rows: list[tuple[str, str]]
) -> None:
    """
    Attach the analysis db and put a v1-shaped table holding the given JSON rows in it.

    :param db: The library database connection the analysis db is attached onto.
    :param ctrl: Controller used to attach the analysis database.
    :param rows: (item_id, analysis_data JSON) pairs, inserted with timestamp 1000 + index.
    """
    await ctrl.setup_database()
    await db.execute(f"DROP TABLE {AA_TABLE_ANALYSIS}")
    await db.execute(V1_ANALYSIS_DDL)
    for index, (item_id, data) in enumerate(rows):
        await db.execute(
            f"INSERT INTO {AA_TABLE_ANALYSIS}"
            "(media_type, item_id, provider, aa_provider_domain, analysis_data, "
            " analysis_version, timestamp_created) VALUES "
            "(:media_type, :item, 'fs--a', 'sonic_analysis', :data, 3, :ts)",
            {"media_type": "track", "item": item_id, "data": data, "ts": 1000 + index},
        )
    await db.insert_or_replace(AA_TABLE_SETTINGS, {"key": "version", "value": "1", "type": "str"})
    await db.commit()


async def _stored_version(db: DatabaseConnection) -> int:
    """Return the schema version recorded in the attached analysis database."""
    row = await db.get_row(AA_TABLE_SETTINGS, {"key": "version"})
    assert row is not None
    return int(row["value"])


@pytest.mark.asyncio
async def test_v1_table_is_converted_to_packed_rows(
    library_db: DatabaseConnection, tmp_path: pathlib.Path
) -> None:
    """Both JSON row shapes in a v1 file convert to packed rows and the source is dropped."""
    ctrl = _make_controller(library_db, tmp_path)
    typed = json_dumps(AudioAnalysisData(bpm=120.0, rms_energy=[0.5] * 1800).to_dict())
    legacy = json_dumps({"extra_data": {"clap_embedding": [0.25] * 1024}})
    await _seed_v1_table(library_db, ctrl, [("t0", typed), ("t1", legacy)])

    await ctrl.setup_database()

    assert await _stored_version(library_db) == AA_DB_SCHEMA_VERSION
    columns = await library_db.get_rows_from_query(
        f"PRAGMA {AA_DB_SCHEMA}.table_info({DB_TABLE_AUDIO_ANALYSIS})", limit=0
    )
    names = {c["name"] for c in columns}
    assert {"header", "payload"} <= names
    assert "analysis_data" not in names
    assert f"{DB_TABLE_AUDIO_ANALYSIS}_v1" not in await _table_names(library_db, AA_DB_SCHEMA)

    rows = await library_db.get_rows(AA_TABLE_ANALYSIS, order_by="timestamp_created", limit=0)
    assert [r["item_id"] for r in rows] == ["t0", "t1"]
    assert [r["timestamp_created"] for r in rows] == [1000, 1001]
    assert [r["analysis_version"] for r in rows] == [3, 3]
    assert [(r["provider"], r["aa_provider_domain"]) for r in rows] == [
        ("fs--a", "sonic_analysis"),
        ("fs--a", "sonic_analysis"),
    ]
    first = decode(rows[0]["header"], rows[0]["payload"])
    assert first.bpm == 120.0
    assert first.rms_energy == pytest.approx([0.5] * 1800)
    second = decode(rows[1]["header"], rows[1]["payload"])
    assert second.clap_embedding == pytest.approx([0.25] * 1024)


@pytest.mark.asyncio
async def test_migration_resumes_after_partial_run(
    library_db: DatabaseConnection, tmp_path: pathlib.Path
) -> None:
    """A packed row already written by an interrupted run is kept, the rest still convert."""
    ctrl = _make_controller(library_db, tmp_path)
    rows = [(f"t{i}", json_dumps({"bpm": 100.0 + i})) for i in range(3)]
    await _seed_v1_table(library_db, ctrl, rows)
    # simulate a crash after the first batch: the v1 table is already renamed, the v2 table
    # exists and holds the first row
    await library_db.execute(f"ALTER TABLE {AA_TABLE_ANALYSIS} RENAME TO {V1_TABLE_NAME}")
    await library_db.commit()
    await ctrl._prepare_analysis_table()
    header, payload = encode(AudioAnalysisData(bpm=100.0))
    await library_db.insert(
        AA_TABLE_ANALYSIS,
        {
            "media_type": "track",
            "item_id": "t0",
            "provider": "fs--a",
            "aa_provider_domain": "sonic_analysis",
            "analysis_version": 3,
            "timestamp_created": 1000,
            "header": header,
            "payload": payload,
        },
    )
    await library_db.commit()

    await ctrl.setup_database()

    packed = await library_db.get_rows(AA_TABLE_ANALYSIS, order_by="timestamp_created", limit=0)
    assert [r["item_id"] for r in packed] == ["t0", "t1", "t2"]
    assert V1_TABLE_NAME not in await _table_names(library_db, AA_DB_SCHEMA)


@pytest.mark.asyncio
async def test_unreadable_v1_rows_are_dropped_with_a_summary_warning(
    library_db: DatabaseConnection, tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A row that cannot be decoded is dropped with the source and reported once."""
    ctrl = _make_controller(library_db, tmp_path)
    await _seed_v1_table(
        library_db,
        ctrl,
        [("t0", '{"bpm": 100.0}'), ("bad", "not json"), ("t1", '{"bpm": 110.0}')],
    )

    with caplog.at_level(logging.WARNING):
        await ctrl.setup_database()

    rows = await library_db.get_rows(AA_TABLE_ANALYSIS, limit=0)
    assert {r["item_id"] for r in rows} == {"t0", "t1"}
    assert V1_TABLE_NAME not in await _table_names(library_db, AA_DB_SCHEMA)
    assert any(
        record.levelno == logging.WARNING
        and "unreadable" in record.getMessage()
        and "1" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_migration_failure_keeps_v1_table(
    library_db: DatabaseConnection,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failing insert preserves the source while keeping older readers out."""
    ctrl = _make_controller(library_db, tmp_path)
    await _seed_v1_table(library_db, ctrl, [("t0", '{"bpm": 100.0}')])
    real_execute = library_db.execute

    async def failing_execute(query: str, values: dict[str, Any] | None = None) -> Any:
        if query.lstrip().upper().startswith("INSERT OR IGNORE INTO AA.AUDIO_ANALYSIS "):
            raise sqlite3.OperationalError("disk I/O error")
        return await real_execute(query, values)

    monkeypatch.setattr(library_db, "execute", failing_execute)
    with caplog.at_level(logging.ERROR):
        await ctrl.setup_database()

    assert V1_TABLE_NAME in await _table_names(library_db, AA_DB_SCHEMA)
    assert await _stored_version(library_db) == AA_DB_SCHEMA_VERSION
    assert "disk I/O error" in caplog.text


@pytest.mark.asyncio
async def test_unencodable_rows_are_counted_and_dropped(
    library_db: DatabaseConnection,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One row that cannot be packed is dropped like an unreadable one; the rest convert."""
    ctrl = _make_controller(library_db, tmp_path)
    await _seed_v1_table(
        library_db,
        ctrl,
        [("t0", '{"bpm": 100.0}'), ("bad", '{"bpm": 105.0}'), ("t1", '{"bpm": 110.0}')],
    )

    def failing_encode(analysis: AudioAnalysisData) -> tuple[str, bytes]:
        if analysis.bpm == 105.0:
            raise TypeError("boom")
        return encode(analysis)

    monkeypatch.setattr(audio_analysis_mod, "encode", failing_encode)
    with caplog.at_level(logging.WARNING):
        await ctrl.setup_database()

    rows = await library_db.get_rows(AA_TABLE_ANALYSIS, limit=0)
    assert {r["item_id"] for r in rows} == {"t0", "t1"}
    assert V1_TABLE_NAME not in await _table_names(library_db, AA_DB_SCHEMA)
    assert await _stored_version(library_db) == AA_DB_SCHEMA_VERSION
    assert ctrl._database_ready
    assert any(
        record.levelno == logging.WARNING and "unpackable" in record.getMessage()
        for record in caplog.records
    )
    assert any(
        record.levelno == logging.WARNING
        and "1 unreadable audio analysis rows" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_incomplete_conversion_keeps_source_table(
    library_db: DatabaseConnection,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A row that silently fails to land keeps the source table for the next start."""
    ctrl = _make_controller(library_db, tmp_path)
    await _seed_v1_table(library_db, ctrl, [("t0", '{"bpm": 100.0}'), ("t1", '{"bpm": 110.0}')])
    real_execute = library_db.execute

    async def dropping_execute(query: str, values: dict[str, Any] | None = None) -> Any:
        if (
            query.lstrip().upper().startswith("INSERT OR IGNORE INTO AA.AUDIO_ANALYSIS ")
            and values is not None
            and values["item_id"] == "t1"
        ):
            return MagicMock()  # the write is silently dropped, no error raised
        return await real_execute(query, values)

    monkeypatch.setattr(library_db, "execute", dropping_execute)
    with caplog.at_level(logging.ERROR):
        await ctrl.setup_database()

    assert V1_TABLE_NAME in await _table_names(library_db, AA_DB_SCHEMA)
    assert await _stored_version(library_db) == AA_DB_SCHEMA_VERSION
    packed_rows = await library_db.get_rows(AA_TABLE_ANALYSIS)
    assert len(packed_rows) == 1
    with monkeypatch.context() as downgrade:
        downgrade.setattr(audio_analysis_mod, "AA_DB_SCHEMA_VERSION", 1)
        older = _make_controller(library_db, tmp_path)
        await older.setup_database()
        await _assert_analysis_unavailable(older)
    assert await _stored_version(library_db) == AA_DB_SCHEMA_VERSION
    assert await library_db.get_rows(AA_TABLE_ANALYSIS) == packed_rows
    assert any(
        record.levelno == logging.ERROR and "incomplete" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_failed_conversion_disables_analysis_until_restart(
    library_db: DatabaseConnection,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Incomplete packed conversion blocks all analysis but leaves playback available."""
    ctrl = _make_controller(library_db, tmp_path)
    await _seed_v1_table(library_db, ctrl, [("t0", '{"bpm": 100.0}')])
    real_execute = library_db.execute

    async def failing_execute(query: str, values: dict[str, Any] | None = None) -> Any:
        if query.lstrip().upper().startswith("INSERT OR IGNORE INTO AA.AUDIO_ANALYSIS "):
            raise sqlite3.OperationalError("disk I/O error")
        return await real_execute(query, values)

    monkeypatch.setattr(library_db, "execute", failing_execute)
    await ctrl.setup_database()
    await _assert_analysis_unavailable(ctrl)
    assert V1_TABLE_NAME in await _table_names(library_db, AA_DB_SCHEMA)
    assert await _stored_version(library_db) == AA_DB_SCHEMA_VERSION
    find_candidates = AsyncMock(return_value=[])
    monkeypatch.setattr(ctrl, "_find_candidates_missing_analysis", find_candidates)
    await ctrl._run_background_scan()
    find_candidates.assert_not_awaited()
    monkeypatch.setattr(library_db, "execute", real_execute)
    restarted = _make_controller(library_db, tmp_path)
    await restarted.setup_database()
    assert restarted._database_ready
    assert await restarted.get_audio_analysis_count("sonic_analysis") == 1
    assert V1_TABLE_NAME not in await _table_names(library_db, AA_DB_SCHEMA)


@pytest.mark.asyncio
async def test_database_is_ready_after_a_successful_start(
    library_db: DatabaseConnection, tmp_path: pathlib.Path
) -> None:
    """A fresh install and a completed conversion both leave the scan gate open."""
    ctrl = _make_controller(library_db, tmp_path)
    await ctrl.setup_database()
    assert ctrl._database_ready

    await _seed_v1_table(library_db, ctrl, [("t0", '{"bpm": 100.0}')])
    await ctrl.setup_database()
    assert ctrl._database_ready


@pytest.mark.asyncio
async def test_schema_preparation_failure_does_not_quarantine_the_file(
    library_db: DatabaseConnection, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A locked v1 rename disables analysis without moving the file aside."""
    ctrl = _make_controller(library_db, tmp_path)
    await _seed_v1_table(library_db, ctrl, [("t0", '{"bpm": 100.0}')])
    real_execute = library_db.execute

    async def failing_execute(query: str, values: dict[str, Any] | None = None) -> Any:
        if query.lstrip().upper().startswith("ALTER TABLE"):
            raise sqlite3.OperationalError("database table is locked")
        return await real_execute(query, values)

    monkeypatch.setattr(library_db, "execute", failing_execute)
    await ctrl.setup_database()
    await _assert_analysis_unavailable(ctrl)

    assert not (tmp_path / f"{AA_DB_FILENAME}.corrupt").exists()
    # the rename never happened, so the JSON-shaped table is still the live one
    columns = await library_db.get_rows_from_query(
        f"PRAGMA {AA_DB_SCHEMA}.table_info({DB_TABLE_AUDIO_ANALYSIS})", limit=0
    )
    assert "analysis_data" in {c["name"] for c in columns}
    with sqlite3.connect(tmp_path / AA_DB_FILENAME) as db:
        version = db.execute("SELECT value FROM settings WHERE key = 'version'").fetchone()
        assert int(version[0]) == AA_DB_SCHEMA_VERSION
    monkeypatch.setattr(library_db, "execute", real_execute)
    restarted = _make_controller(library_db, tmp_path)
    await restarted.setup_database()
    assert restarted._database_ready
    assert await restarted.get_audio_analysis_count("sonic_analysis") == 1


@pytest.mark.asyncio
async def test_schema_marker_failure_keeps_json_table(
    library_db: DatabaseConnection, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No packed schema may be created before its compatibility marker is durable."""
    ctrl = _make_controller(library_db, tmp_path)
    await _seed_v1_table(library_db, ctrl, [("t0", '{"bpm": 100.0}')])
    monkeypatch.setattr(
        library_db,
        "insert_or_replace",
        AsyncMock(side_effect=sqlite3.OperationalError("disk full")),
    )
    await ctrl.setup_database()
    await _assert_analysis_unavailable(ctrl)
    assert await _stored_version(library_db) == 1
    rows = await library_db.get_rows(AA_TABLE_ANALYSIS)
    assert rows[0]["analysis_data"] == '{"bpm": 100.0}'
    assert V1_TABLE_NAME not in await _table_names(library_db, AA_DB_SCHEMA)


@pytest.mark.asyncio
async def test_setup_database_on_file_without_settings_table(
    library_db: DatabaseConnection, tmp_path: pathlib.Path
) -> None:
    """A pre-existing empty analysis file is treated as version 0 and set up from scratch."""
    empty = DatabaseConnection(str(tmp_path / AA_DB_FILENAME))
    await empty.setup()
    await empty.close()
    ctrl = _make_controller(library_db, tmp_path)

    await ctrl.setup_database()

    tables = await _table_names(library_db, AA_DB_SCHEMA)
    assert {DB_TABLE_AUDIO_ANALYSIS, DB_TABLE_AUDIO_ANALYSIS_FAILURES, DB_TABLE_SETTINGS} <= tables
    assert await _stored_version(library_db) == AA_DB_SCHEMA_VERSION
    assert not (tmp_path / f"{AA_DB_FILENAME}.corrupt").exists()


@pytest.mark.asyncio
async def test_analysis_db_is_compacted_after_conversion(
    library_db: DatabaseConnection, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The analysis file is compacted after a conversion, and left alone without one."""
    ctrl = _make_controller(library_db, tmp_path)
    await _seed_v1_table(library_db, ctrl, [("t0", '{"bpm": 100.0}')])
    mock_vacuum = AsyncMock()
    monkeypatch.setattr(library_db, "vacuum", mock_vacuum)

    await ctrl.setup_database()
    mock_vacuum.assert_any_await(schema=AA_DB_SCHEMA)

    mock_vacuum.reset_mock()
    await ctrl.setup_database()
    mock_vacuum.assert_not_awaited()


@pytest.mark.asyncio
async def test_progress_is_logged_every_2000_rows(
    library_db: DatabaseConnection,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Long conversions report progress at a fixed row interval."""
    monkeypatch.setattr(audio_analysis_mod, "MIGRATE_PROGRESS_ROWS", 2)
    monkeypatch.setattr(audio_analysis_mod, "MIGRATE_BATCH_SIZE", 1)
    ctrl = _make_controller(library_db, tmp_path)
    await _seed_v1_table(library_db, ctrl, [(f"t{i}", '{"bpm": 100.0}') for i in range(5)])

    with caplog.at_level(logging.INFO):
        await ctrl.setup_database()

    progress = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.INFO
        and record.getMessage().startswith("Converted ")
        and "/5 audio analysis rows" in record.getMessage()
    ]
    assert progress == [
        f"Converted 2/5 audio analysis rows from {AA_TABLE_ANALYSIS_V1}",
        f"Converted 4/5 audio analysis rows from {AA_TABLE_ANALYSIS_V1}",
    ]
