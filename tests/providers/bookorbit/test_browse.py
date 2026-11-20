"""Tests for BookOrbit browse path routing and search."""

from __future__ import annotations

import asyncio
import time
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import ClientResponseError
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import BrowseFolder

from music_assistant.providers.bookorbit import SUPPORTED_FEATURES, BookOrbitProvider
from music_assistant.providers.bookorbit.constants import CATALOG_TTL_SEC
from music_assistant.providers.bookorbit.helpers import (
    collection_sequence_key,
    compare_series_indices,
    parse_browse_path,
    parse_series_index,
    series_index_sort_key,
)
from tests.providers.bookorbit.conftest import manifest_from_book, wire_mass_create_task

INSTANCE = "bookorbit--test"


@pytest.fixture
def provider(book_audiobook: dict[str, Any], book_multifile: dict[str, Any]) -> BookOrbitProvider:
    """Create a provider with a mocked client and mass; library 1 holds both books."""
    mass = Mock()
    wire_mass_create_task(mass)
    mass.cache.get = AsyncMock(return_value=None)
    mass.cache.set = AsyncMock()
    mass.music.audiobooks.get_library_items_by_prov_id = AsyncMock(return_value=[])
    manifest = Mock()
    manifest.domain = "bookorbit"
    config = Mock()
    config.instance_id = INSTANCE
    config.name = "BookOrbit Test"
    config.enabled = True
    config.get_value.side_effect = lambda key, default=None: {"log_level": "GLOBAL"}.get(
        key, default
    )
    prov = BookOrbitProvider(mass, manifest, config, SUPPORTED_FEATURES)
    prov.base_url = "http://bo"
    prov._client = Mock()
    prov._client.list_libraries = AsyncMock(
        return_value=[{"id": 1, "name": "Audio"}, {"id": 2, "name": "Ebooks"}]
    )
    books = {
        1: [
            {"id": book_audiobook["id"], "updatedAt": book_audiobook.get("updatedAt")},
            {"id": book_multifile["id"], "updatedAt": book_multifile.get("updatedAt")},
        ],
        2: [],
    }
    prov._client.query_audiobooks = AsyncMock(side_effect=lambda library_id: books[library_id])
    details = {book_audiobook["id"]: book_audiobook, book_multifile["id"]: book_multifile}
    prov._client.get_book = AsyncMock(side_effect=lambda book_id: details[book_id])
    manifests = {
        book_audiobook["id"]: manifest_from_book(book_audiobook),
        book_multifile["id"]: manifest_from_book(book_multifile),
    }
    prov._client.get_audiobook_manifest = AsyncMock(
        side_effect=lambda book_id: manifests.get(book_id, {"assets": []})
    )
    prov._client.list_authors = AsyncMock(return_value=[])
    return prov


async def _browse(provider: BookOrbitProvider, path: str) -> list[Any]:
    return list(await provider.browse(path))


def test_parse_browse_path() -> None:
    """Browse paths split into library id and sub path."""
    assert parse_browse_path(f"{INSTANCE}://") == (None, [])
    assert parse_browse_path(f"{INSTANCE}://lb 3") == (3, [])
    assert parse_browse_path(f"{INSTANCE}://lb 3/a") == (3, ["a"])
    assert parse_browse_path(f"{INSTANCE}://lb 3/a/author:11") == (3, ["a", "author:11"])
    for bad in (f"{INSTANCE}://lp 3", f"{INSTANCE}://lb", f"{INSTANCE}://lb x"):
        with pytest.raises(ValueError, match="BookOrbit browse path"):
            parse_browse_path(bad)


def test_series_index_uses_integer_segments_not_float() -> None:
    """BookOrbit orders ``1.10`` after ``1.2``; float() would reverse that."""
    assert parse_series_index("1.10") == "1.10"
    assert parse_series_index(1.10) == "1.1"  # numeric 1.10 collapses; strings preserve
    assert compare_series_indices("1.2", "1.10") < 0
    assert compare_series_indices("1", "1.2") < 0
    ordered = sorted(["1.10", "1.2", "1", "2"], key=series_index_sort_key)
    assert ordered == ["1", "1.2", "1.10", "2"]


