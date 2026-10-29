"""Tests for BookOrbit provider sync, library filtering and lookups."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
from aiohttp import ClientResponseError
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError, ProviderUnavailableError
from music_assistant_models.media_items import Audiobook, ProviderMapping
from yarl import URL

from music_assistant.constants import (
    CONF_ENTRY_LIBRARY_SYNC_DELETIONS,
    CONF_PASSWORD,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
)
from music_assistant.controllers.cache import BYPASS_CACHE
from music_assistant.controllers.music.media.base import AudiobookSyncDetails
from music_assistant.models.music_provider import SYNC_RUN_STATE, SyncRunState
from music_assistant.providers.bookorbit import SUPPORTED_FEATURES, BookOrbitProvider
from music_assistant.providers.bookorbit.constants import (
    CACHE_CATEGORY_LIBRARY_FINGERPRINTS,
    CACHE_KEY_LIBRARY_FINGERPRINTS,
    CONF_URL,
)
from music_assistant.providers.bookorbit.helpers import library_may_contain_audio
from music_assistant.providers.bookorbit.parsers import AUTHOR_PREFIX, parse_audiobook
from tests.providers.bookorbit.conftest import manifest_from_book, wire_mass_create_task


@pytest.fixture
def provider(book_audiobook: dict[str, Any], book_multifile: dict[str, Any]) -> BookOrbitProvider:
    """Create a provider with a mocked client."""
    mass = Mock()
    wire_mass_create_task(mass)
    mass.cache.get = AsyncMock(return_value=None)
    mass.cache.set = AsyncMock()
    mass.music.audiobooks.get_library_items_by_prov_id = AsyncMock(return_value=[])
    mass.music.get_playlog_provider_item_ids = AsyncMock(return_value=[])
    manifest = Mock()
    manifest.domain = "bookorbit"
    config = Mock()
    config.instance_id = "bookorbit--test"
    config.name = "BookOrbit Test"
    config.enabled = True
    config.get_value.side_effect = lambda key, default=None: {"log_level": "GLOBAL"}.get(
        key, default
    )
    prov = BookOrbitProvider(mass, manifest, config, SUPPORTED_FEATURES)
    prov.base_url = "http://bo"
    prov._catalog = {}
    prov._client = Mock()
    prov._client.list_libraries = AsyncMock(
        return_value=[
            {"id": 1, "name": "Audio", "allowedFormats": ["m4b", "epub"]},
            {"id": 2, "name": "Ebooks", "allowedFormats": ["epub", "pdf"]},
            {"id": 3, "name": "Any", "allowedFormats": []},
        ]
    )
    books = {
        1: [{"id": book_audiobook["id"], "updatedAt": book_audiobook.get("updatedAt")}],
        2: [],
        3: [{"id": book_multifile["id"], "updatedAt": book_multifile.get("updatedAt")}],
    }
    prov._client.query_audiobooks = AsyncMock(side_effect=lambda library_id: books[library_id])
    details = {
        book_audiobook["id"]: book_audiobook,
        book_multifile["id"]: book_multifile,
        # Direct lookup target for get_audiobook; not returned by query_audiobooks.
        999: {**book_audiobook, "id": 999, "files": [{"id": 1, "format": "epub"}]},
    }
    prov._client.get_book = AsyncMock(side_effect=lambda book_id: details[book_id])
    manifests = {
        book_audiobook["id"]: manifest_from_book(book_audiobook),
        book_multifile["id"]: manifest_from_book(book_multifile),
    }
    prov._client.get_audiobook_manifest = AsyncMock(
        side_effect=lambda book_id: manifests.get(book_id, {"assets": []})
    )
    prov._client.list_authors = AsyncMock(
        return_value=[
            {
                "id": book_audiobook["authors"][0]["id"],
                "name": book_audiobook["authors"][0]["name"],
                "imageUrl": "/api/v1/authors/11/thumbnail?t=1",
                "coverBookId": book_audiobook["id"],
            }
        ]
    )
    prov._client.get_continue_listening_book_ids = AsyncMock(return_value=[])
    return prov


def test_library_filter() -> None:
    """Book libraries are queried; allowedFormats is ignored; podcasts are skipped."""
    assert library_may_contain_audio({"allowedFormats": ["M4B"]})
    assert library_may_contain_audio({"allowedFormats": []})
    assert library_may_contain_audio({})
    assert library_may_contain_audio({"allowedFormats": ["epub", "pdf"]})
    assert library_may_contain_audio({"type": "books", "allowedFormats": ["epub"]})
    assert not library_may_contain_audio({"type": "podcasts", "allowedFormats": ["mp3"]})


@pytest.mark.asyncio
async def test_refresh_catalog_skips_podcast_libraries(
    provider: BookOrbitProvider, book_audiobook: dict[str, Any]
) -> None:
    """Podcast libraries must not be queried; they reject /books with HTTP 400."""
    cast("Any", provider._client).list_libraries = AsyncMock(
        return_value=[
            {"id": 1, "name": "Audio", "type": "books", "allowedFormats": ["m4b"]},
            {"id": 9, "name": "Shows", "type": "podcasts", "allowedFormats": ["mp3"]},
            {"id": 2, "name": "Ebooks", "allowedFormats": ["epub"]},
        ]
    )

    async def _query(library_id: int) -> list[dict[str, Any]]:
        if library_id == 9:
            raise ClientResponseError(
                request_info=Mock(real_url=URL("http://bo/libraries/9/books")),
                history=(),
                status=400,
                message="wrong library type",
            )
        if library_id == 1:
            return [{"id": book_audiobook["id"], "updatedAt": book_audiobook.get("updatedAt")}]
        return []

    cast("Any", provider._client).query_audiobooks = AsyncMock(side_effect=_query)
    await provider._refresh_catalog(force=True)
    queried = [
        c.kwargs["library_id"]
        for c in cast("Mock", provider._client.query_audiobooks).call_args_list
    ]
    assert queried == [1, 2]
    assert str(book_audiobook["id"]) in provider._catalog


@pytest.mark.asyncio
async def test_refresh_catalog_filters_libraries_and_ebooks(
    provider: BookOrbitProvider, book_audiobook: dict[str, Any], book_multifile: dict[str, Any]
) -> None:
    """All book libraries are queried; podcast libraries are excluded elsewhere."""
    await provider._refresh_catalog()
    assert set(provider._catalog) == {str(book_audiobook["id"]), str(book_multifile["id"])}
    query_mock = cast("Mock", provider._client.query_audiobooks)
    queried = [c.kwargs["library_id"] for c in query_mock.call_args_list]
    assert queried == [1, 2, 3]

    audiobooks = [book async for book in provider.get_library_audiobooks()]
    assert {b.item_id for b in audiobooks} == set(provider._catalog)
    artists = {a.item_id: a async for a in provider.get_library_artists()}
    author_id = f"author:{book_audiobook['authors'][0]['id']}"
    assert author_id in artists
    assert all(a.startswith(("author:", "narrator:")) for a in artists)
    author_images = artists[author_id].metadata.images
    assert author_images is not None
    (author_image,) = author_images
    assert author_image.path.endswith("/api/v1/authors/11/thumbnail?t=1")
    narrator_id = f"narrator:{book_audiobook['audioMetadata']['narrators'][0]['id']}"
    assert not artists[narrator_id].metadata.images


@pytest.mark.asyncio
async def test_refresh_catalog_skips_detail_fetch_error(
    provider: BookOrbitProvider, book_audiobook: dict[str, Any], book_multifile: dict[str, Any]
) -> None:
    """A failed detail fetch is skipped; successful books still refresh the catalog."""

    async def _get_book(book_id: int) -> dict[str, Any]:
        if book_id == book_multifile["id"]:
            raise ClientResponseError(
                request_info=Mock(real_url=URL("http://bo/x")),
                history=(),
                status=500,
                message="boom",
            )
        if book_id == book_audiobook["id"]:
            return book_audiobook
        if book_id == 999:
            return {"id": 999, "files": [{"id": 1, "format": "epub"}]}
        raise KeyError(book_id)

    cast("Any", provider._client).get_book = AsyncMock(side_effect=_get_book)
    await provider._refresh_catalog(force=True)
    assert str(book_audiobook["id"]) in provider._catalog
    assert str(book_multifile["id"]) not in provider._catalog
    assert str(book_multifile["id"]) in provider._catalog_detail_failed_ids


@pytest.mark.asyncio
async def test_get_library_artists_marks_incomplete_when_catalog_detail_failed(
    provider: BookOrbitProvider, book_audiobook: dict[str, Any], book_multifile: dict[str, Any]
) -> None:
    """
    A reused partial catalog must mark artist sync incomplete.

    Authors/narrators come only from loaded books; without this, sync deletions can
    remove an author that still exists on a book whose detail fetch failed.
    """

    async def _get_book(book_id: int) -> dict[str, Any]:
        if book_id == book_multifile["id"]:
            raise ClientResponseError(
                request_info=Mock(real_url=URL("http://bo/x")),
                history=(),
                status=500,
                message="boom",
            )
        if book_id == book_audiobook["id"]:
            return book_audiobook
        raise KeyError(book_id)

    cast("Any", provider._client).get_book = AsyncMock(side_effect=_get_book)
    await provider._refresh_catalog(force=True)
    assert str(book_multifile["id"]) in provider._catalog_detail_failed_ids

    # Artist sync reuses the catalog within the TTL (force_catalog_refresh is False).
    provider._force_catalog_refresh = False
    token = SYNC_RUN_STATE.set(SyncRunState())
    try:
        artists = [artist async for artist in provider.get_library_artists()]
        state = SYNC_RUN_STATE.get()
        assert state is not None
        assert MediaType.ARTIST in state.incomplete_media_types
    finally:
        SYNC_RUN_STATE.reset(token)

    author_id = f"{AUTHOR_PREFIX}{book_audiobook['authors'][0]['id']}"
    assert author_id in {artist.item_id for artist in artists}


@pytest.mark.asyncio
async def test_get_library_audiobooks_reports_failed_ids_when_catalog_reused(
    provider: BookOrbitProvider, book_audiobook: dict[str, Any], book_multifile: dict[str, Any]
) -> None:
    """A reused partial catalog must re-report failed audiobook ids during sync."""

    async def _get_book(book_id: int) -> dict[str, Any]:
        if book_id == book_multifile["id"]:
            raise ClientResponseError(
                request_info=Mock(real_url=URL("http://bo/x")),
                history=(),
                status=500,
                message="boom",
            )
        if book_id == book_audiobook["id"]:
            return book_audiobook
        raise KeyError(book_id)

    cast("Any", provider._client).get_book = AsyncMock(side_effect=_get_book)
    await provider._refresh_catalog(force=True)
    failed_id = str(book_multifile["id"])
    assert failed_id in provider._catalog_detail_failed_ids

    provider._force_catalog_refresh = False
    token = SYNC_RUN_STATE.set(SyncRunState())
    try:
        books = [book async for book in provider.get_library_audiobooks()]
        state = SYNC_RUN_STATE.get()
        assert state is not None
        assert failed_id in state.skipped_item_ids.get(MediaType.AUDIOBOOK, set())
    finally:
        SYNC_RUN_STATE.reset(token)

    assert str(book_audiobook["id"]) in {book.item_id for book in books}


@pytest.mark.asyncio
async def test_refresh_catalog_skips_when_manifest_unavailable(
    provider: BookOrbitProvider, book_audiobook: dict[str, Any], book_multifile: dict[str, Any]
) -> None:
    """Transient manifest errors skip that book and do not cache unordered detail."""
    cache_set = cast("AsyncMock", provider.mass.cache.set)
    cache_set.reset_mock()

    async def _manifest(book_id: int) -> dict[str, Any]:
        if book_id == book_multifile["id"]:
            raise ClientResponseError(
                request_info=Mock(real_url=URL("http://bo/manifest")),
                history=(),
                status=503,
                message="unavailable",
            )
        return manifest_from_book(book_audiobook)

    cast("Any", provider._client).get_audiobook_manifest = AsyncMock(side_effect=_manifest)
    await provider._refresh_catalog(force=True)
    assert str(book_audiobook["id"]) in provider._catalog
    assert str(book_multifile["id"]) not in provider._catalog
    cached_ids = {call.kwargs.get("key") for call in cache_set.await_args_list}
    assert str(book_multifile["id"]) not in cached_ids


@pytest.mark.asyncio
async def test_refresh_catalog_cold_start_keeps_successful_books(
    provider: BookOrbitProvider, book_audiobook: dict[str, Any], book_multifile: dict[str, Any]
) -> None:
    """Cold start publishes successful books when a sibling detail fetch fails."""
    assert provider._catalog == {}

    async def _get_book(book_id: int) -> dict[str, Any]:
        if book_id == book_multifile["id"]:
            raise ClientResponseError(
                request_info=Mock(real_url=URL("http://bo/x")),
                history=(),
                status=503,
                message="unavailable",
            )
        if book_id == book_audiobook["id"]:
            return book_audiobook
        if book_id == 999:
            return {"id": 999, "files": [{"id": 1, "format": "epub"}]}
        raise KeyError(book_id)

    cast("Any", provider._client).get_book = AsyncMock(side_effect=_get_book)
    await provider._refresh_catalog(force=True)
    assert str(book_audiobook["id"]) in provider._catalog
    assert str(book_multifile["id"]) not in provider._catalog


@pytest.mark.asyncio
async def test_fetch_catalog_continues_sibling_detail_fetches_on_timeout(
    provider: BookOrbitProvider, book_audiobook: dict[str, Any], book_multifile: dict[str, Any]
) -> None:
    """A timed-out detail fetch is skipped without cancelling successful siblings."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def _get_book(book_id: int) -> dict[str, Any]:
        if book_id == book_audiobook["id"]:
            started.set()
            await release.wait()
            return book_audiobook
        if book_id == book_multifile["id"]:
            await started.wait()
            raise TimeoutError("detail timeout")
        if book_id == 999:
            return {"id": 999, "files": [{"id": 1, "format": "epub"}]}
        raise KeyError(book_id)

    cast("Any", provider._client).get_book = AsyncMock(side_effect=_get_book)
    refresh = asyncio.create_task(provider._refresh_catalog(force=True))
    await started.wait()
    # Give the failing sibling time to timeout and be recorded as skipped.
    await asyncio.sleep(0)
    release.set()
    await refresh
    assert str(book_audiobook["id"]) in provider._catalog
    assert str(book_multifile["id"]) not in provider._catalog


