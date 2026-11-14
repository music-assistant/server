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

BOOK_PROVIDER = "filesystem_local--cleanup"
PODCAST_PROVIDER = "podcastfeed--cleanup"


def _mapping(item_id: str, provider_instance: str, in_library: bool) -> ProviderMapping:
    return ProviderMapping(
        item_id=item_id,
        provider_domain=provider_instance.split("--", maxsplit=1)[0],
        provider_instance=provider_instance,
        in_library=in_library,
    )


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
        provider_mappings={_mapping(item_id, PODCAST_PROVIDER, in_library=False)},
    )


async def test_cleanup_keeps_library_progress(mass: MusicAssistant) -> None:
    """
    Old rows are removed, unless they hold the progress of a library audiobook or podcast.

    A library row that only holds a dislike does not count as in the library.
    """
    user = await mass.webserver.auth.create_user("cleanup")
    book_ids = {}
    for item_id, in_library in (("book_1", True), ("book_disliked", False)):
        book = await mass.music.audiobooks.add_item_to_library(
            Audiobook(
                item_id=item_id,
                provider=BOOK_PROVIDER,
                name=item_id,
                provider_mappings={_mapping(item_id, BOOK_PROVIDER, in_library)},
            )
        )
        book_ids[item_id] = book.item_id
    for item_id, in_library in (("podcast_1", True), ("podcast_disliked", False)):
        await mass.music.podcasts.add_item_to_library(
            Podcast(
                item_id=item_id,
                provider=PODCAST_PROVIDER,
                name=item_id,
                provider_mappings={_mapping(item_id, PODCAST_PROVIDER, in_library)},
            )
        )
    for episode in (
        _episode("episode_1", "podcast_1"),
        _episode("episode_2", "podcast_2"),
        _episode("episode_disliked", "podcast_disliked"),
    ):
        await mass.music.mark_item_played(
            episode, fully_played=False, seconds_played=60, userid=user.user_id
        )
    for item_id, provider, media_type in (
        (book_ids["book_1"], "library", "audiobook"),
        (book_ids["book_disliked"], "library", "audiobook"),
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
    assert sorted(row["item_id"] for row in rows) == sorted([book_ids["book_1"], "episode_1"])
