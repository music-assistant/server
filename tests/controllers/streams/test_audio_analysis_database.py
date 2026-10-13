"""Tests for the controller-owned audio_analysis.db (connection, schema version, quarantine)."""

from __future__ import annotations

import logging
import os
import pathlib
import sqlite3
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ContentType, MediaType
from music_assistant_models.errors import ProviderUnavailableError
from music_assistant_models.media_items import AudioFormat, ProviderMapping, Track

import music_assistant.controllers.streams.audio_analysis as audio_analysis_mod
from music_assistant.constants import (
    DB_TABLE_AUDIO_ANALYSIS,
    DB_TABLE_AUDIO_ANALYSIS_FAILURES,
    DB_TABLE_PROVIDER_MAPPINGS,
    DB_TABLE_SETTINGS,
)
from music_assistant.controllers.streams.audio_analysis import (
    PROVIDER_LOUDNESS_DOMAIN,
    SMART_FADES_ANALYSIS_DOMAIN,
    SONIC_ANALYSIS_DOMAIN,
    AudioAnalysisController,
)
from music_assistant.controllers.streams.constants import (
    AA_DB_FILENAME,
    AA_DB_SCHEMA_VERSION,
    AA_TABLE_ANALYSIS,
    AA_TABLE_FAILURES,
    AA_TABLE_SETTINGS,
)
from music_assistant.helpers.database import DatabaseConnection
from music_assistant.models.audio_analysis import AudioAnalysisData
from music_assistant.models.audio_analysis_provider import AudioAnalysisProvider
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


@pytest.fixture
async def library_db(tmp_path: pathlib.Path) -> AsyncGenerator[DatabaseConnection]:
    """Return a real on-disk library.db connection with nothing but a provider_mappings table."""
    db = DatabaseConnection(str(tmp_path / "library.db"))
    await db.setup()
    await db.execute(
        f"CREATE TABLE {DB_TABLE_PROVIDER_MAPPINGS}("
        "provider_item_id TEXT, provider_instance TEXT, provider_domain TEXT, media_type TEXT)"
    )
    await db.commit()
    yield db
    await db.close()


@pytest.fixture
async def ctrl(
    library_db: DatabaseConnection, tmp_path: pathlib.Path
) -> AsyncGenerator[AudioAnalysisController]:
    """Return a controller whose analysis database lives in the test directory."""
    controller = _make_controller(library_db, tmp_path)
    yield controller
    await controller.close_database()


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


async def _table_names(db: DatabaseConnection) -> set[str]:
    rows = await db.get_rows_from_query(
        "SELECT name FROM main.sqlite_master WHERE type = 'table'", limit=0
    )
    return {r["name"] for r in rows}


async def _assert_analysis_unavailable(ctrl: AudioAnalysisController) -> None:
    """Playback degrades without touching the database, while management reports failure."""
    provider = MagicMock(spec=AudioAnalysisProvider)
    provider.available = True
    provider.start_analysis = AsyncMock()
    ctrl.mass.get_providers = MagicMock(return_value=[provider])  # type: ignore[method-assign]
    assert not ctrl.database_ready
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


def _corruption() -> sqlite3.DatabaseError:
    err = sqlite3.DatabaseError("database disk image is malformed")
    err.sqlite_errorcode = sqlite3.SQLITE_CORRUPT
    return err


@pytest.mark.asyncio
async def test_setup_database_opens_file_and_creates_tables(
    ctrl: AudioAnalysisController, library_db: DatabaseConnection, tmp_path: pathlib.Path
) -> None:
    """Setup creates the db file with its tables and a version row, apart from library.db."""
    await ctrl.setup_database()

    assert ctrl.database_ready
    assert (tmp_path / AA_DB_FILENAME).exists()
    tables = await _table_names(ctrl.database)
    assert {DB_TABLE_AUDIO_ANALYSIS, DB_TABLE_AUDIO_ANALYSIS_FAILURES, DB_TABLE_SETTINGS} <= tables
    assert DB_TABLE_AUDIO_ANALYSIS not in await _table_names(library_db)
    attached = await library_db.get_rows_from_query("PRAGMA database_list", limit=0)
    assert [row["name"] for row in attached] == ["main"]
    version = await ctrl.database.get_row(AA_TABLE_SETTINGS, {"key": "version"})
    assert version is not None
    assert int(version["value"]) == AA_DB_SCHEMA_VERSION


