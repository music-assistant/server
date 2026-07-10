"""Store, gate, and API tests for audio_analysis_failures against a real temp DB."""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from music_assistant.constants import (
    DB_TABLE_AUDIO_ANALYSIS,
    DB_TABLE_AUDIO_ANALYSIS_FAILURES,
    DB_TABLE_PROVIDER_MAPPINGS,
)
from music_assistant.controllers.streams.audio_analysis import AudioAnalysisController
from music_assistant.helpers.database import DatabaseConnection
from music_assistant.models.audio_analysis import AudioAnalysisData
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


@pytest.fixture
async def real_db(tmp_path: pathlib.Path) -> AsyncGenerator[DatabaseConnection]:
    """Create a real on-disk sqlite DB with the minimal tables the gate/store touch."""
    db = DatabaseConnection(str(tmp_path / "test.db"))
    await db.setup()
    await db.execute(
        f"CREATE TABLE {DB_TABLE_PROVIDER_MAPPINGS}("
        "provider_item_id TEXT, provider_instance TEXT, provider_domain TEXT, media_type TEXT)"
    )
    await db.execute(
        f"CREATE TABLE {DB_TABLE_AUDIO_ANALYSIS}("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, media_type TEXT, item_id TEXT, provider TEXT, "
        "aa_provider_domain TEXT, analysis_data json, analysis_version INTEGER, "
        "timestamp_created INTEGER DEFAULT (cast(strftime('%s','now') as int)), "
        "UNIQUE(item_id,provider,aa_provider_domain,media_type))"
    )
    await db.execute(
        f"CREATE TABLE {DB_TABLE_AUDIO_ANALYSIS_FAILURES}("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, media_type TEXT, item_id TEXT, provider TEXT, "
        "aa_provider_domain TEXT, reason TEXT, analysis_version INTEGER NOT NULL DEFAULT 1, "
        "next_retry INTEGER, "
        "timestamp_created INTEGER DEFAULT (cast(strftime('%s','now') as int)), "
        "UNIQUE(item_id,provider,aa_provider_domain,media_type))"
    )
    await db.commit()
    yield db
    await db.close()


def _make_fs_music_provider() -> MagicMock:
    """Return a fake filesystem (non-streaming) MusicProvider keyed by instance_id."""
    prov = MagicMock(spec=MusicProvider)
    prov.is_streaming_provider = False
    prov.domain = "filesystem_local"
    prov.instance_id = "filesystem_local--abc"
    prov.available = True
    return prov


def _make_controller(real_db: DatabaseConnection, music_prov: MagicMock) -> AudioAnalysisController:
    """Return an AudioAnalysisController whose mass.music.database is the real temp DB."""
    streams = MagicMock()
    mass = MagicMock()
    streams.mass = mass
    mass.music.database = real_db
    mass.get_provider = MagicMock(return_value=music_prov)
    mass.get_providers = MagicMock(return_value=[music_prov])
    return AudioAnalysisController(streams)


@pytest.mark.asyncio
async def test_table_roundtrip(real_db: DatabaseConnection) -> None:
    """A row inserted into the failures table reads back with next_retry NULL preserved."""
    await real_db.insert_or_replace(
        DB_TABLE_AUDIO_ANALYSIS_FAILURES,
        {
            "media_type": "track",
            "item_id": "t1",
            "provider": "filesystem_local--abc",
            "aa_provider_domain": "sonic_analysis",
            "reason": "boom",
            "analysis_version": 1,
            "next_retry": None,
        },
    )
    rows = await real_db.get_rows(DB_TABLE_AUDIO_ANALYSIS_FAILURES, limit=0)
    assert len(rows) == 1
    assert rows[0]["reason"] == "boom"
    assert rows[0]["next_retry"] is None


@pytest.mark.asyncio
async def test_record_and_clear_failure_roundtrip(real_db: DatabaseConnection) -> None:
    """record_analysis_failure writes prov_key + NULL retry; clear deletes the row."""
    music_prov = _make_fs_music_provider()
    controller = _make_controller(real_db, music_prov)

    await controller.record_analysis_failure(
        item_id="t1",
        provider_instance_id_or_domain="filesystem_local--abc",
        aa_provider_domain="sonic_analysis",
        reason="no usable audio frames extracted",
    )
    rows = await real_db.get_rows(DB_TABLE_AUDIO_ANALYSIS_FAILURES, limit=0)
    assert len(rows) == 1
    assert rows[0]["provider"] == "filesystem_local--abc"  # instance_id for non-streaming
    assert rows[0]["next_retry"] is None
    assert rows[0]["reason"] == "no usable audio frames extracted"

    await controller.clear_analysis_failure(
        item_id="t1",
        provider_instance_id_or_domain="filesystem_local--abc",
        aa_provider_domain="sonic_analysis",
    )
    rows = await real_db.get_rows(DB_TABLE_AUDIO_ANALYSIS_FAILURES, limit=0)
    assert rows == []


