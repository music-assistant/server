"""Tests that the playlog cleanup keeps the progress of library audiobooks and podcasts."""

from __future__ import annotations

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import (
    Audiobook,
    ItemMapping,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
)

from music_assistant.constants import DB_TABLE_MEDIA_PROGRESS
from music_assistant.mass import MusicAssistant

PODCAST_PROVIDER = "podcastfeed--cleanup"


def _episode(item_id: str, podcast_item_id: str) -> PodcastEpisode:
    return PodcastEpisode(
        item_id=item_id,
        provider=PODCAST_PROVIDER,
        name=item_id,
        position=1,
        duration=600,
        podcast=ItemMapping(
            item_id=podcast_item_id,
            provider=PODCAST_PROVIDER,
            name=podcast_item_id,
            media_type=MediaType.PODCAST,
        ),
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain="podcastfeed",
                provider_instance=PODCAST_PROVIDER,
            )
        },
    )


async def test_cleanup_keeps_library_progress(mass: MusicAssistant) -> None:
    """Old rows are removed, unless they hold the progress of a library audiobook or podcast."""
    user = await mass.webserver.auth.create_user("cleanup")
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
    await mass.music.podcasts.add_item_to_library(
        Podcast(
            item_id="podcast_1",
            provider=PODCAST_PROVIDER,
            name="Podcast",
            provider_mappings={
                ProviderMapping(
                    item_id="podcast_1",
                    provider_domain="podcastfeed",
                    provider_instance=PODCAST_PROVIDER,
                )
            },
        )
    )
    for episode in (_episode("episode_1", "podcast_1"), _episode("episode_2", "podcast_2")):
        await mass.music.mark_item_played(
            episode, fully_played=False, seconds_played=60, userid=user.user_id
        )
    for item_id, provider, media_type in (
        (audiobook.item_id, "library", "audiobook"),
        ("removed_book", "library", "audiobook"),
        ("track_1", "library", "track"),
    ):
        await mass.music.database.insert(
            DB_TABLE_MEDIA_PROGRESS,
            {
                "item_id": item_id,
                "provider": provider,
                "media_type": media_type,
                "name": item_id,
                "userid": user.user_id,
                "seconds_played": 120,
                "fully_played": False,
            },
        )
    await mass.music.database.execute(
        f"UPDATE {DB_TABLE_MEDIA_PROGRESS} SET timestamp = 1000 WHERE userid = :userid",
        {"userid": user.user_id},
    )

    await mass.music._cleanup_database()

    rows = await mass.music.database.get_rows(DB_TABLE_MEDIA_PROGRESS, {"userid": user.user_id})
    assert sorted(row["item_id"] for row in rows) == sorted([audiobook.item_id, "episode_1"])
