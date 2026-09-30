"""Test that Audiobookshelf serves series as collections and authors/narrators as artists."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aioaudiobookshelf.schema.author import AuthorExpanded
from aioaudiobookshelf.schema.shelf import SeriesShelf, ShelfAuthors, ShelfSeries
from aioaudiobookshelf.schema.shelf import ShelfId as AbsShelfId
from aioaudiobookshelf.schema.shelf import ShelfType as AbsShelfType
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import (
    Artist,
    MediaCollection,
    MediaItemType,
    RecommendationFolder,
    UniqueList,
)

from music_assistant.helpers.collections import get_collection_item_id
from music_assistant.providers.audiobookshelf import Audiobookshelf

ARTISTS = {
    "aut1": Artist(
        item_id="1", provider="library", name="Terry Pratchett", provider_mappings=set()
    ),
    "aut2": Artist(item_id="2", provider="library", name="Robert Jordan", provider_mappings=set()),
}


def _collection(name: str) -> MediaCollection[Mock]:
    return MediaCollection(
        item_id=get_collection_item_id(name, MediaType.AUDIOBOOK),
        name=name,
        provider="library",
        provider_mappings=set(),
        items=UniqueList(),
    )


def _serve_persisted_payload(provider: Audiobookshelf, folders: list[RecommendationFolder]) -> None:
    """Serve the folders from the persistent cache, serialized like the cache db stores them."""
    restored = [RecommendationFolder.from_dict(x.to_dict()) for x in folders]
    provider.mass.cache.get_with_freshness = AsyncMock(  # type: ignore[method-assign]
        return_value=(restored, True, True)
    )
    provider.mass.create_task = Mock(  # type: ignore[method-assign]
        side_effect=lambda coro, **_kwargs: asyncio.ensure_future(coro)
    )


def _stub_library(provider: Audiobookshelf) -> None:
    """Serve collapsed library audiobooks and library artists."""
    provider.mass.music.audiobooks.library_items = AsyncMock(  # type: ignore[method-assign]
        return_value=[_collection("Wheel of Time"), Mock(), _collection("Discworld")]
    )

    async def _get_library_item_by_prov_id(item_id: str, **_kwargs: str) -> Artist | None:
        return ARTISTS.get(item_id)

    provider.mass.music.get_library_item_by_prov_id = AsyncMock(  # type: ignore[method-assign]
        side_effect=_get_library_item_by_prov_id
    )


@pytest.mark.asyncio
async def test_recommendation_shelves(provider: Audiobookshelf) -> None:
    """Series shelves hold collections and author shelves hold artists, not folders."""
    _stub_library(provider)
    series_shelf = Mock(spec=ShelfSeries)
    series_shelf.id_ = AbsShelfId.RECENT_SERIES
    series_shelf.type_ = AbsShelfType.SERIES
    series_shelf.entities = []
    for name in ("Discworld", "Not synced", "Wheel of Time"):
        entity = Mock(spec=SeriesShelf)
        entity.name = name
        series_shelf.entities.append(entity)
    authors_shelf = Mock(spec=ShelfAuthors)
    authors_shelf.id_ = AbsShelfId.NEWEST_AUTHORS
    authors_shelf.type_ = AbsShelfType.AUTHORS
    authors_shelf.entities = []
    for author_id, num_books in (("aut1", 3), ("aut2", 0)):
        entity = Mock(spec=AuthorExpanded)
        entity.id_ = author_id
        entity.num_books = num_books
        authors_shelf.entities.append(entity)
    items_by_shelf_id: dict[AbsShelfId, list[list[MediaItemType]]] = {}

    await provider._recommendations_iter_shelves([series_shelf, authors_shelf], items_by_shelf_id)

    (series,) = items_by_shelf_id[AbsShelfId.RECENT_SERIES]
    assert [(type(x), x.name) for x in series] == [
        (MediaCollection, "Discworld"),
        (MediaCollection, "Wheel of Time"),
    ]
    assert items_by_shelf_id[AbsShelfId.NEWEST_AUTHORS] == [[ARTISTS["aut1"]]]


@pytest.mark.parametrize(
    ("path_key", "client_method"),
    [("a", "get_library_authors"), ("n", "get_library_narrators")],
)
@pytest.mark.asyncio
async def test_browse_authors_and_narrators(
    provider: Audiobookshelf, path_key: str, client_method: str
) -> None:
    """Authors and narrators browse to their library artists, sorted by name."""
    _stub_library(provider)
    setattr(
        provider._client,
        client_method,
        AsyncMock(return_value=[SimpleNamespace(id_=x) for x in ("aut1", "missing", "aut2")]),
    )

    items = await provider.browse(f"{provider.instance_id}://lb lib1/{path_key}")

    assert items == [ARTISTS["aut2"], ARTISTS["aut1"]]


@pytest.mark.asyncio
async def test_browse_series(provider: Audiobookshelf) -> None:
    """Series browse to the library collections of the library's series, sorted by name."""
    _stub_library(provider)

    async def _get_library_series(**_kwargs: str) -> AsyncGenerator[SimpleNamespace]:
        names = ("Wheel of Time", "Not synced", "Discworld")
        yield SimpleNamespace(results=[SimpleNamespace(name=x) for x in names])
        yield SimpleNamespace(results=[])

    provider._client.get_library_series = _get_library_series  # type: ignore[method-assign,assignment]

    items = await provider.browse(f"{provider.instance_id}://lb lib1/s")

    assert [(type(x), x.name) for x in items] == [
        (MediaCollection, "Discworld"),
        (MediaCollection, "Wheel of Time"),
    ]


@pytest.mark.asyncio
async def test_series_row_from_persisted_payload(provider: Audiobookshelf) -> None:
    """A series row restored from the persisted payload serves collections, not item mappings."""
    _stub_library(provider)
    folder = RecommendationFolder(
        item_id=AbsShelfId.RECENT_SERIES,
        provider=provider.instance_id,
        name="Recent series",
        items=UniqueList([_collection("Discworld")]),
    )
    _serve_persisted_payload(provider, [folder])

    items = await provider.get_recommendation_items(AbsShelfId.RECENT_SERIES)

    assert [(type(x), x.name) for x in items] == [(MediaCollection, "Discworld")]


@pytest.mark.parametrize("sync_artists", [True, False])
@pytest.mark.asyncio
async def test_authors_and_narrators_need_artist_sync(
    provider: Audiobookshelf, sync_artists: bool
) -> None:
    """Authors and narrators only show in browse and recommendations with artist sync enabled."""
    provider.config.get_value.side_effect = lambda key, default=None: {  # type: ignore[attr-defined]
        "library_sync_artists": sync_artists
    }.get(key, default)
    folder = RecommendationFolder(
        item_id=AbsShelfId.NEWEST_AUTHORS,
        provider=provider.instance_id,
        name="Newest authors",
        items=UniqueList([ARTISTS["aut1"]]),
    )
    _serve_persisted_payload(provider, [folder])

    folders = await provider.browse(f"{provider.instance_id}://lb lib1")
    rows = await provider.get_recommendations()

    expected = {"authors", "narrators"} if sync_artists else set()
    assert {x.item_id for x in folders} & {"authors", "narrators"} == expected
    assert (AbsShelfId.NEWEST_AUTHORS in [x.item_id for x in rows]) is sync_artists
