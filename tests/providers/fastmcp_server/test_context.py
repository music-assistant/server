"""Tests for Context-driven observability and progress (C7)."""
# mypy: disable-error-code="arg-type, no-untyped-def, type-arg, assignment, operator, misc"

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import Client, FastMCP

from music_assistant.providers.fastmcp_server.tools import (
    build_library_server,
    build_metadata_server,
)


@pytest.fixture
def search_mass(mock_mass: MagicMock) -> MagicMock:
    """
    Configure mock_mass.music.search to return shaped results for assertions.

    Uses ``SimpleNamespace`` rather than ``MagicMock(name=…)`` deliberately —
    ``MagicMock``'s ``name`` constructor kwarg sets the mock's *repr* name,
    not the ``.name`` attribute, so a downstream attribute read would return
    a child ``MagicMock`` and the value-checked assertions would pass for
    the wrong reason (matching against ``str(mock)`` rather than ``.name``).
    """
    fake_track = SimpleNamespace(
        uri="library://track/1",
        name="Some Track",
        artists=[],
        album=None,
        duration=180,
    )
    mock_mass.music.search = AsyncMock(return_value=SimpleNamespace(tracks=[fake_track]))
    return mock_mass


async def test_search_tracks_emits_info_log(search_mass: MagicMock) -> None:
    """
    ``search_tracks`` emits a ``ctx.info`` log notification carrying the query.

    FastMCP delivers ``ctx.info`` calls to clients as ``notifications/message``
    events. The previous test installed the handler via a method that doesn't
    exist on ``Client`` (``set_message_handler``) — guarded by ``hasattr``,
    so the call was a no-op and the captured-log list never populated. The
    test then asserted on tool output instead, which would pass even if
    ``ctx.info`` were removed entirely. Now the handler is wired at
    construction time via the documented ``log_handler=`` constructor kwarg
    and the assertion checks the captured notifications.
    """
    mcp: FastMCP = FastMCP(name="t")
    mcp.mount(build_library_server(search_mass), namespace="library")

    captured: list[Any] = []

    async def collect(message: Any) -> None:
        captured.append(message)

    async with Client(mcp, log_handler=collect) as client:
        await client.call_tool("library_search_tracks", {"query": "smoke", "limit": 3})

    # ``ctx.info`` emits one notification whose ``data`` carries the formatted
    # message. The provider's search_tracks formats it as
    # ``"Searching MA for tracks matching 'smoke' (limit=3)"`` (or similar);
    # we only require the query to appear so wording changes don't break us.
    rendered = " ".join(str(getattr(m, "data", "")) for m in captured)
    assert "smoke" in rendered, f"ctx.info did not surface the query; captured={captured!r}"


async def test_search_tracks_returns_real_track_name(search_mass: MagicMock) -> None:
    """
    The returned ``TrackBrief`` carries the fake track's actual ``.name``.

    Previously the fake was built with ``MagicMock(name="Some Track")``, which
    set the mock's *repr* name rather than the ``.name`` attribute. The
    assertion ``"Some Track" in str(result)`` then passed because ``str(mock)``
    happened to include the repr name — but a regression that read e.g.
    ``track.title`` instead of ``track.name`` would have passed too.
    """
    mcp: FastMCP = FastMCP(name="t")
    mcp.mount(build_library_server(search_mass), namespace="library")

    async with Client(mcp) as client:
        result = await client.call_tool("library_search_tracks", {"query": "smoke", "limit": 3})

    text_blocks = [c.text for c in result.content if hasattr(c, "text")]
    parsed = [json.loads(t) for t in text_blocks if t.lstrip().startswith(("[", "{"))]
    flat: list[dict] = []
    for entry in parsed:
        if isinstance(entry, list):
            flat.extend(entry)
        else:
            flat.append(entry)
    names = [item.get("name") for item in flat if isinstance(item, dict)]
    assert "Some Track" in names, f"expected 'Some Track' in parsed names, got {names!r}"


async def test_recommendations_runs_under_context(mock_mass: MagicMock) -> None:
    """metadata.recommendations accepts an injected Context and still returns shaped data."""
    folder = SimpleNamespace(name="Hits", provider="library", item_id="hits")
    mock_mass.music.recommendations.get_recommendations = AsyncMock(return_value=[folder])
    mcp: FastMCP = FastMCP(name="t")
    mcp.mount(build_metadata_server(mock_mass), namespace="metadata")

    async with Client(mcp) as client:
        result = await client.call_tool("metadata_recommendations", {})

    # Assert against the parsed brief's actual fields, not against
    # ``str(mock)`` substring overlap.
    flat = _parsed_dicts(result)
    names = [item.get("name") for item in flat]
    assert "Hits" in names, f"expected 'Hits' folder in parsed result, got {names!r}"
    hits = next(item for item in flat if item.get("name") == "Hits")
    assert hits.get("provider") == "library"
    assert hits.get("item_id") == "hits"


async def test_recommendation_items_returns_row_items(mock_mass: MagicMock) -> None:
    """metadata.recommendation_items serves a row's items via the controller items call."""
    track = SimpleNamespace(uri="library://track/1", name="Some Track", media_type="track")
    mock_mass.music.recommendations.get_recommendation_items = AsyncMock(return_value=[track])
    mcp: FastMCP = FastMCP(name="t")
    mcp.mount(build_metadata_server(mock_mass), namespace="metadata")

    async with Client(mcp) as client:
        result = await client.call_tool(
            "metadata_recommendation_items",
            {"provider": "library", "item_id": "hits"},
        )

    mock_mass.music.recommendations.get_recommendation_items.assert_awaited_once_with(
        "library", "hits"
    )
    flat = _parsed_dicts(result)
    uris = [item.get("uri") for item in flat]
    assert "library://track/1" in uris, f"expected track uri in parsed result, got {uris!r}"


def _parsed_dicts(result: Any) -> list[dict]:
    """Flatten a tool result's JSON text blocks into a list of dicts."""
    text_blocks = [c.text for c in result.content if hasattr(c, "text")]
    parsed = [json.loads(t) for t in text_blocks if t.lstrip().startswith(("[", "{"))]
    flat: list[dict] = []
    for entry in parsed:
        if isinstance(entry, list):
            flat.extend(entry)
        else:
            flat.append(entry)
    return [item for item in flat if isinstance(item, dict)]


# Playlists routing tests live in tests/test_playlists.py — that suite covers
# the per-track loop, progress reporting, and the URI/int normalisation
# contract that supersedes the previous ≤10 fast-path tests.
