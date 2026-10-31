"""Regression coverage for Audible external history import primitives."""

from datetime import UTC, datetime, timedelta

from music_assistant.providers.audible.remote_history import (
    MAX_ASINS_PER_REQUEST,
    asin_chunks,
    eligible_external_plays,
    parse_lastposition,
    parse_lastpositions,
    parse_timestamp,
)


def test_uses_endpoint_batch_limits_and_deduplicates() -> None:
    """Uses endpoint batch limits and deduplicates."""
    ids = [f"B{i:09d}" for i in range(57)] + ["B000000000"]
    chunks = list(asin_chunks(ids))
    assert sum(map(len, chunks)) == 57
    assert all(len(chunk) <= MAX_ASINS_PER_REQUEST for chunk in chunks)
    assert all(len(",".join(chunk)) <= 480 for chunk in chunks)
    assert chunks[0][0] == "B000000000"


def test_batches_by_url_length() -> None:
    """Batches by url length."""
    chunks = list(asin_chunks(["X" * 100] * 2 + [str(i) + "Y" * 99 for i in range(8)]))
    assert sum(map(len, chunks)) == 9
    assert all(len(",".join(chunk)) <= 480 for chunk in chunks)


def test_parses_real_timestamps_and_positions() -> None:
    """Parses real timestamps and positions."""
    plays = parse_lastpositions(
        {
            "asin_last_position_heard_annots": [
                {
                    "asin": "A1",
                    "last_position_heard": {
                        "position_ms": 130500,
                        "last_updated": "2026-10-29T10:00:00Z",
                    },
                },
                {
                    "asin": "A2",
                    "last_position_heard": {
                        "position_ms": "250000",
                        "reported_time": "2026-10-30T11:00:00+00:00",
                    },
                },
            ]
        }
    )
    assert [item.position_seconds for item in plays] == [130, 250]
    assert [item.asin for item in plays] == ["A1", "A2"]
    assert plays[0].updated_at.tzinfo is UTC


def test_ignores_unreliable_rows() -> None:
    """Ignores unreliable rows."""
    assert (
        parse_lastpositions(
            {
                "asin_last_position_heard_annots": [
                    {"asin": "missing-date", "last_position_heard": {"position_ms": 1000}},
                    {
                        "asin": "missing-position",
                        "last_position_heard": {
                            "position_ms": 0,
                            "last_updated": "2026-10-31T00:00:00Z",
                        },
                    },
                    {
                        "asin": "invalid-ms",
                        "last_position_heard": {
                            "position_ms": "bad",
                            "last_updated": "2026-10-31T00:00:00Z",
                        },
                    },
                    None,
                ]
            }
        )
        == []
    )
    assert parse_lastpositions(None) == []
    assert parse_lastpositions({"asin_last_position_heard_annots": "invalid"}) == []


def test_timestamp_handles_naive_aware_millis_and_invalid() -> None:
    """Timestamp handles naive aware millis and invalid."""
    expected = datetime(2026, 10, 30, tzinfo=UTC)
    assert parse_timestamp("2026-10-30T00:00:00") == expected
    assert parse_timestamp("2026-10-30T00:00:00Z") == expected
    assert parse_timestamp(expected.timestamp()) == expected
    assert parse_timestamp(expected.timestamp() * 1000) == expected
    assert parse_timestamp("bad") is None


def test_import_drops_old_future_duplicate_and_sorts_by_real_played_at() -> None:
    """Import drops old future duplicate and sorts by real played at."""
    now = datetime(2026, 10, 31, tzinfo=UTC)
    entries = parse_lastpositions(
        {
            "asin_last_position_heard_annots": [
                {
                    "asin": "later",
                    "last_position_heard": {
                        "position_ms": 250000,
                        "last_updated": "2026-10-30T10:00:00Z",
                    },
                },
                {
                    "asin": "earlier",
                    "last_position_heard": {
                        "position_ms": 120000,
                        "last_updated": "2026-10-28T10:00:00Z",
                    },
                },
                {
                    "asin": "stale",
                    "last_position_heard": {
                        "position_ms": 120000,
                        "last_updated": "2026-01-01T10:00:00Z",
                    },
                },
                {
                    "asin": "future",
                    "last_position_heard": {
                        "position_ms": 120000,
                        "last_updated": "2026-11-03T10:00:00Z",
                    },
                },
                {
                    "asin": "seen",
                    "last_position_heard": {
                        "position_ms": 120000,
                        "last_updated": "2026-10-27T10:00:00Z",
                    },
                },
            ]
        }
    )
    pending = eligible_external_plays(entries, {"seen": "2026-10-27T10:00:00+00:00"}, now=now)
    assert [item.asin for item in pending] == ["earlier", "later"]
    assert pending[-1].updated_at == now - timedelta(hours=14)


def test_duplicate_api_rows_keep_only_latest_date() -> None:
    """Duplicate api rows keep only latest date."""
    now = datetime(2026, 10, 31, tzinfo=UTC)
    entries = parse_lastpositions(
        {
            "asin_last_position_heard_annots": [
                {
                    "asin": "B1",
                    "last_position_heard": {
                        "position_ms": 120000,
                        "last_updated": "2026-10-27T10:00:00Z",
                    },
                },
                {
                    "asin": "B1",
                    "last_position_heard": {
                        "position_ms": 180000,
                        "last_updated": "2026-10-29T10:00:00Z",
                    },
                },
            ]
        }
    )
    selected = eligible_external_plays(entries, {}, now=now)
    assert len(selected) == 1
    assert selected[0].position_seconds == 180
    assert selected[0].updated_at == datetime(2026, 10, 29, 10, tzinfo=UTC)


def test_numeric_millisecond_timestamp_string() -> None:
    """Numeric millisecond timestamp string."""
    expected = datetime(2026, 10, 30, tzinfo=UTC)
    assert parse_timestamp(str(int(expected.timestamp() * 1000))) == expected


def test_shared_annotation_parser_preserves_undated_resume_position() -> None:
    """Resume lookup retains exact milliseconds even without an importable date."""
    row = {"asin": "A", "last_position_heard": {"position_ms": 1234}}
    assert parse_lastposition(row) == (1234, None)
    assert parse_lastpositions({"asin_last_position_heard_annots": [row]}) == []


def test_shared_annotation_parser_accepts_numeric_dates_and_rejects_invalid_positions() -> None:
    """Single and batch paths share timestamp and malformed-position handling."""
    timestamp = datetime(2026, 10, 1, tzinfo=UTC)
    row = {
        "asin": "A",
        "last_position_heard": {"position_ms": 1234, "timestamp": timestamp.timestamp() * 1000},
    }
    assert parse_lastposition(row) == (1234, timestamp)
    assert (
        parse_lastpositions({"asin_last_position_heard_annots": [row]})[0].updated_at == timestamp
    )
    assert parse_lastposition({"last_position_heard": {"position_ms": "bad"}}) is None