@pytest.mark.asyncio
async def test_refresh_catalog_skips_when_manifest_unmappable(
    provider: BookOrbitProvider, book_audiobook: dict[str, Any], book_multifile: dict[str, Any]
) -> None:
    """Multipart books with no mappable manifest assets are skipped, not cached."""
    cache_set = cast("AsyncMock", provider.mass.cache.set)
    cache_set.reset_mock()

    async def _manifest(book_id: int) -> dict[str, Any]:
        if book_id == book_multifile["id"]:
            return {"assets": [{"assetId": "a1", "fileId": 99999, "sequence": 0}]}
        return manifest_from_book(book_audiobook)

    cast("Any", provider._client).get_audiobook_manifest = AsyncMock(side_effect=_manifest)
    await provider._refresh_catalog(force=True)
    assert str(book_audiobook["id"]) in provider._catalog
    assert str(book_multifile["id"]) not in provider._catalog
    cached_ids = {call.kwargs.get("key") for call in cache_set.await_args_list}
    assert str(book_multifile["id"]) not in cached_ids


@pytest.mark.asyncio
async def test_refresh_catalog_skips_when_detail_has_no_audio(
    provider: BookOrbitProvider, book_audiobook: dict[str, Any], book_multifile: dict[str, Any]
) -> None:
    """
    Audio-listed books whose detail has no audio are skipped.

    During sync, skipped ids are recorded so deletion does not remove them while
    the rest of the catalog still refreshes.
    """
    sole_author_book = {
        **book_multifile,
        "authors": [{"id": 9001, "name": "Sole Author"}],
        "audioMetadata": {
            **book_multifile.get("audioMetadata", {}),
            "narrators": [],
        },
    }

    skip = Mock()
    provider.report_skipped_sync_item = skip  # type: ignore[method-assign]

    async def _get_book(book_id: int) -> dict[str, Any]:
        if book_id == book_multifile["id"]:
            return {**sole_author_book, "files": [{"id": 1, "format": "epub"}]}
        if book_id == book_audiobook["id"]:
            return book_audiobook
        raise KeyError(book_id)

    cast("Any", provider._client).get_book = AsyncMock(side_effect=_get_book)
    token = SYNC_RUN_STATE.set(SyncRunState())
    try:
        await provider._refresh_catalog(force=True)
    finally:
        SYNC_RUN_STATE.reset(token)

    assert str(book_audiobook["id"]) in provider._catalog
    assert str(book_multifile["id"]) not in provider._catalog
    skipped_ids = {call.args[1] for call in skip.call_args_list}
    assert str(book_multifile["id"]) in skipped_ids
    assert all(call.args[0] is MediaType.AUDIOBOOK for call in skip.call_args_list)
    assert any(isinstance(call.args[2], ValueError) for call in skip.call_args_list)


