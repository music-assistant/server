"""
Tests for crediting the parent podcast show when a podcast episode is fully played.

The integration tests use the ``mass`` fixture from ``tests/conftest.py`` which
creates a full MusicAssistant instance with a real SQLite database in a
temporary directory.
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


async def test_fully_played_episode_credits_parent_podcast(mass: MusicAssistant) -> None:
    """A fully played episode writes a playlog row for its parent podcast show."""
    user = await mass.webserver.auth.create_user("podcastcredit")

    podcast = Podcast(
        item_id="show-001",
        provider="test_podcast_prov",
        name="My Podcast Show",
        provider_mappings=_provider_mapping(),
    )
    episode = PodcastEpisode(
        item_id="ep-001",
        provider="test_podcast_prov",
        name="Episode 1",
        provider_mappings=_provider_mapping(),
        position=1,
        podcast=podcast,
    )

    await mass.music.mark_item_played(
        episode, fully_played=True, user_initiated=False, userid=user.user_id
    )

    row = await mass.music.database.get_row(
        DB_TABLE_PLAYLOG,
        {
            "media_type": MediaType.PODCAST.value,
            "item_id": podcast.item_id,
            "userid": user.user_id,
        },
    )
    assert row is not None, "Expected a playlog row for the parent podcast show"
    assert int(row["user_initiated"]) == 0


async def test_user_initiated_show_play_stays_sticky_across_episode_credit(
    mass: MusicAssistant,
) -> None:
    """An explicit user play of the show stays user_initiated after a later episode credit."""
    user = await mass.webserver.auth.create_user("podcaststicky")

    podcast = Podcast(
        item_id="show-003",
        provider="test_podcast_prov",
        name="Sticky Show",
        provider_mappings=_provider_mapping(),
    )
    episode = PodcastEpisode(
        item_id="ep-003",
        provider="test_podcast_prov",
        name="Episode 3",
        provider_mappings=_provider_mapping(),
        position=1,
        podcast=podcast,
    )

    # the user explicitly plays the show -> a user_initiated podcast row
    await mass.music.mark_item_played(
        podcast, fully_played=True, user_initiated=True, userid=user.user_id
    )
    # a later episode credit writes user_initiated=False; the ON CONFLICT merge must keep it sticky
    await mass.music.mark_item_played(
        episode, fully_played=True, user_initiated=False, userid=user.user_id
    )

    row = await mass.music.database.get_row(
        DB_TABLE_PLAYLOG,
        {
            "media_type": MediaType.PODCAST.value,
            "item_id": podcast.item_id,
            "userid": user.user_id,
        },
    )
    assert row is not None
    assert int(row["user_initiated"]) == 1, (
        "an explicit show play must not be downgraded by a credit"
    )


async def test_partial_episode_does_not_credit_podcast(mass: MusicAssistant) -> None:
    """A partially played episode does NOT write a playlog row for the parent podcast."""
    user = await mass.webserver.auth.create_user("podcastnocredit")

    podcast = Podcast(
        item_id="show-002",
        provider="test_podcast_prov",
        name="Another Podcast Show",
        provider_mappings=_provider_mapping(),
    )
    episode = PodcastEpisode(
        item_id="ep-002",
        provider="test_podcast_prov",
        name="Episode 2",
        provider_mappings=_provider_mapping(),
        position=1,
        podcast=podcast,
    )

    await mass.music.mark_item_played(
        episode, fully_played=False, seconds_played=30, user_initiated=False, userid=user.user_id
    )

    row = await mass.music.database.get_row(
        DB_TABLE_PLAYLOG,
        {
            "media_type": MediaType.PODCAST.value,
            "item_id": podcast.item_id,
            "userid": user.user_id,
        },
    )
    assert row is None, "Expected no playlog row for the parent podcast on partial play"


async def test_library_podcast_episode_credits_library_scoped_row(
    mass: MusicAssistant,
) -> None:
    """An episode of a library podcast credits the library row, not a provider-scoped duplicate."""
    user = await mass.webserver.auth.create_user("podcastlibraryscoped")

    ext_item_id = uuid4().hex
    library_podcast = await mass.music.podcasts.add_item_to_library(
        Podcast(
            item_id="0",
            provider="library",
            name="Library Podcast Show",
            provider_mappings={
                ProviderMapping(
                    item_id=ext_item_id,
                    provider_domain="test_podcast_prov",
                    provider_instance="test_podcast_prov",
                )
            },
        )
    )

    # an explicit user play of the library show -> a user_initiated library-scoped row
    await mass.music.mark_item_played(
        library_podcast, fully_played=True, user_initiated=True, userid=user.user_id
    )

    # the episode's parent podcast carries provider-scoped ids, as returned by the provider layer
    provider_scoped_podcast = Podcast(
        item_id=ext_item_id,
        provider="test_podcast_prov",
        name="Library Podcast Show",
        provider_mappings=_provider_mapping(),
    )
    episode = PodcastEpisode(
        item_id="ep-lib-001",
        provider="test_podcast_prov",
        name="Episode 1",
        provider_mappings=_provider_mapping(),
        position=1,
        podcast=provider_scoped_podcast,
    )
    await mass.music.mark_item_played(
        episode, fully_played=True, user_initiated=False, userid=user.user_id
    )

    row = await mass.music.database.get_row(
        DB_TABLE_PLAYLOG,
        {
            "media_type": MediaType.PODCAST.value,
            "item_id": library_podcast.item_id,
            "provider": "library",
            "userid": user.user_id,
        },
    )
    assert row is not None, "Expected the episode credit to land on the library-scoped row"
    assert int(row["user_initiated"]) == 1, (
        "the episode credit must merge into the library row, not downgrade it"
    )

    dup_row = await mass.music.database.get_row(
        DB_TABLE_PLAYLOG,
        {
            "media_type": MediaType.PODCAST.value,
            "item_id": ext_item_id,
            "userid": user.user_id,
        },
    )
    assert dup_row is None, "Should not create a separate provider-scoped duplicate row"


async def test_non_library_podcast_episode_credits_provider_scoped_row(
    mass: MusicAssistant,
) -> None:
    """An episode of a podcast that isn't in the library still credits the provider-scoped row."""
    user = await mass.webserver.auth.create_user("podcastnonlibraryscoped")

    podcast = Podcast(
        item_id="show-004",
        provider="test_podcast_prov",
        name="Non-Library Show",
        provider_mappings=_provider_mapping(),
    )
    episode = PodcastEpisode(
        item_id="ep-004",
        provider="test_podcast_prov",
        name="Episode 4",
        provider_mappings=_provider_mapping(),
        position=1,
        podcast=podcast,
    )

    await mass.music.mark_item_played(
        episode, fully_played=True, user_initiated=False, userid=user.user_id
    )

    row = await mass.music.database.get_row(
        DB_TABLE_PLAYLOG,
        {
            "media_type": MediaType.PODCAST.value,
            "item_id": podcast.item_id,
            "provider": podcast.provider,
            "userid": user.user_id,
        },
    )
    assert row is not None, "Expected a provider-scoped playlog row for the non-library podcast"
