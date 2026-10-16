"""Tests for BookOrbit provider sync, library filtering and lookups."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
from aiohttp import ClientResponseError
from music_assistant_models.enums import ImageType, MediaType
from music_assistant_models.errors import MediaNotFoundError, ProviderUnavailableError
from music_assistant_models.media_items import (
    Artist,
    Audiobook,
    MediaItemImage,
    ProviderMapping,
    UniqueList,
)
from yarl import URL

from music_assistant.controllers.music.media.base import AudiobookSyncDetails
from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.bookorbit import SUPPORTED_FEATURES, BookOrbitProvider
from music_assistant.providers.bookorbit.constants import (
    CONF_PASSWORD,
    CONF_URL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
)
from music_assistant.providers.bookorbit.helpers import library_may_contain_audio
from music_assistant.providers.bookorbit.parsers import AUTHOR_PREFIX
from tests.providers.bookorbit.conftest import manifest_from_book


@pytest.fixture
def provider(book_audiobook: dict[str, Any], book_multifile: dict[str, Any]) -> BookOrbitProvider:
    """Create a provider with a mocked client."""
    mass = Mock()
    mass.cache.get = AsyncMock(return_value=None)
    mass.cache.set = AsyncMock()
    mass.music.audiobooks.get_library_items_by_prov_id = AsyncMock(return_value=[])
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
        3: [
            {"id": book_multifile["id"], "updatedAt": book_multifile.get("updatedAt")},
            {"id": 999},
        ],
    }
    prov._client.query_audiobooks = AsyncMock(side_effect=lambda library_id: books[library_id])
    details = {
        book_audiobook["id"]: book_audiobook,
        book_multifile["id"]: book_multifile,
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
    """Ebook-only libraries and books without audio files are skipped."""
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
async def test_refresh_catalog_aborts_on_detail_fetch_error(
    provider: BookOrbitProvider, book_audiobook: dict[str, Any], book_multifile: dict[str, Any]
) -> None:
    """A failed detail fetch must raise and leave the previous catalog untouched."""
    previous = {str(book_audiobook["id"]): book_audiobook}
    provider._catalog = previous
    provider._catalog_refreshed_at = 0.0

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
    with pytest.raises(ProviderUnavailableError, match="incomplete"):
        await provider._refresh_catalog(force=True)
    assert provider._catalog is previous


@pytest.mark.asyncio
async def test_refresh_catalog_aborts_when_manifest_unavailable(
    provider: BookOrbitProvider, book_audiobook: dict[str, Any], book_multifile: dict[str, Any]
) -> None:
    """Transient manifest errors must not cache unordered multipart BookDetail."""
    previous = {str(book_audiobook["id"]): book_audiobook}
    provider._catalog = previous
    provider._catalog_refreshed_at = 0.0
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
    with pytest.raises(ProviderUnavailableError, match="incomplete"):
        await provider._refresh_catalog(force=True)
    assert provider._catalog is previous
    cached_ids = {call.kwargs.get("key") for call in cache_set.await_args_list}
    assert str(book_multifile["id"]) not in cached_ids


@pytest.mark.asyncio
async def test_refresh_catalog_cold_start_detail_error_raises(
    provider: BookOrbitProvider, book_audiobook: dict[str, Any], book_multifile: dict[str, Any]
) -> None:
    """Cold start must not publish an empty catalog after a detail failure."""
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
    with pytest.raises(ProviderUnavailableError, match="incomplete"):
        await provider._refresh_catalog(force=True)
    assert provider._catalog == {}


@pytest.mark.asyncio
async def test_fetch_catalog_cancels_pending_detail_fetches_on_error(
    provider: BookOrbitProvider, book_audiobook: dict[str, Any], book_multifile: dict[str, Any]
) -> None:
    """A failed detail fetch must cancel siblings before releasing the catalog lock."""
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
            raise TimeoutError("detail timeout")
        if book_id == 999:
            return {"id": 999, "files": [{"id": 1, "format": "epub"}]}
        raise KeyError(book_id)

    cast("Any", provider._client).get_book = AsyncMock(side_effect=_get_book)
    with pytest.raises(TimeoutError, match="detail timeout"):
        await provider._refresh_catalog(force=True)
    assert cancelled.is_set()
    assert provider._catalog == {}


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
    with pytest.raises(ClientResponseError):
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


def test_library_item_needs_update_when_updated_at_changes(provider: BookOrbitProvider) -> None:
    """Metadata edits bump updatedAt; that must trigger a library update via mapping details."""
    mapping_old = ProviderMapping(
        item_id="1",
        provider_domain="bookorbit",
        provider_instance=provider.instance_id,
        details="2026-01-01T00:00:00.000Z",
        in_library=True,
        is_unique=True,
    )
    mapping_new = ProviderMapping(
        item_id="1",
        provider_domain="bookorbit",
        provider_instance=provider.instance_id,
        details="2026-10-14T20:00:00.000Z",
        in_library=True,
        is_unique=True,
    )
    library = AudiobookSyncDetails(
        item_id=1,
        date_added=datetime(2026, 1, 1, tzinfo=UTC),
        provider_mappings={mapping_old},
        artist_links=frozenset(),
        authors=(),
        narrators=(),
        fully_played=None,
        resume_position_ms=None,
    )
    prov_item = Audiobook(
        item_id="1",
        provider=provider.instance_id,
        name="Hatchet",
        provider_mappings={mapping_new},
        duration=100,
    )
    assert provider._library_item_needs_update(library, prov_item) is True
    mapping_same = ProviderMapping(
        item_id="1",
        provider_domain="bookorbit",
        provider_instance=provider.instance_id,
        details="2026-01-01T00:00:00.000Z",
        in_library=True,
        is_unique=True,
    )
    prov_same = Audiobook(
        item_id="1",
        provider=provider.instance_id,
        name="Hatchet",
        provider_mappings={mapping_same},
        duration=100,
    )
    assert provider._library_item_needs_update(library, prov_same) is False


def test_library_item_needs_update_when_title_diverges(provider: BookOrbitProvider) -> None:
    """A stale library title must refresh even if updatedAt was already stored."""
    mapping = ProviderMapping(
        item_id="1",
        provider_domain="bookorbit",
        provider_instance=provider.instance_id,
        details="2026-10-14T20:00:00.000Z",
        in_library=True,
        is_unique=True,
    )
    library = Audiobook(
        item_id="1",
        provider=provider.instance_id,
        name="Hatchet",
        provider_mappings={mapping},
        duration=100,
    )
    prov_item = Audiobook(
        item_id="1",
        provider=provider.instance_id,
        name="Hatchet | Survival",
        provider_mappings={mapping},
        duration=100,
    )
    assert provider._library_item_needs_update(library, prov_item) is True


def test_library_item_needs_update_when_sole_provider_images_change(
    provider: BookOrbitProvider,
) -> None:
    """Stale book-cover stand-ins must refresh when the provider clears artist images."""
    mapping = ProviderMapping(
        item_id="author:11",
        provider_domain="bookorbit",
        provider_instance=provider.instance_id,
        details="Frank Herbert|",
        in_library=True,
        is_unique=True,
    )
    library = Artist(
        item_id="author:11",
        provider=provider.instance_id,
        name="Frank Herbert",
        provider_mappings={mapping},
    )
    library.metadata.images = UniqueList(
        [
            MediaItemImage(
                type=ImageType.THUMB,
                path="http://bo/api/v1/books/42/cover",
                provider=provider.instance_id,
            )
        ]
    )
    prov_item = Artist(
        item_id="author:11",
        provider=provider.instance_id,
        name="Frank Herbert",
        provider_mappings={mapping},
    )
    prov_item.metadata.images = UniqueList([])
    assert provider._library_item_needs_update(library, prov_item) is True


def test_library_sync_overwrite_sole_provider(provider: BookOrbitProvider) -> None:
    """Merged library items keep merge semantics; sole BookOrbit items overwrite."""
    sole = Audiobook(
        item_id="1",
        provider=provider.instance_id,
        name="Hatchet",
        provider_mappings={
            ProviderMapping(
                item_id="1",
                provider_domain="bookorbit",
                provider_instance=provider.instance_id,
                in_library=True,
            )
        },
        duration=100,
    )
    assert provider._is_sole_library_provider(sole) is True
    assert provider._library_sync_overwrite(sole, sole) is True

    merged = Audiobook(
        item_id="1",
        provider=provider.instance_id,
        name="Hatchet",
        provider_mappings={
            ProviderMapping(
                item_id="1",
                provider_domain="bookorbit",
                provider_instance=provider.instance_id,
                in_library=True,
            ),
            ProviderMapping(
                item_id="other",
                provider_domain="filesystem_local",
                provider_instance="filesystem_local--x",
                in_library=True,
            ),
        },
        duration=100,
    )
    assert provider._is_sole_library_provider(merged) is False
    assert provider._library_sync_overwrite(merged, sole) is False


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
    # Cached book skipped; ebook-only id 999 and multifile still fetched.
    assert get_book.await_count == 2
    assert {c.args[0] for c in get_book.await_args_list} == {
        book_multifile["id"],
        999,
    }
    assert cache.set.await_count == 2


@pytest.mark.asyncio
async def test_sync_library_forces_catalog_refresh(provider: BookOrbitProvider) -> None:
    """An audiobook library sync always re-fetches BookOrbit, ignoring the TTL."""
    provider._catalog_refreshed_at = time.monotonic()
    provider._catalog = {"1": {"id": 1}}

    async def _parent_sync(self: MusicProvider, media_type: MediaType) -> None:
        assert media_type == MediaType.AUDIOBOOK
        async for _ in self.get_library_audiobooks():
            break

    with (
        patch.object(provider, "_fetch_catalog", new_callable=AsyncMock) as fetch,
        patch.object(MusicProvider, "sync_library", _parent_sync),
    ):
        await provider.sync_library(MediaType.AUDIOBOOK)
    fetch.assert_awaited_once()


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