def test_collection_sequence_key_returns_plain_display_index() -> None:
    """Collection sequences are plain series indices shown to users."""
    assert collection_sequence_key("1") == "1"
    assert collection_sequence_key("1.10") == "1.10"
    assert collection_sequence_key(2) == "2"
    assert collection_sequence_key("not-an-index") is None


@pytest.mark.asyncio
async def test_browse_tree(provider: BookOrbitProvider, book_audiobook: dict[str, Any]) -> None:
    """Root lists audio libraries; sections, people, series and books route correctly."""
    root = await _browse(provider, f"{INSTANCE}://")
    assert [(f.item_id, f.path) for f in root] == [
        ("1", f"{INSTANCE}://lb 1")
    ]  # ebook-only library is not listed

    lib = await _browse(provider, f"{INSTANCE}://lb 1")
    assert [f.path.split("/")[-1] for f in lib] == [
        "a",
        "n",
        "s",
        "b",
    ]

    author_id = f"author:{book_audiobook['authors'][0]['id']}"
    authors = await _browse(provider, f"{INSTANCE}://lb 1/a")
    assert all(isinstance(a, BrowseFolder) for a in authors)
    folder = next(a for a in authors if a.item_id == author_id)
    assert folder.path == f"{INSTANCE}://lb 1/a/{author_id}"
    assert folder.name == book_audiobook["authors"][0]["name"]
    books = await _browse(provider, folder.path)
    assert [b.item_id for b in books] == [str(book_audiobook["id"])]

    narrators = await _browse(provider, f"{INSTANCE}://lb 1/n")
    assert narrators
    assert all(n.item_id.startswith("narrator:") for n in narrators)

    series = await _browse(provider, f"{INSTANCE}://lb 1/s")
    assert [(s.item_id, s.name) for s in series] == [("7", "Dune Chronicles")]
    series_books = await _browse(provider, f"{INSTANCE}://lb 1/s/7")
    assert [b.item_id for b in series_books] == [str(book_audiobook["id"])]

    all_books = await _browse(provider, f"{INSTANCE}://lb 1/b")
    assert len(all_books) == 2

    assert await _browse(provider, f"{INSTANCE}://lb 99") == []
    assert await _browse(provider, f"{INSTANCE}://lb 1/x") == []
    assert await _browse(provider, f"{INSTANCE}://nonsense") == []


@pytest.mark.asyncio
async def test_browse_series_orders_by_segmented_index(
    provider: BookOrbitProvider, book_audiobook: dict[str, Any]
) -> None:
    """Series browse uses BookOrbit segment order, not float decimal order."""
    later = {
        **book_audiobook,
        "id": 43,
        "title": "Dune Later",
        "seriesIndex": "1.10",
        "seriesMemberships": [
            {
                "seriesId": 7,
                "seriesName": "Dune Chronicles",
                "seriesIndex": "1.10",
                "displayOrder": 0,
            }
        ],
    }
    earlier = {
        **book_audiobook,
        "id": 44,
        "title": "Dune Earlier",
        "seriesIndex": "1.2",
        "seriesMemberships": [
            {
                "seriesId": 7,
                "seriesName": "Dune Chronicles",
                "seriesIndex": "1.2",
                "displayOrder": 0,
            }
        ],
    }
    provider._catalog = {
        str(book_audiobook["id"]): book_audiobook,
        "43": later,
        "44": earlier,
    }
    provider._library_books = {1: [str(book_audiobook["id"]), "43", "44"]}
    provider._library_names = {1: "Audio"}
    # Keep the injected catalog inside the TTL so browse does not refetch.
    provider._catalog_refreshed_at = time.monotonic()
    series_books = await _browse(provider, f"{INSTANCE}://lb 1/s/7")
    assert [b.item_id for b in series_books] == [str(book_audiobook["id"]), "44", "43"]


