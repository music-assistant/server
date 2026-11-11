"""Regression test: a genre a provider adds later sticks to a podcast already in the library."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

from music_assistant.constants import DB_TABLE_GENRE_MEDIA_ITEM_MAPPING
from tests.common import wait_for_sync_completion

if TYPE_CHECKING:
    from music_assistant_models.media_items import Podcast

    from music_assistant.mass import MusicAssistant
    from music_assistant.models.music_provider import MusicProvider

NEW_GENRE = "Spoken Word"


async def _sync_and_wait(mass: MusicAssistant, timeout: float = 60.0) -> None:
    """Run a full sync and wait until it and the follow-up genre scan are done."""
    async with wait_for_sync_completion(mass):
        await mass.music.start_sync()
    elapsed = 0.0
    while mass.music.active_sync_tasks or mass.music.genres._genre_scan_running:
        if elapsed >= timeout:
            raise TimeoutError("sync tasks did not become idle in time")
        await asyncio.sleep(0.25)
        elapsed += 0.25


async def test_new_provider_genre_survives_genre_scan(e2e_mass: MusicAssistant) -> None:
    """A re-sync saves a newly added provider genre, so the genre scan keeps its link."""
    mass = e2e_mass
    await _sync_and_wait(mass)
    podcast_count = await mass.music.podcasts.library_count()
    assert podcast_count > 0

    test_prov = cast("MusicProvider", mass.get_provider("test"))
    original_get_podcast = test_prov.get_podcast

    async def get_podcast_with_new_genre(prov_podcast_id: str) -> Podcast:
        podcast = await original_get_podcast(prov_podcast_id)
        podcast.metadata.genres = {*(podcast.metadata.genres or ()), NEW_GENRE}
        return podcast

    with patch.object(test_prov, "get_podcast", side_effect=get_podcast_with_new_genre):
        await _sync_and_wait(mass)

    for podcast in await mass.music.podcasts.library_items(limit=podcast_count):
        full_podcast = await mass.music.podcasts.get_library_item(podcast.item_id)
        assert NEW_GENRE in (full_podcast.metadata.genres or set())
    linked = await mass.music.database.get_count_from_query(
        f"SELECT * FROM {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} "
        "WHERE media_type = 'podcast' AND alias = :alias",
        {"alias": NEW_GENRE},
    )
    assert linked == podcast_count
