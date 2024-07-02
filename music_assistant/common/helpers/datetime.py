"""Helpers for date and time."""

from __future__ import annotations

import datetime

LOCAL_TIMEZONE = datetime.datetime.now(datetime.UTC).astimezone().tzinfo


def utc() -> datetime.datetime:
    """Get current UTC datetime."""
    return datetime.datetime.now(datetime.UTC)


def utc_timestamp() -> float:
    """Return UTC timestamp in seconds as float."""
    return utc().timestamp()


def now() -> datetime.datetime:
    """Get current datetime in local timezone."""
    return datetime.datetime.now(LOCAL_TIMEZONE)


def now_timestamp() -> float:
    """Return current datetime as timestamp in local timezone."""
    return now().timestamp()


def future_timestamp(**kwargs: float) -> float:
    """Return current timestamp + timedelta."""
    return (now() + datetime.timedelta(**kwargs)).timestamp()


def from_utc_timestamp(timestamp: float) -> datetime.datetime:
    """Return datetime from UTC timestamp."""
    return datetime.datetime.fromtimestamp(timestamp, datetime.UTC)


def iso_from_utc_timestamp(timestamp: float) -> str:
    """Return ISO 8601 datetime string from UTC timestamp."""
    return from_utc_timestamp(timestamp).isoformat()


def from_iso_string(iso_datetime: str) -> datetime.datetime:
    """Return datetime from ISO datetime string."""
    return datetime.datetime.fromisoformat(iso_datetime)
