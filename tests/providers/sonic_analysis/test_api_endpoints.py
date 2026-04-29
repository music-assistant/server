"""Tests for sonic_analysis public API command handlers.

These cover the guard rails and shape of the responses returned by
_handle_status / _handle_analyzed_tracks / _handle_export_analysis,
and verify each one routes through the AudioAnalysisController helpers
(get_audio_analysis_count / get_audio_analysis_rows) rather than touching the
database directly. The handlers are unit-tested via ``object.__new__``
to bypass the heavy provider __init__.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from music_assistant.providers.sonic_analysis import SonicAnalysisProvider


def _stub_provider(
    *,
    clap_model: Any = None,
    count: int = 0,
    rows: list[dict[str, Any]] | None = None,
) -> tuple[SonicAnalysisProvider, MagicMock, MagicMock]:
    """Build a SonicAnalysisProvider plus the mocks the tests assert against.

    :returns: (provider, aa_controller_mock, tracks_mock) — using mocks
        directly avoids mypy fighting AsyncMock substitution on attributes
        whose real type is a coroutine method.
    """
    p = SonicAnalysisProvider.__new__(SonicAnalysisProvider)
    p.logger = MagicMock()
    p.config = MagicMock()
    p.config.get_value = MagicMock(return_value=False)
    p._clap_model = clap_model
    p.manifest = MagicMock(domain="sonic_analysis")

    aa_controller = MagicMock()
    aa_controller.get_audio_analysis_count = AsyncMock(return_value=count)
    aa_controller.get_audio_analysis_rows = AsyncMock(return_value=rows or [])

    fake_track = MagicMock()
    fake_track.name = "name"
    fake_track.artists = []
    tracks = MagicMock()
    tracks.get = AsyncMock(return_value=fake_track)

    p.mass = SimpleNamespace(  # type: ignore[assignment]
        streams=SimpleNamespace(audio_analysis=aa_controller),
        music=SimpleNamespace(tracks=tracks),
    )
    return p, aa_controller, tracks


@pytest.mark.asyncio
async def test_status_minimal_no_clap() -> None:
    """Status returns sensible defaults when CLAP is absent and no rows exist."""
    p, _, _ = _stub_provider(clap_model=None, count=0)
    result = await p._handle_status()
    assert result["provider_loaded"] is True
    assert result["clap_model_loaded"] is False
    assert result["analyzed_tracks_count"] == 0
    assert result["analysis_version"] == p.analysis_version


@pytest.mark.asyncio
async def test_status_routes_count_through_controller() -> None:
    """_handle_status pulls the count from the controller helper, not the DB."""
    p, aa, _ = _stub_provider(clap_model=MagicMock(), count=42)
    result = await p._handle_status()
    assert result["clap_model_loaded"] is True
    assert result["analyzed_tracks_count"] == 42
    aa.get_audio_analysis_count.assert_awaited_once_with("sonic_analysis")


@pytest.mark.asyncio
async def test_analyzed_tracks_dedupes_and_paginates() -> None:
    """_handle_analyzed_tracks dedupes (item_id, provider) pairs and respects offset/limit."""
    rows = [
        {"item_id": "a", "provider": "filesystem_local"},
        {"item_id": "a", "provider": "filesystem_local"},  # duplicate, should drop
        {"item_id": "b", "provider": "filesystem_local"},
        {"item_id": "c", "provider": "filesystem_local"},
    ]
    p, aa, _ = _stub_provider(rows=rows)

    result = await p._handle_analyzed_tracks(limit=2, offset=0)
    assert result["total"] == 3  # deduped
    assert len(result["items"]) == 2  # limited
    aa.get_audio_analysis_rows.assert_awaited_once_with("sonic_analysis")


@pytest.mark.asyncio
async def test_export_analysis_extracts_fields_and_extra_data() -> None:
    """_handle_export_analysis reads rows via the controller and returns scalars + extra_data."""
    rows = [
        {
            "item_id": "track1",
            "provider": "filesystem_local",
            "analysis_data": json.dumps(
                {
                    "bpm": 120.5,
                    "danceability": 0.7,
                    "extra_data": {"clap_embedding": [0.0] * 1024},
                }
            ),
        }
    ]
    p, aa, _ = _stub_provider(rows=rows)

    result = await p._handle_export_analysis(limit=10, offset=0)
    assert result["total"] == 1
    item = result["items"][0]
    assert item["bpm"] == 120.5
    assert item["danceability"] == 0.7
    assert item["extra_data"]["clap_embedding"] == [0.0] * 1024
    aa.get_audio_analysis_rows.assert_awaited_once_with("sonic_analysis")


@pytest.mark.asyncio
async def test_export_analysis_skips_unparseable_rows() -> None:
    """Rows with corrupt JSON are silently skipped (defensive against legacy data)."""
    rows = [
        {"item_id": "a", "provider": "filesystem_local", "analysis_data": "not json"},
        {
            "item_id": "b",
            "provider": "filesystem_local",
            "analysis_data": json.dumps({"bpm": 100.0}),
        },
    ]
    p, _, _ = _stub_provider(rows=rows)

    result = await p._handle_export_analysis(limit=10, offset=0)
    assert result["total"] == 1
    assert result["items"][0]["bpm"] == 100.0
