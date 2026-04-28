"""Tests for sonic_analysis public API command handlers.

These cover the guard rails and shape of the responses returned by
_handle_status / _handle_analyzed_tracks / _handle_export_analysis.
Happy-path orchestration that requires real DB rows is exercised
through integration; these are unit-level checks that bypass the heavy
provider __init__ via ``object.__new__``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from music_assistant.providers.sonic_analysis import SonicAnalysisProvider


def _stub_provider(
    *,
    clap_model: Any = None,
    db: Any = None,
) -> SonicAnalysisProvider:
    """Build a minimal SonicAnalysisProvider with just enough state for the API tests."""
    p = SonicAnalysisProvider.__new__(SonicAnalysisProvider)
    p.logger = MagicMock()
    p.config = MagicMock()
    p.config.get_value = MagicMock(return_value=False)
    p._clap_model = clap_model
    p.mass = SimpleNamespace(  # type: ignore[assignment]
        music=SimpleNamespace(database=db, tracks=MagicMock()),
    )
    return p


@pytest.mark.asyncio
async def test_status_minimal_no_clap_no_db() -> None:
    """Status returns sensible defaults when CLAP and DB are both absent."""
    p = _stub_provider(db=None, clap_model=None)
    result = await p._handle_status()
    assert result["provider_loaded"] is True
    assert result["clap_model_loaded"] is False
    assert result["analyzed_tracks_count"] == 0
    assert result["analysis_version"] == p.analysis_version


@pytest.mark.asyncio
async def test_status_reports_clap_loaded_when_model_present() -> None:
    """Status reflects whether the CLAP model is loaded."""
    p = _stub_provider(clap_model=MagicMock())
    result = await p._handle_status()
    assert result["clap_model_loaded"] is True


@pytest.mark.asyncio
async def test_analyzed_tracks_raises_when_db_missing() -> None:
    """analyzed_tracks asserts the DB is present (caller's responsibility to gate)."""
    p = _stub_provider(db=None)
    with pytest.raises(AssertionError):
        await p._handle_analyzed_tracks(limit=10, offset=0)


@pytest.mark.asyncio
async def test_export_analysis_raises_when_db_missing() -> None:
    """export_analysis asserts the DB is present (caller's responsibility to gate)."""
    p = _stub_provider(db=None)
    with pytest.raises(AssertionError):
        await p._handle_export_analysis(limit=10, offset=0)