@pytest.mark.asyncio
async def test_setup_database_is_idempotent(ctrl: AudioAnalysisController) -> None:
    """Calling setup_database twice reuses the connection and keeps one version row."""
    await ctrl.setup_database()
    connection = ctrl.database
    await ctrl.setup_database()
    assert ctrl.database is connection
    rows = await ctrl.database.get_rows(AA_TABLE_SETTINGS, {"key": "version"})
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_database_uses_wal(ctrl: AudioAnalysisController) -> None:
    """The analysis file uses WAL journaling like the other database files."""
    await ctrl.setup_database()
    journal = await ctrl.database.get_rows_from_query("PRAGMA journal_mode", limit=0)
    assert journal[0]["journal_mode"] == "wal"


@pytest.mark.asyncio
async def test_close_database_marks_analysis_unavailable(ctrl: AudioAnalysisController) -> None:
    """Closing the connection leaves the controller in the unavailable state."""
    await ctrl.setup_database()
    await ctrl.close_database()
    assert ctrl._database is None
    await _assert_analysis_unavailable(ctrl)


@pytest.mark.asyncio
async def test_unreadable_database_is_quarantined_and_recreated(
    ctrl: AudioAnalysisController, tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An unreadable analysis file is moved aside and replaced with a fresh one."""
    garbage = b"this is definitely not a sqlite database" * 8
    (tmp_path / AA_DB_FILENAME).write_bytes(garbage)
    with caplog.at_level(logging.ERROR):
        await ctrl.setup_database()

    assert ctrl.database_ready
    assert (tmp_path / f"{AA_DB_FILENAME}.corrupt").read_bytes() == garbage
    tables = await _table_names(ctrl.database)
    assert {DB_TABLE_AUDIO_ANALYSIS, DB_TABLE_AUDIO_ANALYSIS_FAILURES, DB_TABLE_SETTINGS} <= tables
    assert any(
        record.levelno == logging.ERROR and "unusable" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_quarantine_failure_leaves_analysis_unavailable(
    ctrl: AudioAnalysisController, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file that cannot be moved aside keeps analysis unavailable and the file in place."""
    await ctrl.setup_database()
    await ctrl.close_database()
    monkeypatch.setattr(ctrl, "_open_database", AsyncMock(side_effect=_corruption()))
    monkeypatch.setattr(os, "replace", MagicMock(side_effect=OSError("read-only file system")))

    await ctrl.setup_database()

    assert (tmp_path / AA_DB_FILENAME).exists()
    assert not (tmp_path / f"{AA_DB_FILENAME}.corrupt").exists()
    await _assert_analysis_unavailable(ctrl)


@pytest.mark.asyncio
async def test_quarantine_closes_connection_before_replacing_database(
    ctrl: AudioAnalysisController, tmp_path: pathlib.Path
) -> None:
    """The open file is closed and moved aside, and setup then starts on a fresh one."""
    await ctrl.setup_database()
    await ctrl.database.insert_or_replace(
        AA_TABLE_SETTINGS, {"key": "sentinel", "value": "original", "type": "str"}
    )
    await ctrl.database.commit()

    await ctrl._quarantine_database(str(tmp_path / AA_DB_FILENAME))

    assert ctrl._database is None
    assert not (tmp_path / AA_DB_FILENAME).exists()
    assert (tmp_path / f"{AA_DB_FILENAME}.corrupt").exists()
    await ctrl.setup_database()
    assert ctrl.database_ready
    assert await ctrl.database.get_row(AA_TABLE_SETTINGS, {"key": "sentinel"}) is None


@pytest.mark.asyncio
async def test_setup_database_after_quarantine_is_idempotent(
    ctrl: AudioAnalysisController, tmp_path: pathlib.Path
) -> None:
    """A second setup_database call on the replacement file changes nothing."""
    garbage = b"this is definitely not a sqlite database" * 8
    (tmp_path / AA_DB_FILENAME).write_bytes(garbage)
    await ctrl.setup_database()
    await ctrl.setup_database()

    assert (tmp_path / f"{AA_DB_FILENAME}.corrupt").read_bytes() == garbage
    rows = await ctrl.database.get_rows(AA_TABLE_SETTINGS, {"key": "version"})
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_delete_audio_analysis_removes_only_that_provider_key(
    ctrl: AudioAnalysisController,
) -> None:
    """Deletion removes successes and failures for only the given item/provider key."""
    await ctrl.setup_database()
    for provider, domain in (
        ("fs--a", "loudness_analysis"),
        ("fs--a", "smart_fades"),
        ("fs--b", "loudness_analysis"),
    ):
        await ctrl.database.insert(
            AA_TABLE_ANALYSIS,
            {
                "media_type": "track",
                "item_id": "t1",
                "provider": provider,
                "aa_provider_domain": domain,
                "header": "{}",
                "payload": b"",
                "analysis_version": 1,
            },
        )
        await ctrl.database.insert(
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
        rows = await ctrl.database.get_rows(table, limit=0)
        assert [(r["provider"], r["aa_provider_domain"]) for r in rows] == [
            ("fs--b", "loudness_analysis")
        ]


@pytest.mark.asyncio
async def test_candidate_queries_read_library_keys_through_temp_table(
    ctrl: AudioAnalysisController, library_db: DatabaseConnection
) -> None:
    """Candidates come from the library's filesystem tracks minus up-to-date analysis rows."""
    await ctrl.setup_database()
    fs_prov = MagicMock(domain="filesystem_local", available=True)
    ctrl.mass.providers = [fs_prov]  # type: ignore[misc]
    for item_id, domain, media_type in (
        ("t1", "filesystem_local", "track"),
        ("t2", "filesystem_local", "track"),
        ("t3", "filesystem_local", "track"),
        ("s1", "spotify", "track"),
        ("a1", "filesystem_local", "album"),
    ):
        await library_db.insert(
            DB_TABLE_PROVIDER_MAPPINGS,
            {
                "provider_item_id": item_id,
                "provider_instance": f"{domain}--x",
                "provider_domain": domain,
                "media_type": media_type,
            },
        )
    await library_db.commit()
    # t1 is analyzed at the current version, t2 at an older one, t3 not at all
    for item_id, version in (("t1", 2), ("t2", 1)):
        await ctrl.database.insert(
            AA_TABLE_ANALYSIS,
            {
                "media_type": "track",
                "item_id": item_id,
                "provider": "filesystem_local--x",
                "aa_provider_domain": "sonic_analysis",
                "header": "{}",
                "payload": b"",
                "analysis_version": version,
            },
        )
    await ctrl.database.commit()

    candidates = await ctrl._find_candidates_missing_analysis({"sonic_analysis": 2}, limit=0)
    count = await ctrl._count_candidates_missing_analysis("sonic_analysis", 2)

    assert sorted(c["item_id"] for c in candidates) == ["t2", "t3"]
    assert count == 2
    leftover = await ctrl.database.get_rows_from_query(
        "SELECT * FROM temp.candidate_tracks", limit=0
    )
    assert leftover == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_code",
    [sqlite3.SQLITE_FULL, sqlite3.SQLITE_READONLY, sqlite3.SQLITE_BUSY, sqlite3.SQLITE_IOERR],
)
async def test_operational_errors_preserve_database(
    ctrl: AudioAnalysisController,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    error_code: int,
) -> None:
    """A readable but unavailable database must never be quarantined or block playback."""
    await ctrl.setup_database()
    real_open = ctrl._open_database
    err = sqlite3.OperationalError("storage unavailable")
    err.sqlite_errorcode = error_code
    monkeypatch.setattr(ctrl, "_open_database", AsyncMock(side_effect=err))
    quarantine = AsyncMock()
    monkeypatch.setattr(ctrl, "_quarantine_database", quarantine)
    await ctrl.setup_database()
    quarantine.assert_not_awaited()
    assert (tmp_path / AA_DB_FILENAME).exists()
    assert not (tmp_path / f"{AA_DB_FILENAME}.corrupt").exists()
    await _assert_analysis_unavailable(ctrl)
    monkeypatch.setattr(ctrl, "_open_database", real_open)
    await ctrl.setup_database()
    assert ctrl.database_ready


@pytest.mark.asyncio
async def test_null_schema_version_disables_analysis_without_blocking_playback(
    ctrl: AudioAnalysisController, tmp_path: pathlib.Path
) -> None:
    """Malformed version metadata stays untouched and uses the normal unavailable state."""
    await ctrl.setup_database()
    await ctrl.database.insert_or_replace(
        AA_TABLE_SETTINGS, {"key": "version", "value": None, "type": "str"}
    )
    await ctrl.database.commit()
    await ctrl.close_database()

    await ctrl.setup_database()

    await _assert_analysis_unavailable(ctrl)
    with sqlite3.connect(tmp_path / AA_DB_FILENAME) as db:
        value = db.execute("SELECT value FROM settings WHERE key = 'version'").fetchone()[0]
    assert value is None
    assert not (tmp_path / f"{AA_DB_FILENAME}.corrupt").exists()


@pytest.mark.asyncio
async def test_newer_schema_preserves_version_and_rows(
    ctrl: AudioAnalysisController, tmp_path: pathlib.Path
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

    await ctrl.setup_database()

    await _assert_analysis_unavailable(ctrl)
    with sqlite3.connect(tmp_path / AA_DB_FILENAME) as db:
        version = db.execute("SELECT value FROM settings WHERE key = 'version'").fetchone()[0]
        rows = db.execute("SELECT header, payload FROM audio_analysis").fetchall()
    assert int(version) == AA_DB_SCHEMA_VERSION + 1
    assert rows == [(b"header", b"payload")]
    assert not (tmp_path / f"{AA_DB_FILENAME}.corrupt").exists()


@pytest.mark.asyncio
async def test_newer_schema_version_disables_analysis(
    ctrl: AudioAnalysisController, caplog: pytest.LogCaptureFixture
) -> None:
    """A file written by a newer build is refused instead of being used or rewritten."""
    await ctrl.setup_database()
    await ctrl.database.insert_or_replace(
        AA_TABLE_SETTINGS, {"key": "version", "value": "99", "type": "str"}
    )
    await ctrl.database.commit()
    await ctrl.close_database()
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
async def test_round_trip_through_controller(ctrl: AudioAnalysisController) -> None:
    """A record written through the controller reads back with its scalars and arrays."""
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


@pytest.mark.asyncio
async def test_setup_database_on_file_without_settings_table(
    ctrl: AudioAnalysisController, tmp_path: pathlib.Path
) -> None:
    """A pre-existing empty analysis file is treated as version 0 and set up from scratch."""
    empty = DatabaseConnection(str(tmp_path / AA_DB_FILENAME))
    await empty.setup()
    await empty.close()

    await ctrl.setup_database()

    tables = await _table_names(ctrl.database)
    assert {DB_TABLE_AUDIO_ANALYSIS, DB_TABLE_AUDIO_ANALYSIS_FAILURES, DB_TABLE_SETTINGS} <= tables
    version = await ctrl.database.get_row(AA_TABLE_SETTINGS, {"key": "version"})
    assert version is not None
    assert int(version["value"]) == AA_DB_SCHEMA_VERSION
    assert not (tmp_path / f"{AA_DB_FILENAME}.corrupt").exists()


def _music_provider(instance_id: str, domain: str, streaming: bool) -> MagicMock:
    provider = MagicMock(spec=MusicProvider)
    provider.instance_id = instance_id
    provider.domain = domain
    provider.is_streaming_provider = streaming
    return provider


def _aa_provider(domain: str) -> MagicMock:
    provider = MagicMock(spec=AudioAnalysisProvider)
    provider.domain = domain
    provider.available = True
    return provider


def _track(*mappings: tuple[str, str, ContentType]) -> Track:
    """Build a track with one mapping per (item_id, provider instance, content type)."""
    return Track(
        item_id=mappings[0][0],
        provider=mappings[0][1],
        name="Test",
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain=instance.split("--")[0],
                provider_instance=instance,
                audio_format=AudioFormat(content_type=content_type),
            )
            for item_id, instance, content_type in mappings
        },
    )


