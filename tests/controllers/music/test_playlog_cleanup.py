"""Tests that the playlog cleanup keeps the progress of library audiobooks."""

from __future__ import annotations

from music_assistant_models.media_items import Audiobook, ProviderMapping

from music_assistant.constants import DB_TABLE_MEDIA_PROGRESS
from music_assistant.mass import MusicAssistant


async def test_cleanup_keeps_library_audiobook_progress(mass: MusicAssistant) -> None:
    """Old rows are removed, unless they hold the progress of an audiobook in the library."""
    audiobook = await mass.music.audiobooks.add_item_to_library(
        Audiobook(
            item_id="book_1",
            provider="filesystem_local--cleanup",
            name="Book",
            provider_mappings={
                ProviderMapping(
                    item_id="book_1",
                    provider_domain="filesystem_local",
                    provider_instance="filesystem_local--cleanup",
                )
            },
        )
    )
    for item_id, provider, media_type, fully_played in (
        (audiobook.item_id, "library", "audiobook", False),
        ("removed_book", "library", "audiobook", True),
        ("episode_1", "podcastfeed--cleanup", "podcast_episode", True),
        ("track_1", "library", "track", True),
    ):
        await mass.music.database.insert(
            DB_TABLE_MEDIA_PROGRESS,
            {
                "item_id": item_id,
                "provider": provider,
                "media_type": media_type,
                "name": item_id,
                "userid": "user_1",
                "seconds_played": 120,
                "fully_played": fully_played,
                "timestamp": 1000,
            },
        )

    await mass.music._cleanup_database()

    rows = await mass.music.database.get_rows(DB_TABLE_MEDIA_PROGRESS, {"userid": "user_1"})
    assert [row["item_id"] for row in rows] == [audiobook.item_id]
