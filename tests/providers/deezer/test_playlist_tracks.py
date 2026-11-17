"""Test the tracks of a Deezer playlist: when each was added and how they are numbered."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest
from music_assistant_models.errors import ResourceTemporarilyUnavailable
from music_assistant_models.media_items import ProviderMapping, Track

from music_assistant.constants import DB_TABLE_CACHE
from music_assistant.providers.deezer import browse
from music_assistant.providers.deezer.browse import DeezerBrowseManager
from music_assistant.providers.deezer.media import DeezerMediaManager
from music_assistant.providers.deezer.provider import DeezerProvider
from tests.common import use_real_create_task

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


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

    assert [(t.item_id, t.position) for t in tracks] == [("1", 1), ("3", 2), ("4", 3)]
    assert [t.date_added for t in tracks] == [
        datetime(2024, 3, 1, 12, 0, 0, tzinfo=UTC),
        datetime(2024, 5, 10, 8, 30, 0, tzinfo=UTC),
        None,
    ]


@pytest.mark.asyncio
async def test_move_to_top_past_an_unavailable_track(
    provider: DeezerProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unavailable first track leaves no gap that a block move could pick as its target."""
    page = _page([_edge(None, None), _edge("1", None), _edge("2", None)])
    # the playlist is read once to plan the block move and once for the move itself
    _serve_pages(provider, monkeypatch, [page, page])
    provider.mass.cache.delete = AsyncMock()  # type: ignore[method-assign]
    provider.media_manager = DeezerMediaManager(provider)
    move = AsyncMock(return_value=SimpleNamespace(move_status=True))
    provider.gql_client.move_track_in_playlist = move  # type: ignore[method-assign]

    await provider.move_playlist_tracks("123", [2], 1)

    move.assert_awaited_once_with(playlist_id="123", track_id="2", after_track_id=None)


@pytest.mark.asyncio
async def test_a_change_deezer_failed_to_confirm_is_read_anew_once_forgotten(
    provider: DeezerProvider, mass_minimal: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The kept tracks of a playlist are dropped through the provider after a failed change."""
    await mass_minimal.cache._setup_database()
    monkeypatch.setattr(browse, "parse_track", _parse_track)
    provider.mass = mass_minimal
    provider.browse_manager = DeezerBrowseManager(provider)
    provider.media_manager = DeezerMediaManager(provider)
    edges = [_edge("1", None)]

    async def _get_playlist(**_kwargs: Any) -> SimpleNamespace:
        return _page(list(edges))

    async def _add_tracks(**_kwargs: Any) -> None:
        # Deezer applies the change but its answer gets lost
        edges.append(_edge("2", None))
        raise ResourceTemporarilyUnavailable("Deezer did not answer")

    provider.gql_client.get_playlist = AsyncMock(side_effect=_get_playlist)  # type: ignore[method-assign]
    provider.gql_client.add_tracks_to_playlist = AsyncMock(side_effect=_add_tracks)  # type: ignore[method-assign]

    async def _listed() -> list[str]:
        return [track.item_id for track in await provider.get_playlist_tracks("123")]

    assert await _listed() == ["1"]
    await _wait_for_kept(mass_minimal, provider.instance_id)
    with pytest.raises(ResourceTemporarilyUnavailable):
        await provider.add_playlist_tracks("123", ["2"])
    assert await _listed() == ["1"]
    # what the playlist controller does after every change, however it ended
    await provider.forget_playlist_tracks("123")
    assert await _listed() == ["1", "2"]


async def _wait_for_kept(mass: MusicAssistant, provider_id: str) -> None:
    """Wait until a result is kept in the cache for the provider."""
    assert mass.cache.database is not None
    query = f"SELECT key FROM {DB_TABLE_CACHE} WHERE provider = :provider"
    for _ in range(200):
        if await mass.cache.database.get_count_from_query(query, {"provider": provider_id}):
            return
        await asyncio.sleep(0.01)
    pytest.fail(f"nothing was ever kept for {provider_id}")
