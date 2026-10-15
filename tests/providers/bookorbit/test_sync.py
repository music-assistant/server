"""Tests for BookOrbit provider sync, library filtering and lookups."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
from music_assistant_models.enums import ImageType, MediaType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    Artist,
    Audiobook,
    MediaItemImage,
    ProviderMapping,
    UniqueList,
)

from music_assistant.controllers.music.media.base import AudiobookSyncDetails
from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.bookorbit import SUPPORTED_FEATURES, BookOrbitProvider
from music_assistant.providers.bookorbit.helpers import library_may_contain_audio


@pytest.fixture
def provider(book_audiobook: dict[str, Any], book_multifile: dict[str, Any]) -> BookOrbitProvider:
    """Create a provider with a mocked client."""
    mass = Mock()
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
        1: [{"id": book_audiobook["id"]}],
        2: [],
        3: [{"id": book_multifile["id"]}, {"id": 999}],
    }
    prov._client.query_audiobooks = AsyncMock(side_effect=lambda library_id: books[library_id])
    details = {
        book_audiobook["id"]: book_audiobook,
        book_multifile["id"]: book_multifile,
        999: {**book_audiobook, "id": 999, "files": [{"id": 1, "format": "epub"}]},
    }
    prov._client.get_book = AsyncMock(side_effect=lambda book_id: details[book_id])
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
    """All libraries are queried; allowedFormats is not used to skip libraries."""
    assert library_may_contain_audio({"allowedFormats": ["M4B"]})
    assert library_may_contain_audio({"allowedFormats": []})
    assert library_may_contain_audio({})
    assert library_may_contain_audio({"allowedFormats": ["epub", "pdf"]})


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
    """Metadata edits bump updatedAt; that must trigger a library update."""
    mapping_old = ProviderMapping(
        item_id="1",
        provider_domain="bookorbit",
        provider_instance=provider.instance_id,
        details="2026-01-01T00:00:00.000Z",
        in_library=True,
    )
    mapping_new = ProviderMapping(
        item_id="1",
        provider_domain="bookorbit",
        provider_instance=provider.instance_id,
        details="2026-10-14T20:00:00.000Z",
        in_library=True,
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
    with patch.object(BookOrbitProvider, "_check_provider_mappings", return_value=True):
        assert provider._library_item_needs_update(library, prov_item) is True
    mapping_same = ProviderMapping(
        item_id="1",
        provider_domain="bookorbit",
        provider_instance=provider.instance_id,
        details="2026-01-01T00:00:00.000Z",
        in_library=True,
    )
    prov_same = Audiobook(
        item_id="1",
        provider=provider.instance_id,
        name="Hatchet",
        provider_mappings={mapping_same},
        duration=100,
    )
    with patch.object(BookOrbitProvider, "_check_provider_mappings", return_value=True):
        assert provider._library_item_needs_update(library, prov_same) is False


def test_library_item_needs_update_when_title_diverges(provider: BookOrbitProvider) -> None:
    """A stale library title must refresh even if updatedAt was already stored."""
    mapping = ProviderMapping(
        item_id="1",
        provider_domain="bookorbit",
        provider_instance=provider.instance_id,
        details="2026-10-14T20:00:00.000Z",
        in_library=True,
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
    with patch.object(BookOrbitProvider, "_check_provider_mappings", return_value=True):
        assert provider._library_item_needs_update(library, prov_item) is True


def test_library_item_needs_update_when_sole_provider_images_change(
    provider: BookOrbitProvider,
) -> None:
    """Stale book-cover stand-ins must refresh when the provider clears artist images."""
    mapping = ProviderMapping(
        item_id="author:11",
        provider_domain="bookorbit",
        provider_instance=provider.instance_id,
        details="",
        in_library=True,
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
    with patch.object(BookOrbitProvider, "_check_provider_mappings", return_value=True):
        assert provider._library_item_needs_update(library, prov_item) is True


def test_is_sole_library_provider(provider: BookOrbitProvider) -> None:
    """Merged library items must keep merge semantics; sole BookOrbit items overwrite."""
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