async def _setup_metadata_fixture(ctrl: AudioAnalysisController) -> list[Track]:
    """Store analysis for a mix of tracks and return tracks covering each lookup outcome."""
    await ctrl.setup_database()
    providers = {
        "fs--a": _music_provider("fs--a", "filesystem_local", streaming=False),
        "spotify--x": _music_provider("spotify--x", "spotify", streaming=True),
        "spotify--y": _music_provider("spotify--y", "spotify", streaming=True),
    }
    ctrl.mass.get_provider = MagicMock(side_effect=providers.get)  # type: ignore[method-assign]
    ctrl.mass.get_providers = MagicMock(  # type: ignore[method-assign]
        return_value=[
            _aa_provider(SMART_FADES_ANALYSIS_DOMAIN),
            _aa_provider(SONIC_ANALYSIS_DOMAIN),
        ]
    )
    stored = [
        ("t1", "fs--a", SONIC_ANALYSIS_DOMAIN, AudioAnalysisData(bpm=100.0, key="D")),
        ("t1", "fs--a", SMART_FADES_ANALYSIS_DOMAIN, AudioAnalysisData(bpm=128.0)),
        ("t2", "fs--a", SONIC_ANALYSIS_DOMAIN, AudioAnalysisData(key="F#", mode="minor")),
        ("t3", "fs--a", SONIC_ANALYSIS_DOMAIN, AudioAnalysisData(loudness_integrated=-8.0)),
        ("t4", "fs--a", PROVIDER_LOUDNESS_DOMAIN, AudioAnalysisData(bpm=90.0)),
        ("lofi", "fs--a", SMART_FADES_ANALYSIS_DOMAIN, AudioAnalysisData(bpm=70.0)),
        ("hifi", "fs--a", SMART_FADES_ANALYSIS_DOMAIN, AudioAnalysisData(bpm=140.0)),
        # stored under the streaming domain, so any instance of it finds the row
        ("s1", "spotify--x", SMART_FADES_ANALYSIS_DOMAIN, AudioAnalysisData(bpm=110.0, key="A")),
    ]
    for item_id, instance, aa_domain, analysis in stored:
        await ctrl.set_audio_analysis(item_id, instance, aa_domain, analysis)
    mp3, flac = ContentType.MP3, ContentType.FLAC
    return [
        _track(("t1", "fs--a", mp3)),
        _track(("t2", "fs--a", mp3)),
        _track(("t3", "fs--a", mp3)),
        _track(("t4", "fs--a", mp3)),
        _track(("missing", "fs--a", mp3)),
        _track(("t1", "unloaded--z", mp3)),
        _track(("lofi", "fs--a", mp3), ("hifi", "fs--a", flac)),
        _track(("lofi", "fs--a", mp3), ("missing", "fs--a", flac)),
        _track(("s1", "spotify--y", mp3)),
        _track(("t1", "fs--a", mp3)),
    ]