@pytest.mark.asyncio
async def test_unload_cancels_in_flight_detail_fetches(
    provider: BookOrbitProvider, book_audiobook: dict[str, Any], book_multifile: dict[str, Any]
) -> None:
    """Unload cancels pending catalog detail tasks before closing the client."""
    started = asyncio.Event()
    release = asyncio.Event()
    cancelled = asyncio.Event()

    async def _get_book(book_id: int) -> dict[str, Any]:
        if book_id == book_audiobook["id"]:
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return book_audiobook
        if book_id == book_multifile["id"]:
            await started.wait()
            await asyncio.sleep(3600)
            return book_multifile
        if book_id == 999:
            return {"id": 999, "files": [{"id": 1, "format": "epub"}]}
        raise KeyError(book_id)

    cast("Any", provider._client).get_book = AsyncMock(side_effect=_get_book)
    cast("Any", provider._client).close = AsyncMock()
    refresh = asyncio.create_task(provider._refresh_catalog(force=True))
    await started.wait()
    await provider.unload()
    with pytest.raises((asyncio.CancelledError, TimeoutError, ProviderUnavailableError)):
        await refresh
    assert cancelled.is_set()
    cast("AsyncMock", provider._client.close).assert_awaited()


@pytest.mark.asyncio
async def test_refresh_catalog_author_list_failure_preserves_author_meta(
    provider: BookOrbitProvider,
) -> None:
    """A failed author-list fetch must not clear stored author artwork metadata."""
    await provider._refresh_catalog()
    previous_meta = provider._author_meta
    assert previous_meta
    cast("Any", provider._client).list_authors = AsyncMock(
        side_effect=ClientResponseError(
            request_info=Mock(real_url=URL("http://bo/authors")),
            history=(),
            status=500,
            message="authors down",
        )
    )
    provider._catalog_refreshed_at = 0.0
    with pytest.raises(ProviderUnavailableError):
        await provider._refresh_catalog(force=True)
    assert provider._author_meta is previous_meta


