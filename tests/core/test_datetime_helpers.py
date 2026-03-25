"""Tests for datetime helper functions."""

import datetime as dt

from music_assistant.helpers.datetime import (
    from_iso_string,
    from_utc_timestamp,
    future_timestamp,
    iso_from_utc_timestamp,
    local_clock_time_to_utc,
    now,
    now_timestamp,
    utc,
    utc_timestamp,
)


def test_utc_returns_utc_datetime() -> None:
    """utc() returns a timezone-aware datetime in UTC."""
    result = utc()
    assert isinstance(result, dt.datetime)
    assert result.tzinfo is not None
    assert result.utcoffset() == dt.timedelta(0)


def test_utc_timestamp_returns_float() -> None:
    """utc_timestamp() returns a positive float."""
    ts = utc_timestamp()
    assert isinstance(ts, float)
    assert ts > 0


def test_utc_timestamp_close_to_now() -> None:
    """utc_timestamp() is within a second of the real UTC time."""
    expected = dt.datetime.now(dt.UTC).timestamp()
    ts = utc_timestamp()
    assert abs(ts - expected) < 1.0


def test_now_returns_aware_datetime() -> None:
    """now() returns a timezone-aware local datetime."""
    result = now()
    assert isinstance(result, dt.datetime)
    assert result.tzinfo is not None


def test_now_timestamp_returns_float() -> None:
    """now_timestamp() returns a positive float."""
    ts = now_timestamp()
    assert isinstance(ts, float)
    assert ts > 0


def test_future_timestamp_is_greater() -> None:
    """future_timestamp(seconds=10) is 10 seconds ahead of now."""
    base = now_timestamp()
    future = future_timestamp(seconds=10)
    assert future > base
    assert abs(future - base - 10) < 0.5


def test_from_utc_timestamp_round_trip() -> None:
    """from_utc_timestamp round-trips a UTC timestamp back to datetime."""
    ts = utc_timestamp()
    result = from_utc_timestamp(ts)
    assert isinstance(result, dt.datetime)
    assert result.tzinfo is not None
    # Round-trip should be within 1ms
    assert abs(result.timestamp() - ts) < 0.001


def test_iso_from_utc_timestamp_is_string() -> None:
    """iso_from_utc_timestamp() returns a valid ISO 8601 string."""
    ts = utc_timestamp()
    iso = iso_from_utc_timestamp(ts)
    assert isinstance(iso, str)
    # Must be parseable
    parsed = dt.datetime.fromisoformat(iso)
    assert abs(parsed.timestamp() - ts) < 0.001


def test_from_iso_string_round_trip() -> None:
    """from_iso_string can parse a string produced by isoformat."""
    original = utc()
    iso = original.isoformat()
    parsed = from_iso_string(iso)
    assert parsed == original


def test_local_clock_time_to_utc_returns_valid_range() -> None:
    """local_clock_time_to_utc returns hour and minute within valid range."""
    utc_h, utc_m = local_clock_time_to_utc(12, 30)
    assert 0 <= utc_h <= 23
    assert 0 <= utc_m <= 59


def test_local_clock_time_to_utc_default_minute() -> None:
    """local_clock_time_to_utc defaults minute to 0."""
    utc_h, utc_m = local_clock_time_to_utc(0)
    assert 0 <= utc_h <= 23
    assert 0 <= utc_m <= 59
