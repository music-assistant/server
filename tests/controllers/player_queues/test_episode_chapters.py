"""Tests that a queued podcast episode gets the chapters of its full details."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, Mock

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import (
    ItemMapping,
    MediaItemChapter,
    PodcastEpisode,
    ProviderMapping,
)

from music_assistant.controllers.player_queues import PlayerQueuesController

QUEUE_ID = "q1"


def _episode(chapters: list[MediaItemChapter] | None = None) -> PodcastEpisode:
    episode = PodcastEpisode(
        item_id="feed1 ep1",
        provider="podcastfeed--1",
        name="Episode One",
        position=1,
        podcast=ItemMapping(
            item_id="feed1",
            provider="podcastfeed--1",
            name="Feed One",
            media_type=MediaType.PODCAST,
        ),
        provider_mappings={
            ProviderMapping(
                item_id="feed1 ep1",
                provider_domain="podcastfeed",
                provider_instance="podcastfeed--1",
            )
        },
    )
    episode.metadata.chapters = chapters
    return episode


def _controller(details: PodcastEpisode) -> tuple[PlayerQueuesController, Mock]:
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    signal_update = Mock()
    ctrl.signal_update = signal_update  # type: ignore[method-assign]
    ctrl.mass = MagicMock()
    ctrl.mass.music.podcasts.episode = AsyncMock(return_value=details)
    return ctrl, signal_update


async def test_chapters_of_the_full_episode_land_on_the_queue_item() -> None:
    """Chapters only the single episode lookup has are copied over and announced."""
    chapters = [MediaItemChapter(position=1, name="Intro", start=0)]
    ctrl, signal_update = _controller(_episode(chapters))
    episode = _episode()

    await ctrl._load_episode_chapters(QUEUE_ID, episode)

    assert episode.metadata.chapters == chapters
    signal_update.assert_called_once_with(QUEUE_ID, items_changed=True)


async def test_episode_without_chapters_is_marked_as_looked_up() -> None:
    """An episode without chapters gets an empty list, so it is not looked up again."""
    ctrl, signal_update = _controller(_episode())
    episode = _episode()

    await ctrl._load_episode_chapters(QUEUE_ID, episode)

    assert episode.metadata.chapters == []
    signal_update.assert_not_called()