@pytest.mark.asyncio
async def test_tracks_audio_metadata_matches_single_track_lookup(
    ctrl: AudioAnalysisController,
) -> None:
    """The batched lookup returns, per track, what the single-track lookup returns."""
    tracks = await _setup_metadata_fixture(ctrl)

    batched_result = await ctrl.get_tracks_audio_metadata(tracks)
    single_result = [await ctrl.get_track_audio_metadata(track) for track in tracks]

    assert batched_result == single_result
    summary = [(m.bpm, m.musical_key) if m else None for m in batched_result]
    assert summary == [
        (128.0, "D"),
        (None, "F# minor"),
        None,
        None,
        None,
        None,
        (140.0, None),
        (70.0, None),
        (110.0, "A"),
        (128.0, "D"),
    ]


@pytest.mark.asyncio
async def test_tracks_audio_metadata_ignores_unavailable_aa_providers(
    ctrl: AudioAnalysisController,
) -> None:
    """Rows of an AA provider that is not available are left out, as for a single track."""
    tracks = await _setup_metadata_fixture(ctrl)
    ctrl.mass.get_providers = MagicMock(  # type: ignore[method-assign]
        return_value=[_aa_provider(SONIC_ANALYSIS_DOMAIN)]
    )

    batched_result = await ctrl.get_tracks_audio_metadata(tracks)

    assert batched_result == [await ctrl.get_track_audio_metadata(track) for track in tracks]
    assert batched_result[0] is not None
    assert batched_result[0].bpm == 100.0


