"""Tests for the datetime helpers."""

from __future__ import annotations

from pathlib import Path

import pytest
from music_assistant_models.errors import InvalidDataError

from music_assistant.helpers.datetime import get_timezone, host_timezone_name


def test_tz_env_var_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid IANA name in the TZ environment variable is used first."""
    monkeypatch.setenv("TZ", "Europe/Amsterdam")
    assert host_timezone_name() == "Europe/Amsterdam"


def test_invalid_tz_env_var_falls_back_to_localtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unrecognized TZ value is ignored in favor of /etc/localtime."""
    monkeypatch.setenv("TZ", "not/a-real-zone")
    monkeypatch.setattr(
        Path, "readlink", lambda _self: Path("/var/db/timezone/zoneinfo/Europe/Berlin")
    )
    assert host_timezone_name() == "Europe/Berlin"


def test_falls_back_to_localtime_symlink(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a TZ override, the /etc/localtime symlink target is used."""
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.setattr(Path, "readlink", lambda _self: Path("/usr/share/zoneinfo/Asia/Tokyo"))
    assert host_timezone_name() == "Asia/Tokyo"


def test_falls_back_to_utc_when_localtime_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing/non-symlink /etc/localtime never raises and falls back to UTC."""
    monkeypatch.delenv("TZ", raising=False)

    def _raise(_self: Path) -> Path:
        raise OSError("not a symlink")

    monkeypatch.setattr(Path, "readlink", _raise)
    assert host_timezone_name() == "UTC"


def test_falls_back_to_utc_when_localtime_target_is_not_a_known_zone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unresolvable /etc/localtime target (e.g. no 'zoneinfo/' segment) falls back to UTC."""
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.setattr(Path, "readlink", lambda _self: Path("/some/other/path"))
    assert host_timezone_name() == "UTC"


def test_get_timezone_resolves_an_iana_timezone() -> None:
    """Resolve an explicit IANA zone rather than the host's local offset."""
    timezone = get_timezone("America/Havana")

    assert timezone.key == "America/Havana"


@pytest.mark.parametrize("timezone", ["Not/AZone", "", "/etc/localtime"])
def test_get_timezone_rejects_invalid_names(timezone: str) -> None:
    """Invalid IANA names retain the API-facing validation error."""
    with pytest.raises(InvalidDataError, match="Invalid timezone"):
        get_timezone(timezone)