@pytest.mark.asyncio
async def test_search_artists_matches_name_only_results(
    provider: BookOrbitProvider, book_audiobook: dict[str, Any]
) -> None:
    """Catalog author search returns name-only rows that must match known artists."""
    await provider._refresh_catalog()
    author_name = book_audiobook["authors"][0]["name"]
    client = cast("Any", provider._client)
    client.search_authors = AsyncMock(return_value=[{"name": author_name}])
    client.search_narrators = AsyncMock(return_value=[])
    found = await provider._search_artists(author_name, limit=5)
    assert len(found) == 1
    assert found[0].item_id == f"{AUTHOR_PREFIX}{book_audiobook['authors'][0]['id']}"


@pytest.mark.asyncio
async def test_enrich_author_overlays_renamed_author_from_list(
    provider: BookOrbitProvider, book_audiobook: dict[str, Any]
) -> None:
    """Forced sync must apply /authors renames even when the book detail is cached."""
    author_id = book_audiobook["authors"][0]["id"]
    stale_book = {
        **book_audiobook,
        "authors": [{"id": author_id, "name": "Old Name", "sortName": "Name, Old"}],
    }

    async def _cache_get(key: str, **_kwargs: Any) -> dict[str, Any] | None:
        return stale_book if key == str(book_audiobook["id"]) else None

    cast("Any", provider.mass.cache).get = AsyncMock(side_effect=_cache_get)
    cast("Any", provider._client).list_authors = AsyncMock(
        return_value=[
            {
                "id": author_id,
                "name": "New Name",
                "sortName": "Name, New",
                "imageUrl": "/api/v1/authors/11/thumbnail?t=2",
            }
        ]
    )
    await provider._refresh_catalog(force=True)
    artists = {a.item_id: a async for a in provider.get_library_artists()}
    artist = artists[f"{AUTHOR_PREFIX}{author_id}"]
    assert artist.name == "New Name"
    assert artist.sort_name == "Name, New"


