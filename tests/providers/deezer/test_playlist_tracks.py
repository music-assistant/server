"""Test that Deezer playlist tracks carry when each was added to the playlist."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from music_assistant_models.media_items import ProviderMapping, Track

from music_assistant.providers.deezer import browse
from music_assistant.providers.deezer.provider import DeezerProvider
from tests.common import use_real_create_task


def _edge(track_id: str | None, added_at: str | None) -> SimpleNamespace:
    """Return a playlist track edge as the GraphQL client parses it."""
    node = SimpleNamespace(id=track_id) if track_id else None
    return SimpleNamespace(node=node, added_at=added_at)


def _page(edges: list[SimpleNamespace], next_cursor: str | None = None) -> SimpleNamespace:
    """Return one page of a playlist's tracks."""
    page_info = SimpleNamespace(has_next_page=next_cursor is not None, end_cursor=next_cursor)
    return SimpleNamespace(tracks=SimpleNamespace(edges=edges, page_info=page_info))


def _parse_track(provider: DeezerProvider, node: Any, position: int = 0) -> Track:
    """Stand in for parse_track: a minimal track for the node."""
    return Track(
        item_id=node.id,
        provider=provider.instance_id,
        name=node.id,
        position=position,
        provider_mappings={
            ProviderMapping(
                item_id=node.id,
                provider_domain="deezer",
                provider_instance=provider.instance_id,
            )
        },
    )


def _serve_pages(
    provider: DeezerProvider, monkeypatch: pytest.MonkeyPatch, pages: list[SimpleNamespace]
) -> None:
    """Make the provider fetch these playlist pages, bypassing the cache."""
    monkeypatch.setattr(browse, "parse_track", _parse_track)
    provider.mass.cache.get_with_freshness = AsyncMock(  # type: ignore[method-assign]
        return_value=(None, False, False)
    )
    provider.mass.cache.set = AsyncMock()  # type: ignore[method-assign]
    use_real_create_task(provider.mass)
    provider.gql_client.get_playlist = AsyncMock(side_effect=pages)  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_playlist_tracks_carry_date_added(
    provider: DeezerProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each track gets the time it was added to the playlist, across pages."""
    _serve_pages(
        provider,
        monkeypatch,
        [
            _page(
                [_edge("1", "2024-03-01T12:00:00.000Z"), _edge(None, "2024-03-01T12:00:00.000Z")],
                next_cursor="c1",
            ),
            _page([_edge("3", "2024-05-10T08:30:00.000Z"), _edge("4", None)]),
        ],
    )

    tracks = await provider.browse_manager.get_playlist_tracks("123")

    assert [(t.item_id, t.position) for t in tracks] == [("1", 1), ("3", 3), ("4", 4)]
    assert [t.date_added for t in tracks] == [
        datetime(2024, 3, 1, 12, 0, 0, tzinfo=UTC),
        datetime(2024, 5, 10, 8, 30, 0, tzinfo=UTC),
        None,
    ]


@pytest.mark.asyncio
async def test_playlist_tracks_without_added_at_field(
    provider: DeezerProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client that doesn't report added_at still loads the playlist, without dates."""
    _serve_pages(provider, monkeypatch, [_page([SimpleNamespace(node=SimpleNamespace(id="1"))])])

    tracks = await provider.browse_manager.get_playlist_tracks("123")

    assert [(t.item_id, t.date_added) for t in tracks] == [("1", None)]