@pytest.mark.asyncio
async def test_search_audiobooks_and_artists(
    provider: BookOrbitProvider, book_audiobook: dict[str, Any]
) -> None:
    """Search maps API hits into audiobooks and (catalog-known) artists."""
    client = cast("Mock", provider._client)
    author = book_audiobook["authors"][0]
    client.search_books = AsyncMock(return_value=[{"id": 12345}, {"id": book_audiobook["id"]}])
    missing = ClientResponseError(Mock(), (), status=404)
    real_get_book = client.get_book.side_effect
    client.get_book = AsyncMock(
        side_effect=lambda book_id: (
            (_ for _ in ()).throw(missing) if book_id == 12345 else real_get_book(book_id)
        )
    )
    client.search_authors = AsyncMock(return_value=[author, {"id": 98765, "name": "Unknown"}])
    client.search_narrators = AsyncMock(return_value=[])
    result = await provider.search("dune", [MediaType.AUDIOBOOK, MediaType.ARTIST], limit=5)
    assert [b.item_id for b in result.audiobooks] == [str(book_audiobook["id"])]
    assert [a.item_id for a in result.artists] == [f"author:{author['id']}"]


@pytest.mark.asyncio
async def test_search_audiobooks_merges_catalog_when_api_page_is_ebooks(
    provider: BookOrbitProvider, book_audiobook: dict[str, Any]
) -> None:
    """A successful ebook-heavy search page must still surface catalog audiobooks."""
    await provider._ensure_catalog()
    client = cast("Mock", provider._client)
    # Default BookOrbit search page is 10 mixed-format hits; none are the audiobook.
    client.search_books = AsyncMock(
        return_value=[
            {"id": i, "files": [{"id": i, "format": "epub", "role": "primary"}]}
            for i in range(1000, 1010)
        ]
    )
    client.get_book = AsyncMock(side_effect=AssertionError("ebook search must not detail-fetch"))
    client.search_authors = AsyncMock(return_value=[])
    client.search_narrators = AsyncMock(return_value=[])
    result = await provider.search("dune", [MediaType.AUDIOBOOK], limit=5)
    assert str(book_audiobook["id"]) in {b.item_id for b in result.audiobooks}
    client.get_book.assert_not_awaited()


@pytest.mark.asyncio
async def test_search_artists_merges_catalog_when_api_page_is_ebook_authors(
    provider: BookOrbitProvider, book_audiobook: dict[str, Any]
) -> None:
    """Ebook-only metadata search pages must not hide catalog audiobook authors."""
    await provider._ensure_catalog()
    client = cast("Mock", provider._client)
    author = book_audiobook["authors"][0]
    client.search_books = AsyncMock(return_value=[])
    # Metadata search returns other names only (ebook authors filling the page).
    client.search_authors = AsyncMock(
        return_value=[{"name": f"Ebook Author {i}"} for i in range(15)]
    )
    client.search_narrators = AsyncMock(return_value=[])
    result = await provider.search(author["name"][:4], [MediaType.ARTIST], limit=20)
    assert f"author:{author['id']}" in {a.item_id for a in result.artists}


@pytest.mark.asyncio
async def test_search_falls_back_to_catalog(
    provider: BookOrbitProvider, book_audiobook: dict[str, Any]
) -> None:
    """When the search endpoints fail, the synced catalog is searched by name."""
    error = ClientResponseError(Mock(), (), status=404)
    client = cast("Mock", provider._client)
    client.search_books = AsyncMock(side_effect=error)
    client.search_authors = AsyncMock(side_effect=error)
    client.search_narrators = AsyncMock(side_effect=error)
    title = book_audiobook["title"]
    result = await provider.search(title[:4], [MediaType.AUDIOBOOK, MediaType.ARTIST])
    assert str(book_audiobook["id"]) in {b.item_id for b in result.audiobooks}
    name = book_audiobook["authors"][0]["name"]
    other = await provider.search(name[:5], [MediaType.ARTIST])
    assert f"author:{book_audiobook['authors'][0]['id']}" in {a.item_id for a in other.artists}
    assert (await provider.search("  ", [MediaType.AUDIOBOOK])).audiobooks == []


@pytest.mark.asyncio
async def test_search_falls_back_to_catalog_on_timeout(
    provider: BookOrbitProvider, book_audiobook: dict[str, Any]
) -> None:
    """Timeouts and connection errors must not drop catalog search matches."""
    client = cast("Mock", provider._client)
    client.search_books = AsyncMock(side_effect=TimeoutError("slow"))
    client.search_authors = AsyncMock(side_effect=TimeoutError("slow"))
    client.search_narrators = AsyncMock(side_effect=TimeoutError("slow"))
    title = book_audiobook["title"]
    result = await provider.search(title[:4], [MediaType.AUDIOBOOK, MediaType.ARTIST])
    assert str(book_audiobook["id"]) in {b.item_id for b in result.audiobooks}