@pytest.mark.asyncio
async def test_get_audiobook_and_artist(
    provider: BookOrbitProvider, book_audiobook: dict[str, Any]
) -> None:
    """Single item lookups."""
    book = await provider.get_audiobook(str(book_audiobook["id"]))
    assert book.item_id == str(book_audiobook["id"])
    with pytest.raises(MediaNotFoundError):
        await provider.get_audiobook("999")  # no audio files
    author_id = f"author:{book_audiobook['authors'][0]['id']}"
    artist = await provider.get_artist(author_id)  # triggers a catalog refresh
    assert artist.item_id == author_id
    with pytest.raises(MediaNotFoundError):
        await provider.get_artist("author:123456")
    with pytest.raises(MediaNotFoundError):
        await provider.get_artist("bogus")


@pytest.mark.asyncio
async def test_get_artist_refreshes_edited_author_when_bypass_cache(
    provider: BookOrbitProvider, book_audiobook: dict[str, Any]
) -> None:
    """refresh_item (BYPASS_CACHE) must not return TTL-stale author metadata from `_catalog`."""
    author_numeric_id = int(book_audiobook["authors"][0]["id"])
    author_id = f"{AUTHOR_PREFIX}{author_numeric_id}"
    await provider._refresh_catalog(force=True)
    assert author_id in provider._catalog_artists()

    # Stale in-memory catalog/author meta after an edit on the server.
    for book in provider._catalog.values():
        for author in book.get("authors") or []:
            if author.get("id") == author_numeric_id:
                author["name"] = "Old Name"
                author["description"] = "Old bio"
    provider._author_meta[author_numeric_id] = {
        "id": author_numeric_id,
        "name": "Old Name",
        "description": "Old bio",
    }
    cast("Any", provider._client).list_authors = AsyncMock(
        return_value=[
            {
                "id": author_numeric_id,
                "name": "New Name",
                "description": "New bio",
                "imageUrl": f"/api/v1/authors/{author_numeric_id}/thumbnail?t=2",
            }
        ]
    )

    # Without BYPASS_CACHE, reuse the TTL-fresh catalog (metadata scan / browse path).
    stale = await provider.get_artist(author_id)
    assert stale.name == "Old Name"

    token = BYPASS_CACHE.set(True)
    try:
        artist = await provider.get_artist(author_id)
    finally:
        BYPASS_CACHE.reset(token)
    assert artist.name == "New Name"


