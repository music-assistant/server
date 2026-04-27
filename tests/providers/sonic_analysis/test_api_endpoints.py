"""Tests for sonic_analysis public API command handlers.

These cover the guard rails and shape of the responses returned by
_handle_status / _handle_text_search / _handle_rebuild_text_search_index
/ _handle_analyzed_tracks. Happy-path orchestration that requires real
DB rows is exercised through integration; these are unit-level checks
that bypass the heavy provider __init__ via ``object.__new__``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from music_assistant.providers.sonic_analysis import SonicAnalysisProvider


def _stub_provider(*, clap_index=None, clap_model=None, db=None) -> SonicAnalysisProvider:
    """Build a minimal SonicAnalysisProvider with just enough state for the API tests."""
    p = SonicAnalysisProvider.__new__(SonicAnalysisProvider)
    p.logger = MagicMock()
    p.config = MagicMock()
    p.config.get_value = MagicMock(return_value=False)
    p._clap_index = clap_index
    p._clap_model = clap_model
    p.mass = SimpleNamespace(
        music=SimpleNamespace(database=db, tracks=MagicMock()),
    )
    return p


@pytest.mark.asyncio
async def test_status_minimal_no_clap_no_db() -> None:
    """status returns sensible defaults when CLAP and DB are both absent."""
    p = _stub_provider(db=None, clap_index=None, clap_model=None)
    result = await p._handle_status()
    assert result["provider_loaded"] is True
    assert result["clap_model_loaded"] is False
    assert result["text_search_enabled"] is False
    assert result["text_search_index_size"] == 0
    assert result["analyzed_tracks_count"] == 0
    assert result["analysis_version"] == p.analysis_version


@pytest.mark.asyncio
async def test_status_reports_clap_index_size_when_loaded() -> None:
    """status surfaces the actual index size when CLAP is loaded."""
    fake_index = MagicMock()
    fake_index.__len__ = MagicMock(return_value=42)
    p = _stub_provider(clap_index=fake_index, clap_model=MagicMock())
    result = await p._handle_status()
    assert result["clap_model_loaded"] is True
    assert result["text_search_index_size"] == 42


@pytest.mark.asyncio
async def test_text_search_returns_error_when_index_disabled() -> None:
    """text_search surfaces a user-actionable error when the index is None."""
    p = _stub_provider(clap_index=None, clap_model=None)
    result = await p._handle_text_search(query="dancy disco", k=5)
    assert result["query"] == "dancy disco"
    assert result["k"] == 5
    assert result["items"] == []
    assert "compute_text_search_embedding" in (result.get("error") or "")


@pytest.mark.asyncio
async def test_rebuild_text_search_index_invokes_rebuild() -> None:
    """rebuild_text_search_index handler delegates and reports the new size."""
    fake_index = MagicMock()
    fake_index.__len__ = MagicMock(return_value=0)
    p = _stub_provider(clap_index=fake_index)
    p.rebuild_text_search_index = AsyncMock()
    result = await p._handle_rebuild_text_search_index()
    p.rebuild_text_search_index.assert_awaited_once()
    assert result["status"] == "rebuilt"
    assert result["text_search_index_size"] == 0


@pytest.mark.asyncio
async def test_analyzed_tracks_returns_empty_when_db_missing() -> None:
    """analyzed_tracks short-circuits to an empty page when DB is unavailable."""
    p = _stub_provider(db=None)
    result = await p._handle_analyzed_tracks(limit=10, offset=0)
    assert result["total"] == 0
    assert result["items"] == []


@pytest.mark.asyncio
async def test_export_analysis_returns_empty_when_db_missing() -> None:
    """export_analysis short-circuits to an empty page when DB is unavailable."""
    p = _stub_provider(db=None)
    result = await p._handle_export_analysis(limit=10, offset=0)
    assert result["total"] == 0
    assert result["items"] == []
