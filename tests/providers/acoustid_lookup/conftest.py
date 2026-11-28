"""Fixtures for the AcoustID Lookup provider tests."""

from __future__ import annotations

import pytest

from music_assistant.providers.acoustid_lookup.auth import THROTTLER


@pytest.fixture(autouse=True)
def _fresh_throttler() -> None:
    """Start every test without a wait or a used up rate limit left by another test."""
    THROTTLER._cooldown_until = 0.0
    THROTTLER.throttler._task_logs.clear()
