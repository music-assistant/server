"""
Tests for restoring the duration probed during playback onto podcast episodes.

Podcast feeds that omit ``itunes:duration`` yield episodes with a duration of 0. The value
determined by ffmpeg while streaming is stored per item, so listings and later playbacks do
not start blind again. These tests use the ``mass`` fixture from ``tests/conftest.py``, which
provides a full MusicAssistant instance with a real cache database.
"""

from __future__ import annotations

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import ItemMapping, PodcastEpisode, ProviderMapping

from music_assistant.helpers.audio import get_probed_duration, store_probed_duration
from music_assistant.mass import MusicAssistant


def _episode(item_id: str, duration: int = 0) -> PodcastEpisode:
    return PodcastEpisode(
        item_id=item_id,
        provider="test_podcast_prov",
        name="Episode One",
        duration=duration,
        position=1,
        podcast=ItemMapping(
            item_id="show-001",
            provider="test_podcast_prov",
            name="My Podcast Show",
            media_type=MediaType.PODCAST,
        ),
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain="test_podcast_prov",
                provider_instance="test_podcast_prov",
            )
        },
    )


def _uri(episode: PodcastEpisode) -> str:
    assert episode.uri is not None  # set in __post_init__
    return episode.uri


async def test_probed_duration_is_restored_on_a_durationless_episode(mass: MusicAssistant) -> None:
    """An episode without a duration gets the one probed during an earlier playback."""
    episode = _episode("ep-restore")
    await store_probed_duration(mass, _uri(episode), 2700)

    await mass.music.podcasts._restore_probed_duration(episode)

    assert episode.duration == 2700


async def test_probed_duration_does_not_replace_the_feeds_duration(mass: MusicAssistant) -> None:
    """A duration published by the feed wins over the probed one."""
    episode = _episode("ep-keep", duration=2712)
    await store_probed_duration(mass, _uri(episode), 2700)

    await mass.music.podcasts._restore_probed_duration(episode)

    assert episode.duration == 2712


async def test_probed_duration_of_an_unknown_episode_stays_zero(mass: MusicAssistant) -> None:
    """An episode that was never played keeps its unknown duration."""
    episode = _episode("ep-unknown")

    await mass.music.podcasts._restore_probed_duration(episode)

    assert episode.duration == 0


async def test_zero_duration_is_never_stored(mass: MusicAssistant) -> None:
    """A probe that produced nothing useful is not written to the store."""
    episode = _episode("ep-zero")

    await store_probed_duration(mass, _uri(episode), 0)

    assert await get_probed_duration(mass, _uri(episode)) is None