@pytest.mark.asyncio
async def test_sync_library_reuses_catalog_within_ttl(provider: BookOrbitProvider) -> None:
    """Forced audiobook listing then artist listing does not refresh twice within TTL."""
    get_book = cast("Mock", provider._client.get_book)
    provider._force_catalog_refresh = True
    async for _ in provider.get_library_audiobooks():
        pass
    calls_after_audiobook = get_book.call_count
    provider._force_catalog_refresh = False
    async for _ in provider.get_library_artists():
        pass
    assert calls_after_audiobook > 0
    assert get_book.call_count == calls_after_audiobook


def _sync_details_without_mapping_details(provider: BookOrbitProvider) -> AudiobookSyncDetails:
    """Build sync-details like core does: provider mappings without ``details``."""
    return AudiobookSyncDetails(
        item_id=1,
        date_added=datetime(2026, 1, 1, tzinfo=UTC),
        provider_mappings={
            ProviderMapping(
                item_id="1",
                provider_domain="bookorbit",
                provider_instance=provider.instance_id,
                in_library=True,
                is_unique=True,
            )
        },
        artist_links=frozenset(),
        authors=(),
        narrators=(),
        fully_played=None,
        resume_position_ms=None,
    )


def test_library_item_needs_update_uses_applied_fingerprint_cache(
    provider: BookOrbitProvider,
) -> None:
    """When sync-details omit details, compare against last-applied mass.cache fingerprints."""
    library = _sync_details_without_mapping_details(provider)
    prov_item = Audiobook(
        item_id="1",
        provider=provider.instance_id,
        name="Hatchet",
        provider_mappings={
            ProviderMapping(
                item_id="1",
                provider_domain="bookorbit",
                provider_instance=provider.instance_id,
                details="2026-10-14T20:00:00.000Z",
                in_library=True,
                is_unique=True,
            )
        },
        duration=100,
    )
    # Cold start: no applied fingerprint => force refresh once.
    assert provider._library_item_needs_update(library, prov_item) is True
    provider._applied_mapping_details = {"1": "2026-10-14T20:00:00.000Z"}
    assert provider._library_item_needs_update(library, prov_item) is False
    provider._applied_mapping_details = {"1": "2026-01-01T00:00:00.000Z"}
    assert provider._library_item_needs_update(library, prov_item) is True


@pytest.mark.asyncio
async def test_refresh_catalog_uses_cache_and_concurrency(
    provider: BookOrbitProvider, book_audiobook: dict[str, Any], book_multifile: dict[str, Any]
) -> None:
    """Catalog refresh reuses mass.cache hits and still fetches cache misses."""
    cache = cast("Mock", provider.mass.cache)

    async def _cache_get(key: str, **_kwargs: Any) -> dict[str, Any] | None:
        return book_audiobook if key == str(book_audiobook["id"]) else None

    cache.get = AsyncMock(side_effect=_cache_get)
    await provider._refresh_catalog(force=True)
    get_book = cast("Mock", provider._client.get_book)
    # Cached book skipped; only the cache-miss multifile is fetched.
    assert get_book.await_count == 1
    assert get_book.await_args_list[0].args[0] == book_multifile["id"]
    assert cache.set.await_count == 1


