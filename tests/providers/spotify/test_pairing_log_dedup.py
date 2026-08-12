"""
Tests for logging output from librespot's pairing (Spotify Connect advertisement) process.

librespot repeats identical WARN/ERROR lines (most commonly libmdns' "No route to host") on
every advertisement retry, even when pairing itself succeeds. Only the first occurrence of a
given line is logged as a warning so a real (distinct) problem stays visible without flooding
the log; exact repeats are demoted to debug.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import MagicMock

import pytest

from music_assistant.providers.spotify.helpers import _log_pairing_output


async def test_duplicate_warning_lines_are_demoted_after_first(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A repeated warning line is logged as a warning only once; repeats become debug."""
    lines = [
        "WARN libmdns: No route to host (os error 65)",
        "WARN libmdns: No route to host (os error 65)",
        "WARN libmdns: No route to host (os error 65)",
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
        "WARN libmdns: No route to host (os error 65)",
        "ERROR librespot: failed to authenticate",
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
