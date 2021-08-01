"""Helpers for date and time."""

import datetime

LOCAL_TIMEZONE = datetime.datetime.now(datetime.timezone.utc).astimezone().tzinfo


def utc() -> datetime.datetime:
    """Get current UTC datetime."""
    return datetime.datetime.now(datetime.timezone.utc)


def utc_timestamp() -> float:
    """Return UTC timestamp in seconds as float."""
    return utc().timestamp()


def now() -> datetime.datetime:
    """Get current datetime in local timezone."""
    return datetime.datetime.now(LOCAL_TIMEZONE)


def now_timestamp() -> float:
    """Return current datetime as timestamp in local timezone."""
    return now().timestamp()


def future_timestamp(**kwargs) -> float:
    """Return current timestamp + timedelta."""
    return (now() + datetime.timedelta(**kwargs)).timestamp()


def from_utc_timestamp(timestamp: float) -> datetime.datetime:
    """Return datetime from UTC timestamp."""
    return datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc)


def iso_from_utc_timestamp(timestamp: float) -> str:
    """Return ISO 8601 datetime string from UTC timestamp."""
    return from_utc_timestamp(timestamp).isoformat()
