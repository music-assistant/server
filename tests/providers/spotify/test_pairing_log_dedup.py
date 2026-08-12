"""
Tests for logging output from librespot's pairing (Spotify Connect advertisement) process.

librespot repeats identical WARN/ERROR lines (most commonly libmdns' "No route to host") on
every advertisement retry, even when pairing itself succeeds. Each line is prefixed with a
timestamp that changes on every repeat (e.g. "[2026-08-12T01:02:42Z WARN  libmdns::fsm] ...")
so repeats are recognized by their level, target and message, ignoring the timestamp. Only the
first occurrence of an otherwise identical line is logged as a warning so a real (distinct)
problem stays visible without flooding the log; exact repeats are demoted to debug.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import MagicMock

import pytest

from music_assistant.providers.spotify.helpers import _log_pairing_output

_MDNS_WARNING = (
    "[{timestamp} WARN  libmdns::fsm] error sending packet "
    'Os {{ code: 65, kind: HostUnreachable, message: "No route to host" }}'
)
_AUTH_ERROR = "[{timestamp} ERROR librespot_core::session] failed to authenticate"


async def test_duplicate_warning_lines_are_demoted_after_first(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    A warning repeated at different timestamps is logged as a warning only once.

    librespot's timestamp changes on every repeat, so the dedupe key must ignore it or every
    repeat would still be logged as a fresh warning.
    """
    lines = [
        _MDNS_WARNING.format(timestamp="2026-08-12T01:02:42Z"),
        _MDNS_WARNING.format(timestamp="2026-08-12T01:02:43Z"),
        _MDNS_WARNING.format(timestamp="2026-08-12T01:02:44Z"),
    ]
    await _log_pairing_output(_fake_process(lines))

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    debugs = [r for r in caplog.records if r.levelname == "DEBUG"]
    assert len(warnings) == 1
    assert len(debugs) == 2


async def test_distinct_warning_lines_all_stay_warnings(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Different warning lines are each surfaced, since they may point at different problems."""
    lines = [
        _MDNS_WARNING.format(timestamp="2026-08-12T01:02:42Z"),
        _AUTH_ERROR.format(timestamp="2026-08-12T01:02:43Z"),
    ]
    await _log_pairing_output(_fake_process(lines))

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 2


def _fake_process(lines: list[str]) -> MagicMock:
    """Return a mock AsyncProcess whose iter_stderr yields the given lines."""

    async def _iter_stderr() -> AsyncIterator[str]:
        for line in lines:
            yield line

    proc = MagicMock()
    proc.iter_stderr = _iter_stderr
    return proc
