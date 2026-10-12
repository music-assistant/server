"""Tests for the Audiobookshelf parsers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from aioaudiobookshelf.schema.library import LibraryItemExpandedBook

from music_assistant.providers.audiobookshelf.parsers import parse_audiobook

if TYPE_CHECKING:
    from music_assistant_models.media_items import Audiobook

BASE_URL = "http://abs.local"
INSTANCE_ID = "audiobookshelf--test123"


def _book(**metadata: Any) -> LibraryItemExpandedBook:
    return LibraryItemExpandedBook.from_dict(
        {
            "id": "book1",
            "ino": "1",
            "libraryId": "lib1",
            "folderId": "folder1",
            "path": "/books/book1",
            "relPath": "book1",
            "mtimeMs": 0,
            "ctimeMs": 0,
            "birthtimeMs": 0,
            "mediaType": "book",
            "isFile": False,
            "addedAt": 1000,
            "updatedAt": 2000,
            "isMissing": False,
            "isInvalid": False,
            "libraryFiles": [],
            "size": 0,
            "media": {
                "libraryItemId": "book1",
                "audioFiles": [],
                "chapters": [],
                "duration": 60.0,
                "size": 0,
                "tracks": [],
                "coverPath": "/covers/book1.jpg",
                "metadata": {
                    "title": "Title",
                    "genres": [],
                    "explicit": False,
                    "titleIgnorePrefix": "Title",
                    "authorName": "",
                    "authorNameLF": "",
                    "narratorName": "",
                    "seriesName": "",
                    "authors": [],
                    "narrators": [],
                    "series": [],
                    **metadata,
                },
            },
        }
    )


def _parse_book(book: LibraryItemExpandedBook) -> Audiobook:
    return parse_audiobook(
        abs_audiobook=book,
        instance_id=INSTANCE_ID,
        domain="audiobookshelf",
        base_url=BASE_URL,
        audiobook_narrators=set(),
    )


@pytest.mark.parametrize(
    ("subtitle", "name"),
    [(None, "Title"), ("", "Title"), ("Part One", "Title | Part One")],
)
def test_audiobook_name_with_subtitle(subtitle: str | None, name: str) -> None:
    """Only a non-empty subtitle is appended to the title."""
    assert _parse_book(_book(subtitle=subtitle)).name == name


def test_audiobook_cover_url_has_no_token() -> None:
    """Abs serves covers without auth, so no credential is stored in the image url."""
    images = _parse_book(_book()).metadata.images

    assert images is not None
    assert [image.path for image in images] == [f"{BASE_URL}/api/items/book1/cover?ts=2000"]