@pytest.mark.asyncio
async def test_record_failure_converts_retry_at_to_epoch(real_db: DatabaseConnection) -> None:
    """A datetime retry_at is stored as an integer epoch in next_retry."""
    music_prov = _make_fs_music_provider()
    controller = _make_controller(real_db, music_prov)
    when = datetime(2030, 6, 1, 12, 0, tzinfo=UTC)

    await controller.record_analysis_failure(
        item_id="t2",
        provider_instance_id_or_domain="filesystem_local--abc",
        aa_provider_domain="sonic_analysis",
        reason="offline",
        retry_at=when,
    )
    rows = await real_db.get_rows(DB_TABLE_AUDIO_ANALYSIS_FAILURES, limit=0)
    assert rows[0]["next_retry"] == int(when.timestamp())


@pytest.mark.asyncio
async def test_record_failure_skips_when_not_music_provider(real_db: DatabaseConnection) -> None:
    """No row is written when the provider lookup is not a MusicProvider."""
    controller = _make_controller(real_db, music_prov=MagicMock())  # not a MusicProvider spec

    await controller.record_analysis_failure(
        item_id="t3",
        provider_instance_id_or_domain="whatever",
        aa_provider_domain="sonic_analysis",
        reason="x",
    )
    rows = await real_db.get_rows(DB_TABLE_AUDIO_ANALYSIS_FAILURES, limit=0)
    assert rows == []


@pytest.mark.asyncio
async def test_set_audio_analysis_clears_existing_failure(real_db: DatabaseConnection) -> None:
    """A successful set_audio_analysis removes any prior failure row for the same key."""
    music_prov = _make_fs_music_provider()
    controller = _make_controller(real_db, music_prov)

    await controller.record_analysis_failure(
        item_id="t1",
        provider_instance_id_or_domain="filesystem_local--abc",
        aa_provider_domain="sonic_analysis",
        reason="boom",
    )
    assert len(await real_db.get_rows(DB_TABLE_AUDIO_ANALYSIS_FAILURES, limit=0)) == 1

    await controller.set_audio_analysis(
        item_id="t1",
        provider_instance_id_or_domain="filesystem_local--abc",
        aa_provider_domain="sonic_analysis",
        analysis=AudioAnalysisData(energy=0.5),
    )
    assert await real_db.get_rows(DB_TABLE_AUDIO_ANALYSIS_FAILURES, limit=0) == []


async def _insert_pm(db: DatabaseConnection, item_id: str) -> None:
    await db.insert(
        DB_TABLE_PROVIDER_MAPPINGS,
        {
            "provider_item_id": item_id,
            "provider_instance": "filesystem_local--abc",
            "provider_domain": "filesystem_local",
            "media_type": "track",
        },
    )


async def _insert_failure(
    db: DatabaseConnection, item_id: str, *, next_retry: int | None, version: int = 1
) -> None:
    await db.insert_or_replace(
        DB_TABLE_AUDIO_ANALYSIS_FAILURES,
        {
            "media_type": "track",
            "item_id": item_id,
            "provider": "filesystem_local--abc",
            "aa_provider_domain": "sonic_analysis",
            "reason": "x",
            "analysis_version": version,
            "next_retry": next_retry,
        },
    )


async def _insert_analysis(
    db: DatabaseConnection,
    item_id: str,
    *,
    version: int | None,
    aa_domain: str = "sonic_analysis",
) -> None:
    await db.insert_or_replace(
        DB_TABLE_AUDIO_ANALYSIS,
        {
            "media_type": "track",
            "item_id": item_id,
            "provider": "filesystem_local--abc",
            "aa_provider_domain": aa_domain,
            "analysis_data": "{}",
            "analysis_version": version,
        },
    )


@pytest.mark.asyncio
async def test_candidate_gate_excludes_blocked_includes_eligible(
    real_db: DatabaseConnection,
) -> None:
    """Blocked (NULL / future, current version) failures are excluded; others are candidates."""
    music_prov = _make_fs_music_provider()
    controller = _make_controller(real_db, music_prov)

    # never-retry, current version -> excluded
    await _insert_pm(real_db, "blocked_null")
    await _insert_failure(real_db, "blocked_null", next_retry=None, version=2)
    # future retry, current version -> excluded
    future = int((datetime.now(UTC) + timedelta(days=1)).timestamp())
    await _insert_pm(real_db, "blocked_future")
    await _insert_failure(real_db, "blocked_future", next_retry=future, version=2)
    # past-due retry -> included
    past = int((datetime.now(UTC) - timedelta(days=1)).timestamp())
    await _insert_pm(real_db, "due")
    await _insert_failure(real_db, "due", next_retry=past, version=2)
    # stale version (1 < current 2) -> included
    await _insert_pm(real_db, "stale")
    await _insert_failure(real_db, "stale", next_retry=None, version=1)
    # no failure at all -> included
    await _insert_pm(real_db, "clean")

    candidates = await controller._find_candidates_missing_analysis({"sonic_analysis": 2}, limit=0)
    found = {c["item_id"] for c in candidates}
    assert found == {"due", "stale", "clean"}


