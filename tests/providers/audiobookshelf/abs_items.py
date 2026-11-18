"""Builders for Audiobookshelf schema objects used in the provider tests."""

from __future__ import annotations

from typing import Any

from aioaudiobookshelf.schema.library import LibraryItemExpandedBook


def expanded_book(item_id: str = "book1", **metadata: Any) -> LibraryItemExpandedBook:
    """Return an expanded abs book with a cover, metadata fields may be overridden."""
    return LibraryItemExpandedBook.from_dict(
        {
            "id": item_id,
            "ino": "1",
            "libraryId": "lib1",
            "folderId": "folder1",
            "path": f"/books/{item_id}",
            "relPath": item_id,
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
                "libraryItemId": item_id,
                "audioFiles": [],
                "chapters": [],
                "duration": 60.0,
                "size": 0,
                "tracks": [],
                "coverPath": f"/covers/{item_id}.jpg",
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
