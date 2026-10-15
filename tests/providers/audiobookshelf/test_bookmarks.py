"""Tests for reading Audiobookshelf bookmarks."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

from aioaudiobookshelf.schema.audio import AudioBookmark
from aioaudiobookshelf.schema.user import User
from music_assistant_models.media_items import MediaItemBookmark

from music_assistant.providers.audiobookshelf import Audiobookshelf


async def test_bookmarks_of_the_book_are_read_from_the_user(provider: Audiobookshelf) -> None:
    """Only the bookmarks of the asked book are returned, the user holds all of them."""
    user = Mock(spec=User)
    user.bookmarks = [
        AudioBookmark(library_item_id="book1", title="Arrakis", time=754.5, created_at=1),
        AudioBookmark(library_item_id="book2", title="Other book", time=30, created_at=2),
    ]
    provider._client.get_my_user = AsyncMock(return_value=user)  # type: ignore[method-assign]

    assert await provider.get_audiobook_bookmarks("book1") == [
        MediaItemBookmark(
            name="Arrakis",
            start=754.5,
            provider_instance_id="audiobookshelf--test123",
            provider_item_id="book1",
        )
    ]
