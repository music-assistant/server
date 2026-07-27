"""Tests for the datetime helpers."""

from __future__ import annotations

import pytest

from music_assistant.helpers.datetime import host_timezone_name


def test_tz_env_var_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid IANA name in the TZ environment variable is used first."""
    monkeypatch.setenv("TZ", "Europe/Amsterdam")
    assert host_timezone_name() == "Europe/Amsterdam"


def test_invalid_tz_env_var_falls_back_to_localtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unrecognized TZ value is ignored in favor of /etc/localtime."""
    monkeypatch.setenv("TZ", "not/a-real-zone")
    monkeypatch.setattr("os.readlink", lambda path: "/var/db/timezone/zoneinfo/Europe/Berlin")
    assert host_timezone_name() == "Europe/Berlin"


def test_falls_back_to_localtime_symlink(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a TZ override, the /etc/localtime symlink target is used."""
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.setattr("os.readlink", lambda path: "/usr/share/zoneinfo/Asia/Tokyo")
    assert host_timezone_name() == "Asia/Tokyo"


def test_falls_back_to_utc_when_localtime_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing/non-symlink /etc/localtime never raises and falls back to UTC."""
    monkeypatch.delenv("TZ", raising=False)

    def _raise(path: str) -> str:
        raise OSError("not a symlink")

    monkeypatch.setattr("os.readlink", _raise)
    assert host_timezone_name() == "UTC"


def test_falls_back_to_utc_when_localtime_target_is_not_a_known_zone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unresolvable /etc/localtime target (e.g. no 'zoneinfo/' segment) falls back to UTC."""
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.setattr("os.readlink", lambda path: "/some/other/path")
    assert host_timezone_name() == "UTC"