@pytest.mark.asyncio
async def test_audiobook_items_survives_catalog_refresh_during_library_lookup(
    provider: BookOrbitProvider, book_audiobook: dict[str, Any]
) -> None:
    """A concurrent catalog refresh must not KeyError mid-browse resolution."""
    provider._catalog = {str(book_audiobook["id"]): book_audiobook}
    provider._library_books = {1: [str(book_audiobook["id"])]}
    provider._library_names = {1: "Audio"}
    provider._catalog_refreshed_at = time.monotonic()
    started = asyncio.Event()

    async def _swap_catalog(*_args: Any, **_kwargs: Any) -> list[Any]:
        started.set()
        provider._catalog = {}
        return []

    cast("Any", provider.mass.music.audiobooks).get_library_items_by_prov_id = AsyncMock(
        side_effect=_swap_catalog
    )
    items = await provider._audiobook_items([str(book_audiobook["id"])])
    assert started.is_set()
    assert [item.item_id for item in items] == [str(book_audiobook["id"])]


@pytest.mark.asyncio
async def test_browse_and_search_refresh_catalog_after_ttl(provider: BookOrbitProvider) -> None:
    """Browse and search honor the catalog TTL instead of serving a forever-stale cache."""
    client = cast("Mock", provider._client)
    client.search_books = AsyncMock(return_value=[])
    client.search_authors = AsyncMock(return_value=[])
    client.search_narrators = AsyncMock(return_value=[])
    await _browse(provider, f"{INSTANCE}://")
    assert client.list_libraries.await_count == 1
    provider._catalog_refreshed_at -= 10 * CATALOG_TTL_SEC
    await _browse(provider, f"{INSTANCE}://lb 1/b")
    await provider.search("dune", [MediaType.AUDIOBOOK, MediaType.ARTIST])
    assert client.list_libraries.await_count >= 2


@pytest.mark.asyncio
async def test_concurrent_catalog_loads_are_single_flight(provider: BookOrbitProvider) -> None:
    """Concurrent browses on an empty catalog trigger one catalog refresh."""
    client = cast("Mock", provider._client)
    libraries = client.list_libraries.return_value

    async def slow_libraries() -> list[dict[str, Any]]:
        await asyncio.sleep(0.02)
        return cast("list[dict[str, Any]]", libraries)

    client.list_libraries = AsyncMock(side_effect=slow_libraries)
    results = await asyncio.gather(*(_browse(provider, f"{INSTANCE}://") for _ in range(3)))
    assert all(len(root) == 1 for root in results)
    client.list_libraries.assert_awaited_once()
    await provider._refresh_catalog(force=True)
    assert client.list_libraries.await_count == 2


@pytest.mark.asyncio
async def test_browse_audiobooks_batches_library_lookups(
    provider: BookOrbitProvider, book_audiobook: dict[str, Any], book_multifile: dict[str, Any]
) -> None:
    """Audiobook browse uses one batched library lookup instead of per-book queries."""
    await _browse(provider, f"{INSTANCE}://lb 1/b")
    batch = cast("AsyncMock", provider.mass.music.audiobooks.get_library_items_by_prov_id)
    batch.assert_awaited_once()
    assert batch.await_args is not None
    kwargs = batch.await_args.kwargs
    assert kwargs["provider_instance"] == INSTANCE
    assert kwargs["limit"] == 0
    assert set(kwargs["provider_item_ids"]) == {
        str(book_audiobook["id"]),
        str(book_multifile["id"]),
    }


@pytest.mark.asyncio
async def test_resolve_image_fetches_bookorbit_covers(provider: BookOrbitProvider) -> None:
    """BookOrbit cover urls are fetched through the authenticated client."""
    client = cast("Mock", provider._client)
    client.get_cover = AsyncMock(return_value=b"img")
    cover = "http://bo/api/v1/books/42/cover?t=1"
    assert await provider.resolve_image(cover) == b"img"
    client.get_cover.assert_awaited_once_with(cover)
    assert await provider.resolve_image("https://elsewhere/x.png") == "https://elsewhere/x.png"
    client.get_cover = AsyncMock(side_effect=ClientResponseError(Mock(), (), status=500))
    assert await provider.resolve_image(cover) is None
