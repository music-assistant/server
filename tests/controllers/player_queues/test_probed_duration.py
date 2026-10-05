"""Tests that a duration determined while streaming is applied to the item that is playing."""

from __future__ import annotations

from typing import Any, NamedTuple
from unittest.mock import AsyncMock, MagicMock, Mock

from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.media_items import (
    AudioFormat,
    ItemMapping,
    PodcastEpisode,
    ProviderMapping,
    Radio,
)
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.player_queues import PlayerQueuesController

QUEUE_ID = "q1"
EPISODE_URI = "overcast--1://podcast_episode/feed1 ep1"


def _await_args(mock: AsyncMock) -> Any:
    """Return the call a mock was last awaited with, narrowed for the type checker."""
    assert mock.await_args is not None
    return mock.await_args


class _Harness(NamedTuple):
    """A bare queue controller with its collaborators kept at their mock types."""

    ctrl: PlayerQueuesController
    signal_update: Mock
    create_task: Mock
    cache_get: AsyncMock
    cache_set: AsyncMock

    async def stored_duration(self) -> int | None:
        """Run the fire and forget cache write and return the value it stored."""
        if not self.create_task.called:
            return None
        await self.create_task.call_args[0][0]
        return int(_await_args(self.cache_set).args[1])


def _harness(stored: int | None = None) -> _Harness:
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    signal_update = Mock()
    ctrl.signal_update = signal_update  # type: ignore[method-assign]
    mass = MagicMock()
    mass.cache.get = AsyncMock(return_value=stored)
    mass.cache.set = AsyncMock()
    ctrl.mass = mass
    return _Harness(ctrl, signal_update, mass.create_task, mass.cache.get, mass.cache.set)


def _streamdetails(media_type: MediaType, duration: int | None) -> StreamDetails:
    return StreamDetails(
        provider="overcast--1",
        item_id="feed1 ep1",
        audio_format=AudioFormat(content_type=ContentType.MP3),
        media_type=media_type,
        stream_type=StreamType.HTTP,
        duration=duration,
    )


def _provider_mappings(item_id: str, domain: str) -> set[ProviderMapping]:
    return {
        ProviderMapping(
            item_id=item_id,
            provider_domain=domain,
            provider_instance=f"{domain}--1",
        )
    }


def _episode(duration: int = 0) -> PodcastEpisode:
    return PodcastEpisode(
        item_id="feed1 ep1",
        provider="overcast--1",
        name="Episode One",
        uri=EPISODE_URI,
        duration=duration,
        position=1,
        podcast=ItemMapping(
            item_id="feed1",
            provider="overcast--1",
            name="Feed One",
            media_type=MediaType.PODCAST,
        ),
        provider_mappings=_provider_mappings("feed1 ep1", "overcast"),
    )


def _radio() -> Radio:
    return Radio(
        item_id="r1",
        provider="radiobrowser--1",
        name="Radio One",
        uri="radio://r1",
        provider_mappings=_provider_mappings("r1", "radiobrowser"),
    )


def _queue_item(media_item: Any, streamdetails: StreamDetails, duration: int = 0) -> QueueItem:
    return QueueItem(
        queue_id=QUEUE_ID,
        queue_item_id="qi1",
        name="Episode One",
        duration=duration,
        media_item=media_item,
        streamdetails=streamdetails,
    )


async def test_probed_duration_is_applied_and_stored() -> None:
    """A duration from the stream lands on the queue item, its media item and the store."""
    harness = _harness()
    episode = _episode()
    queue_item = _queue_item(episode, _streamdetails(MediaType.PODCAST_EPISODE, 2700))

    harness.ctrl._apply_probed_duration(queue_item)

    assert queue_item.duration == 2700
    assert episode.duration == 2700
    harness.signal_update.assert_called_once_with(QUEUE_ID, items_changed=True)
    assert await harness.stored_duration() == 2700
    assert _await_args(harness.cache_set).kwargs["persistent"] is True


async def test_probed_duration_does_not_overwrite_a_known_duration() -> None:
    """A duration reported by the provider is left alone, so no rounded value replaces it."""
    harness = _harness()
    episode = _episode(duration=2712)
    queue_item = _queue_item(
        episode, _streamdetails(MediaType.PODCAST_EPISODE, 2700), duration=2712
    )

    harness.ctrl._apply_probed_duration(queue_item)

    assert queue_item.duration == 2712
    assert episode.duration == 2712
    harness.signal_update.assert_not_called()
    assert await harness.stored_duration() is None


async def test_probed_duration_is_ignored_for_radio() -> None:
    """Radio never gets a duration, which would present it as seekable."""
    harness = _harness()
    queue_item = _queue_item(_radio(), _streamdetails(MediaType.RADIO, 2700))

    harness.ctrl._apply_probed_duration(queue_item)

    assert queue_item.duration == 0
    harness.signal_update.assert_not_called()
    assert await harness.stored_duration() is None


async def test_probed_duration_without_a_result_does_nothing() -> None:
    """A stream whose duration could not be determined leaves the item as it was."""
    harness = _harness()
    episode = _episode()
    queue_item = _queue_item(episode, _streamdetails(MediaType.PODCAST_EPISODE, None))

    harness.ctrl._apply_probed_duration(queue_item)

    assert queue_item.duration == 0
    assert episode.duration == 0
    harness.signal_update.assert_not_called()


async def test_stored_duration_is_restored_on_a_stale_item() -> None:
    """An item played from a listing fetched before its duration was known still gets one."""
    harness = _harness(stored=2700)
    episode = _episode()
    queue_item = _queue_item(episode, _streamdetails(MediaType.PODCAST_EPISODE, None))

    await harness.ctrl._restore_probed_duration(queue_item)

    assert queue_item.duration == 2700
    assert episode.duration == 2700
    assert _await_args(harness.cache_get).args[0] == EPISODE_URI
    harness.signal_update.assert_called_once_with(QUEUE_ID, items_changed=True)


async def test_stored_duration_is_not_looked_up_for_radio() -> None:
    """Radio is never given a stored duration, nor is the store consulted for it."""
    harness = _harness(stored=2700)
    queue_item = _queue_item(_radio(), _streamdetails(MediaType.RADIO, None))

    await harness.ctrl._restore_probed_duration(queue_item)

    assert queue_item.duration == 0
    harness.cache_get.assert_not_awaited()