@pytest.mark.asyncio
async def test_tracks_audio_metadata_chunks_item_ids(
    ctrl: AudioAnalysisController, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item ids are queried in bounded chunks, one query per chunk and provider key."""
    tracks = await _setup_metadata_fixture(ctrl)
    expected = await ctrl.get_tracks_audio_metadata(tracks)
    monkeypatch.setattr(audio_analysis_mod, "_AUDIO_METADATA_BATCH_SIZE", 2)
    query_spy = AsyncMock(wraps=ctrl.database.get_rows_from_query)
    monkeypatch.setattr(ctrl.database, "get_rows_from_query", query_spy)

    assert await ctrl.get_tracks_audio_metadata(tracks) == expected
    # fs--a holds 7 distinct item ids (4 chunks of 2), the spotify domain 1
    assert query_spy.await_count == 5


@pytest.mark.asyncio
async def test_tracks_audio_metadata_skips_corrupt_header(ctrl: AudioAnalysisController) -> None:
    """A malformed stored header is skipped in SQL and does not hide the other provider's row."""
    tracks = await _setup_metadata_fixture(ctrl)
    await ctrl.database.execute(
        f"UPDATE {AA_TABLE_ANALYSIS} SET header = :header "
        "WHERE item_id = :item_id AND aa_provider_domain = :domain",
        {"header": "{not json", "item_id": "t1", "domain": SMART_FADES_ANALYSIS_DOMAIN},
    )
    await ctrl.database.commit()

    result = await ctrl.get_tracks_audio_metadata(tracks)

    assert result[0] is not None
    assert result[0].bpm == 100.0
    assert result[0].musical_key == "D"


@pytest.mark.asyncio
async def test_tracks_audio_metadata_without_database(ctrl: AudioAnalysisController) -> None:
    """Every track gets None while the analysis database is not ready."""
    track = Track(item_id="t0", provider="fs--a", name="Test", provider_mappings=set())
    assert await ctrl.get_tracks_audio_metadata([track, track]) == [None, None]
