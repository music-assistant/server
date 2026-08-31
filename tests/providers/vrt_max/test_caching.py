"""
Tests that everything handed to @use_cache survives a cache round-trip.

The cache stores ``to_dict()`` and rebuilds through ``base_class.from_dict()``. The rest
of the suite neutralizes the cache so the real method bodies run, which means a broken
round-trip would only ever show up against a warm cache in production.
"""

from __future__ import annotations

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import (
    ItemMapping,
    MediaItemChapter,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
)

from music_assistant.providers.vrt_max.models import VrtProgram, VrtResumeTarget, VrtSeason


def test_program_survives_cache_round_trip() -> None:
    """_fetch_program caches a VrtProgram, including its nested seasons."""
    program = VrtProgram(
        "/p/1",
        "Show",
        description="Desc",
        publisher="Radio 1",
        presenters=("Alice", "Bob"),
        seasons=(VrtSeason("Seizoen 2", "comp-2"), VrtSeason(None, "comp-1")),
    )

    restored = VrtProgram.from_dict(program.to_dict())

    assert restored == program
    assert restored.presenters == ("Alice", "Bob")
    assert restored.seasons[1].title is None


def test_resume_target_survives_cache_round_trip() -> None:
    """_fetch_resume_target caches a VrtResumeTarget across restarts."""
    target = VrtResumeTarget("media-id", "media name", 1800)

    assert VrtResumeTarget.from_dict(target.to_dict()) == target


def test_episode_survives_cache_round_trip() -> None:
    """_fetch_episodes caches a list of PodcastEpisode, each carrying its podcast mapping."""
    episode = PodcastEpisode(
        item_id="/vrtmax/luister/radio/s/show~1-2/show~1-3-0/",
        provider="vrt_max--test",
        name="Zondag 9 augustus - Ep",
        position=3,
        duration=3600,
        is_playable=True,
        podcast=ItemMapping(
            media_type=MediaType.PODCAST,
            item_id="/vrtmax/luister/radio/s/show~1-2/",
            provider="vrt_max--test",
            name="Show",
        ),
        provider_mappings={
            ProviderMapping(
                item_id="/vrtmax/luister/radio/s/show~1-2/show~1-3-0/",
                provider_domain="vrt_max",
                provider_instance="vrt_max--test",
                available=True,
            )
        },
    )
    episode.fully_played = True
    episode.resume_position_ms = 42000

    restored = PodcastEpisode.from_dict(episode.to_dict())

    assert restored.item_id == episode.item_id
    assert restored.position == 3
    assert restored.fully_played is True
    assert restored.resume_position_ms == 42000
    assert restored.podcast is not None
    assert restored.podcast.name == "Show"


def test_browse_results_survive_cache_round_trip() -> None:
    """_browse_programs caches Podcast items built from landing-page tiles."""
    podcast = Podcast(
        item_id="/p/1",
        provider="vrt_max--test",
        name="Pod",
        is_playable=True,
        provider_mappings={
            ProviderMapping(
                item_id="/p/1",
                provider_domain="vrt_max",
                provider_instance="vrt_max--test",
            )
        },
    )

    assert Podcast.from_dict(podcast.to_dict()).item_id == "/p/1"


def test_chapters_survive_cache_round_trip() -> None:
    """_fetch_chapters caches the played-songs tracklist."""
    chapter = MediaItemChapter(position=1, name="Song A", start=0.0, end=120.0)

    assert MediaItemChapter.from_dict(chapter.to_dict()) == chapter
