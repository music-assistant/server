"""Tests for PlayerQueuesController.set_overlay."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from music_assistant_models.enums import PlaybackState
from music_assistant_models.errors import InvalidCommand, InvalidDataError
from music_assistant_models.media_items import (
    ItemMapping,
    MediaItemType,
    ProviderMapping,
    SoundEffect,
    Track,
)
from music_assistant_models.player_queue import PlayerQueue

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData

QUEUE_ID = "q1"


def _sound_effect(item_id: str = "rain") -> SoundEffect:
    """Build a SoundEffect on the 'ambient' provider."""
    return SoundEffect(
        item_id=item_id,
        provider="ambient",
        name=f"Sound {item_id}",
        provider_mappings={
            ProviderMapping(item_id=item_id, provider_domain="ambient", provider_instance="ambient")
        },
    )


def _track() -> Track:
    """Build a minimal Track (an invalid overlay source)."""
    return Track(
        item_id="track1",
        provider="test",
        name="Track",
        provider_mappings={
            ProviderMapping(item_id="track1", provider_domain="test", provider_instance="test")
        },
    )


def _controller(resolved_item: MediaItemType | None = None) -> PlayerQueuesController:
    """Build a bare controller with one queue and the collaborators stubbed out."""
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.signal_update = Mock()  # type: ignore[method-assign]
    ctrl.resume = AsyncMock()  # type: ignore[method-assign]
    ctrl.mass = MagicMock()
    ctrl.mass.music.get_item_by_uri = AsyncMock(return_value=resolved_item)
    queue = PlayerQueue(
        queue_id=QUEUE_ID, active=True, display_name="Queue", available=True, items=0
    )
    ctrl._queue_data = {QUEUE_ID: PlayerQueueData(queue=queue)}
    return ctrl


async def test_set_overlay_source_and_enable() -> None:
    """Setting a source and enabling stores the mapping and signals an update."""
    ctrl = _controller(resolved_item=_sound_effect())
    await ctrl.set_overlay(QUEUE_ID, enabled=True, source="ambient://sound_effect/rain")
    queue = ctrl._queue_data[QUEUE_ID].queue
    assert queue.overlay_enabled is True
    assert queue.overlay_source == ItemMapping.from_item(_sound_effect())
    ctrl.signal_update.assert_called_once_with(QUEUE_ID)  # type: ignore[attr-defined]
    # queue is idle: no playback restart
    ctrl.resume.assert_not_awaited()  # type: ignore[attr-defined]


async def test_set_overlay_enable_without_source_raises() -> None:
    """Enabling the overlay without a source selected is rejected."""
    ctrl = _controller()
    with pytest.raises(InvalidCommand):
        await ctrl.set_overlay(QUEUE_ID, enabled=True)


async def test_set_overlay_rejects_non_sound_effect_source() -> None:
    """Only sound effect items are accepted as overlay source."""
    ctrl = _controller(resolved_item=_track())
    with pytest.raises(InvalidDataError):
        await ctrl.set_overlay(QUEUE_ID, source="library://track/1")


@pytest.mark.parametrize("volume", [-1, 201])
async def test_set_overlay_rejects_out_of_range_volume(volume: int) -> None:
    """Overlay volume outside 0-200 is rejected."""
    ctrl = _controller()
    with pytest.raises(InvalidDataError):
        await ctrl.set_overlay(QUEUE_ID, volume=volume)


async def test_set_overlay_restarts_playback_when_playing() -> None:
    """An audible change while playing restarts playback from the current position."""
    ctrl = _controller(resolved_item=_sound_effect())
    queue = ctrl._queue_data[QUEUE_ID].queue
    queue.state = PlaybackState.PLAYING
    await ctrl.set_overlay(QUEUE_ID, enabled=True, source="ambient://sound_effect/rain")
    ctrl.resume.assert_awaited_once_with(QUEUE_ID)  # type: ignore[attr-defined]


async def test_set_overlay_inaudible_change_does_not_restart() -> None:
    """Changing volume while the overlay is disabled never restarts playback."""
    ctrl = _controller()
    queue = ctrl._queue_data[QUEUE_ID].queue
    queue.state = PlaybackState.PLAYING
    await ctrl.set_overlay(QUEUE_ID, volume=50)
    assert queue.overlay_volume == 50
    ctrl.signal_update.assert_called_once_with(QUEUE_ID)  # type: ignore[attr-defined]
    ctrl.resume.assert_not_awaited()  # type: ignore[attr-defined]


async def test_set_overlay_no_change_is_noop() -> None:
    """A call that changes nothing does not signal or restart anything."""
    ctrl = _controller()
    await ctrl.set_overlay(QUEUE_ID, enabled=False, volume=100)
    ctrl.signal_update.assert_not_called()  # type: ignore[attr-defined]
    ctrl.resume.assert_not_awaited()  # type: ignore[attr-defined]


async def test_set_overlay_disable_keeps_source() -> None:
    """Disabling the overlay keeps the selected source for later re-enabling."""
    ctrl = _controller(resolved_item=_sound_effect())
    await ctrl.set_overlay(QUEUE_ID, enabled=True, source="ambient://sound_effect/rain")
    await ctrl.set_overlay(QUEUE_ID, enabled=False)
    queue = ctrl._queue_data[QUEUE_ID].queue
    assert queue.overlay_enabled is False
    assert queue.overlay_source is not None
