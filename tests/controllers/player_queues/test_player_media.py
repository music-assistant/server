"""Tests for PlayerMedia creation from queue items."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import ItemMapping, ProviderMapping, Track, UniqueList
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.constants import MASS_LOGO_ONLINE
from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData

QUEUE_ID = "test_queue"


def _track() -> Track:
    """Return a track with real metadata."""
    return Track(
        item_id="track_1",
        provider="library",
        name="One More Time",
        provider_mappings={
            ProviderMapping(
                item_id="track_1",
                provider_domain="library",
                provider_instance="library",
            )
        },
        artists=UniqueList(
            [
                ItemMapping(
                    item_id="artist_1",
                    provider="library",
                    media_type=MediaType.ARTIST,
                    name="Daft Punk",
                )
            ]
        ),
    )


def _controller() -> PlayerQueuesController:
    """Return a minimal controller with one queue."""
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.mass = MagicMock()
    ctrl._queue_data = {
        QUEUE_ID: PlayerQueueData(
            queue=PlayerQueue(
                queue_id=QUEUE_ID,
                active=True,
                display_name="Queue",
                available=True,
                items=1,
            ),
            session_id="session",
        )
    }
    return ctrl


def _queue_item() -> QueueItem:
    """Return a queue item backed by a real track."""
    return QueueItem(
        queue_id=QUEUE_ID,
        queue_item_id="item_1",
        name="Daft Punk - One More Time",
        duration=320,
        media_item=_track(),
    )


@pytest.mark.asyncio
async def test_player_media_keeps_metadata_by_default() -> None:
    """Without redaction, PlayerMedia carries the full track metadata."""
    ctrl = _controller()

    media = await ctrl.player_media_from_queue_item(_queue_item())

    assert media.title == "One More Time"
    assert media.artist == "Daft Punk"
    assert media.uri == _queue_item().uri
    assert media.duration == 320


@pytest.mark.asyncio
async def test_player_media_redacted_when_flag_set() -> None:
    """With redaction enabled, PlayerMedia carries only neutral metadata."""
    ctrl = _controller()
    ctrl.set_redact_media_details(QUEUE_ID, True)

    media = await ctrl.player_media_from_queue_item(_queue_item())

    assert media.title == "Music Assistant"
    assert media.artist == ""
    assert media.album == ""
    assert media.image_url == MASS_LOGO_ONLINE
    # the fields needed for playback must remain intact
    assert media.uri == _queue_item().uri
    assert media.duration == 320
    assert media.queue_item_id == "item_1"


@pytest.mark.asyncio
async def test_player_media_metadata_restored_after_disabling_redaction() -> None:
    """Disabling redaction restores the full track metadata."""
    ctrl = _controller()
    ctrl.set_redact_media_details(QUEUE_ID, True)
    ctrl.set_redact_media_details(QUEUE_ID, False)

    media = await ctrl.player_media_from_queue_item(_queue_item())

    assert media.title == "One More Time"
    assert media.artist == "Daft Punk"
