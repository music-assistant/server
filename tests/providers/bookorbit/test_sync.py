"""Tests for BookOrbit provider sync, library filtering and lookups."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.errors import MediaNotFoundError

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
        3: [{"id": book_multifile["id"]}, {"id": 999}],
    }
    prov._client.query_audiobooks = AsyncMock(side_effect=lambda library_id: books[library_id])
    details = {
        book_audiobook["id"]: book_audiobook,
        book_multifile["id"]: book_multifile,
        999: {**book_audiobook, "id": 999, "files": [{"id": 1, "format": "epub"}]},
    }
    prov._client.get_book = AsyncMock(side_effect=lambda book_id: details[book_id])
    return prov


def test_library_filter() -> None:
    """Libraries are kept based on allowedFormats."""
    assert library_may_contain_audio({"allowedFormats": ["M4B"]})
    assert library_may_contain_audio({"allowedFormats": []})
    assert library_may_contain_audio({})
    assert not library_may_contain_audio({"allowedFormats": ["epub", "pdf"]})


@pytest.mark.asyncio
async def test_refresh_catalog_filters_libraries_and_ebooks(
    provider: BookOrbitProvider, book_audiobook: dict[str, Any], book_multifile: dict[str, Any]
) -> None:
    """Ebook-only libraries and books without audio files are skipped."""
    await provider._refresh_catalog()
    assert set(provider._catalog) == {str(book_audiobook["id"]), str(book_multifile["id"])}
    query_mock = cast("Mock", provider._client.query_audiobooks)
    queried = [c.kwargs["library_id"] for c in query_mock.call_args_list]
    assert queried == [1, 3]

    audiobooks = [book async for book in provider.get_library_audiobooks()]
    assert {b.item_id for b in audiobooks} == set(provider._catalog)
    artists = {a.item_id async for a in provider.get_library_artists()}
    assert f"author:{book_audiobook['authors'][0]['id']}" in artists
    assert all(a.startswith(("author:", "narrator:")) for a in artists)


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
