"""Tests for Audiobookshelf audiobook narrators."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, Mock

from aioaudiobookshelf.schema.library import LibraryItemExpandedBook
from music_assistant_models.media_items import Artist

from music_assistant.providers.audiobookshelf import Audiobookshelf


def _book(item_id: str, narrators: list[str]) -> LibraryItemExpandedBook:
    return LibraryItemExpandedBook.from_dict(
        {
            "id": item_id,
            "ino": "1",
            "libraryId": "lib1",
            "folderId": "folder1",
            "path": "/books/book",
            "relPath": "book",
            "isFile": False,
            "mtimeMs": 1,
            "ctimeMs": 1,
            "birthtimeMs": 1,
            "addedAt": 1,
            "updatedAt": 1,
            "isMissing": False,
            "isInvalid": False,
            "mediaType": "book",
            "libraryFiles": [],
            "size": 1,
            "media": {
                "libraryItemId": item_id,
                "metadata": {
                    "title": "Book",
                    "titleIgnorePrefix": "Book",
                    "authors": [],
                    "authorName": "",
                    "authorNameLF": "",
                    "narrators": narrators,
                    "narratorName": ", ".join(narrators),
                    "series": [],
                    "seriesName": "",
                    "genres": [],
                    "explicit": False,
                },
                "audioFiles": [],
                "chapters": [],
                "duration": 3600,
                "size": 1,
                "tracks": [
                    {
                        "index": 1,
                        "startOffset": 0,
                        "duration": 3600,
                        "title": "track",
                        "contentUrl": f"/api/items/{item_id}/file/1",
                        "metadata": None,
                    }
                ],
            },
        }
    )


async def test_narrators_taken_from_book(provider: Audiobookshelf) -> None:
    """Narrators come from the book itself, with the ids of abs' narrators endpoint."""
    books = [_book("book1", ["Stefan Kaminski", "ÿþ>?~"]), _book("book2", [])]

    async def get_library_items(**_: Any) -> AsyncGenerator[Mock]:
        yield Mock(results=[Mock(id_="book1"), Mock(id_="book2")])
        yield Mock(results=[])

    provider.mass.config.get = Mock(return_value={"url": "http://abs.local"})  # type: ignore[method-assign]
    provider._client.get_library_items = get_library_items  # type: ignore[method-assign]
    provider._client.get_library_item_batch_book = AsyncMock(return_value=books)  # type: ignore[method-assign]
    get_library_narrators = AsyncMock(return_value=[])
    provider._client.get_library_narrators = get_library_narrators  # type: ignore[method-assign]

    audiobooks = [x async for x in provider.get_library_audiobooks()]

    assert [
        [(n.item_id, n.name) for n in x.narrators if isinstance(n, Artist)] for x in audiobooks
    ] == [
        [("U3RlZmFuIEthbWluc2tp", "Stefan Kaminski"), ("w7%2FDvj4%2Ffg%3D%3D", "ÿþ>?~")],
        [],
    ]
    get_library_narrators.assert_not_called()