@pytest.mark.asyncio
async def test_candidate_gate_resurfaces_stale_analysis_versions(
    real_db: DatabaseConnection,
) -> None:
    """Analysis rows below the current version (or NULL) surface; current-or-newer do not."""
    music_prov = _make_fs_music_provider()
    controller = _make_controller(real_db, music_prov)

    # row at current version -> excluded
    await _insert_pm(real_db, "current")
    await _insert_analysis(real_db, "current", version=2)
    # row at newer version -> excluded
    await _insert_pm(real_db, "newer")
    await _insert_analysis(real_db, "newer", version=3)
    # row at older version -> included
    await _insert_pm(real_db, "stale")
    await _insert_analysis(real_db, "stale", version=1)
    # pre-versioning row (NULL version) -> included
    await _insert_pm(real_db, "nullver")
    await _insert_analysis(real_db, "nullver", version=None)

    candidates = await controller._find_candidates_missing_analysis({"sonic_analysis": 2}, limit=0)
    found = {c["item_id"] for c in candidates}
    assert found == {"stale", "nullver"}


@pytest.mark.asyncio
async def test_candidate_gate_tracks_versions_per_domain(real_db: DatabaseConnection) -> None:
    """With multiple AA domains, each domain is gated by its own current version."""
    music_prov = _make_fs_music_provider()
    controller = _make_controller(real_db, music_prov)

    # analyzed at v1 for both domains; only sonic_analysis bumped to v2
    await _insert_pm(real_db, "t1")
    await _insert_analysis(real_db, "t1", version=1, aa_domain="sonic_analysis")
    await _insert_analysis(real_db, "t1", version=1, aa_domain="loudness_analysis")

    candidates = await controller._find_candidates_missing_analysis(
        {"sonic_analysis": 2, "loudness_analysis": 1}, limit=0
    )
    assert len(candidates) == 1
    assert candidates[0]["item_id"] == "t1"
    assert candidates[0]["missing_domains"] == ["sonic_analysis"]


@pytest.mark.asyncio
async def test_get_failures_returns_rows_and_filters_by_domain(
    real_db: DatabaseConnection,
) -> None:
    """get_failures returns the stored shape, optionally filtered by aa_domain."""
    music_prov = _make_fs_music_provider()
    controller = _make_controller(real_db, music_prov)
    await _insert_failure(real_db, "t1", next_retry=None)
    await real_db.insert_or_replace(
        DB_TABLE_AUDIO_ANALYSIS_FAILURES,
        {
            "media_type": "track",
            "item_id": "t2",
            "provider": "filesystem_local--abc",
            "aa_provider_domain": "loudness_analysis",
            "reason": "y",
            "analysis_version": 1,
            "next_retry": None,
        },
    )

    all_rows = await controller.get_failures()
    assert {r["item_id"] for r in all_rows} == {"t1", "t2"}
    assert set(all_rows[0]) == {
        "item_id",
        "provider",
        "aa_provider_domain",
        "reason",
        "next_retry",
        "timestamp_created",
    }

    sonic_rows = await controller.get_failures(aa_domain="sonic_analysis")
    assert {r["item_id"] for r in sonic_rows} == {"t1"}


@pytest.mark.asyncio
async def test_clear_failures_requires_a_filter(real_db: DatabaseConnection) -> None:
    """clear_failures with no filter deletes nothing and returns 0."""
    music_prov = _make_fs_music_provider()
    controller = _make_controller(real_db, music_prov)
    await _insert_failure(real_db, "t1", next_retry=None)

    deleted = await controller.clear_failures()
    assert deleted == 0
    assert len(await real_db.get_rows(DB_TABLE_AUDIO_ANALYSIS_FAILURES, limit=0)) == 1


@pytest.mark.asyncio
async def test_clear_failures_by_domain(real_db: DatabaseConnection) -> None:
    """clear_failures(aa_domain=...) deletes all rows for that domain and returns the count."""
    music_prov = _make_fs_music_provider()
    controller = _make_controller(real_db, music_prov)
    await _insert_failure(real_db, "t1", next_retry=None)
    await _insert_failure(real_db, "t2", next_retry=None)

    deleted = await controller.clear_failures(aa_domain="sonic_analysis")
    assert deleted == 2
    assert await real_db.get_rows(DB_TABLE_AUDIO_ANALYSIS_FAILURES, limit=0) == []