@pytest.mark.asyncio
async def test_sync_library_forces_catalog_refresh(provider: BookOrbitProvider) -> None:
    """An audiobook library sync always re-fetches BookOrbit, ignoring the TTL."""
    provider._catalog_refreshed_at = time.monotonic()
    provider._catalog = {"1": {"id": 1}}

    async def _run_sync(media_type: MediaType) -> None:
        assert media_type == MediaType.AUDIOBOOK
        async for _ in provider.get_library_audiobooks():
            break

    with (
        patch.object(provider, "_fetch_catalog", new_callable=AsyncMock) as fetch,
        patch.object(provider, "_run_library_sync", _run_sync),
    ):
        await provider.sync_library(MediaType.AUDIOBOOK)
    fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_sync_library_persists_applied_mapping_details(
    provider: BookOrbitProvider, book_audiobook: dict[str, Any]
) -> None:
    """Audiobook sync loads and rewrites last-applied updatedAt fingerprints."""
    book_id = str(book_audiobook["id"])
    updated_at = str(book_audiobook.get("updatedAt") or "2026-10-14T20:00:00.000Z")
    cache = cast("Mock", provider.mass.cache)
    cache.get = AsyncMock(return_value={book_id: "old-fingerprint"})

    async def _run_sync(_media_type: MediaType) -> None:
        return

    provider._catalog = {book_id: {**book_audiobook, "updatedAt": updated_at}}
    with patch.object(provider, "_run_library_sync", _run_sync):
        await provider.sync_library(MediaType.AUDIOBOOK)

    assert provider._applied_mapping_details == {book_id: updated_at}
    cache.get.assert_any_await(
        key=CACHE_KEY_LIBRARY_FINGERPRINTS,
        provider=provider.instance_id,
        category=CACHE_CATEGORY_LIBRARY_FINGERPRINTS,
        default=None,
    )
    cache.set.assert_awaited()
    set_kwargs = cache.set.await_args.kwargs
    assert set_kwargs["key"] == CACHE_KEY_LIBRARY_FINGERPRINTS
    assert set_kwargs["category"] == CACHE_CATEGORY_LIBRARY_FINGERPRINTS
    assert set_kwargs["data"] == {book_id: updated_at}
    assert set_kwargs["persistent"] is True


@pytest.mark.asyncio
async def test_commit_fingerprints_uses_listing_snapshot_not_live_catalog(
    provider: BookOrbitProvider, book_audiobook: dict[str, Any]
) -> None:
    """A catalog refresh between listing and commit must not mark newer versions applied."""
    book_id = str(book_audiobook["id"])
    listed_fp = "2026-01-01T00:00:00.000Z"
    newer_fp = "2026-12-01T00:00:00.000Z"
    provider._catalog = {book_id: {**book_audiobook, "updatedAt": listed_fp}}
    provider._catalog_refreshed_at = time.monotonic()
    provider._force_catalog_refresh = False

    token = SYNC_RUN_STATE.set(SyncRunState())
    try:
        with patch.object(provider, "_refresh_catalog", new_callable=AsyncMock):
            books = [book async for book in provider.get_library_audiobooks()]
        assert str(book_audiobook["id"]) in {book.item_id for book in books}
        assert provider._sync_catalog_fingerprints == {book_id: listed_fp}
        # Browse/search replaces the live catalog while the parent sync awaits writes.
        provider._catalog = {book_id: {**book_audiobook, "updatedAt": newer_fp}}
        provider._commit_applied_mapping_details(set())
    finally:
        SYNC_RUN_STATE.reset(token)

    assert provider._applied_mapping_details == {book_id: listed_fp}


