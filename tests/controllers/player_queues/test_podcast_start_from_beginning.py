"""
Tests for the ``start_from_beginning`` podcast playback option.

The option lets an episode start at absolute position 0 while leaving the
user's saved progress in the playlog untouched (a non-destructive counterpart
to ``music/mark_unplayed``). See discussion music-assistant/discussions#4191.

These integration tests use the ``mass`` fixture from ``tests/conftest.py``
which creates a full MusicAssistant instance with a real SQLite database.
"""

from __future__ import annotations

from uuid import uuid4

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import Podcast, PodcastEpisode, ProviderMapping

from music_assistant.constants import DB_TABLE_PLAYLOG
from music_assistant.mass import MusicAssistant


def _provider_mapping(provider: str = "test_podcast_prov") -> set[ProviderMapping]:
    """Create a single provider mapping with a unique item id."""
    return {
        ProviderMapping(
            item_id=uuid4().hex,
            provider_domain=provider,
            provider_instance=provider,
        )
    }


async def test_start_from_beginning_ignores_saved_resume(mass: MusicAssistant) -> None:
    """start_from_beginning forces resume_position_ms=0 without wiping saved progress."""
    user = await mass.webserver.auth.create_user("podcaststartfrombeginning")

    podcast = Podcast(
        item_id="show-sfb-001",
        provider="test_podcast_prov",
        name="Start From Beginning Show",
        provider_mappings=_provider_mapping(),
    )
    episode = PodcastEpisode(
        item_id="ep-sfb-001",
        provider="test_podcast_prov",
        name="Episode 1",
        provider_mappings=_provider_mapping(),
        position=1,
        podcast=podcast,
    )

    # seed 120 seconds of saved progress for this episode
    await mass.music.mark_item_played(
        episode,
        fully_played=False,
        seconds_played=120,
        user_initiated=True,
        userid=user.user_id,
    )

    resolver = mass.player_queues._media_resolver

    # sanity: without the flag, the saved 120s resume position is applied
    normal = await resolver.get_next_podcast_episodes(None, episode, userid=user.user_id)
    assert normal[0].resume_position_ms == 120_000

    # with the flag, the episode starts at absolute 0
    from_start = await resolver.get_next_podcast_episodes(
        None, episode, userid=user.user_id, start_from_beginning=True
    )
    assert from_start[0].resume_position_ms == 0
    assert from_start[0].fully_played is False

    # the saved progress row must be left untouched (non-destructive)
    row = await mass.music.database.get_row(
        DB_TABLE_PLAYLOG,
        {
            "media_type": MediaType.PODCAST_EPISODE.value,
            "item_id": episode.item_id,
            "provider": episode.provider,
            "userid": user.user_id,
        },
    )
    assert row is not None, "start_from_beginning must not delete the saved progress row"
    assert row["seconds_played"] == 120