@pytest.mark.asyncio
async def test_sync_library_keeps_fingerprint_when_library_write_fails(
    provider: BookOrbitProvider, book_audiobook: dict[str, Any]
) -> None:
    """
    A failed library write must not mark the book's fingerprint as applied.

    Uses the real parent sync path so per-item failures go through SyncRunState.
    """
    book_id = str(book_audiobook["id"])
    old_fp = "2026-01-01T00:00:00.000Z"
    new_fp = str(book_audiobook.get("updatedAt") or "2026-10-14T20:00:00.000Z")
    provider._catalog = {book_id: {**book_audiobook, "updatedAt": new_fp}}
    provider._catalog_refreshed_at = time.monotonic()
    provider._applied_mapping_details = {book_id: old_fp}

    cache = cast("Mock", provider.mass.cache)
    cache.get = AsyncMock(return_value={book_id: old_fp})
    cache.set = AsyncMock()

    mass = cast("Any", provider.mass)
    mass.music.library_supported = Mock(return_value=True)
    mass.music.genres.sync_media_item_genres = AsyncMock()
    mass.music.favorites.record_from_provider = AsyncMock()
    commit_cm = AsyncMock()
    commit_cm.__aenter__.return_value = None
    commit_cm.__aexit__.return_value = None
    mass.music.database.deferred_commit = Mock(return_value=commit_cm)
    mass.music.get_controller = Mock(
        return_value=Mock(get_library_items_by_prov_id=AsyncMock(return_value=[]))
    )

    sync_details = AudiobookSyncDetails(
        item_id=10,
        date_added=datetime(2026, 1, 1, tzinfo=UTC),
        provider_mappings={
            ProviderMapping(
                item_id=book_id,
                provider_domain="bookorbit",
                provider_instance=provider.instance_id,
                in_library=True,
                is_unique=True,
            )
        },
        artist_links=frozenset(),
        authors=(),
        narrators=(),
        fully_played=None,
        resume_position_ms=None,
    )
    mass.music.audiobooks.get_library_item_sync_details = AsyncMock(return_value=sync_details)
    mass.music.audiobooks.update_item_in_library = AsyncMock(
        side_effect=RuntimeError("library write failed")
    )

    config_values = {
        "log_level": "GLOBAL",
        CONF_ENTRY_LIBRARY_SYNC_DELETIONS.key: False,
    }
    cast("Any", provider.config).get_value.side_effect = lambda key, default=None: (
        config_values.get(key, default)
    )

    with patch.object(provider, "_refresh_catalog", new_callable=AsyncMock):

        async def _one_book() -> Any:
            yield parse_audiobook(
                provider._catalog[book_id],
                provider_instance_id=provider.instance_id,
                base_url=provider.base_url,
            )

        provider.get_library_audiobooks = _one_book  # type: ignore[method-assign]
        await provider.sync_library(MediaType.AUDIOBOOK)

    assert provider._applied_mapping_details == {book_id: old_fp}
    # Next sync still sees a fingerprint mismatch and retries the update.
    prov_item = Audiobook(
        item_id=book_id,
        provider=provider.instance_id,
        name="Hatchet",
        provider_mappings={
            ProviderMapping(
                item_id=book_id,
                provider_domain="bookorbit",
                provider_instance=provider.instance_id,
                details=new_fp,
                in_library=True,
                is_unique=True,
            )
        },
        duration=100,
    )
    assert provider._library_item_needs_update(sync_details, prov_item) is True


@pytest.mark.asyncio
async def test_handle_async_init_closes_client_on_login_timeout(
    provider: BookOrbitProvider,
) -> None:
    """Login TimeoutError must close the owned session before ProviderUnavailableError."""
    values = {
        CONF_URL: "http://bo",
        CONF_USERNAME: "u",
        CONF_PASSWORD: "p",
        CONF_VERIFY_SSL: True,
    }
    client = Mock()
    client.login = AsyncMock(side_effect=TimeoutError("total timeout"))
    client.close = AsyncMock()
    with (
        patch.object(
            provider,
            "get_setup_value",
            side_effect=lambda key, default=None: values.get(key, default),
        ),
        patch(
            "music_assistant.providers.bookorbit.create_clientsession",
            return_value=Mock(),
        ),
        patch(
            "music_assistant.providers.bookorbit.BookOrbitClient",
            return_value=client,
        ),
        pytest.raises(ProviderUnavailableError),
    ):
        await provider.handle_async_init()
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_async_init_closes_client_on_cancel(
    provider: BookOrbitProvider,
) -> None:
    """Cancelled init must close the owned session before re-raising."""
    values = {
        CONF_URL: "http://bo",
        CONF_USERNAME: "u",
        CONF_PASSWORD: "p",
        CONF_VERIFY_SSL: True,
    }
    client = Mock()
    client.login = AsyncMock(side_effect=asyncio.CancelledError())
    client.close = AsyncMock()
    with (
        patch.object(
            provider,
            "get_setup_value",
            side_effect=lambda key, default=None: values.get(key, default),
        ),
        patch(
            "music_assistant.providers.bookorbit.create_clientsession",
            return_value=Mock(),
        ),
        patch(
            "music_assistant.providers.bookorbit.BookOrbitClient",
            return_value=client,
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await provider.handle_async_init()
    client.close.assert_awaited_once()
