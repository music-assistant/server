"""Tests for detecting when the Roku moved on to the queued item."""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import MediaType, PlaybackState, PlayerFeature
from music_assistant_models.player import PlayerMedia

from music_assistant.providers.roku_media_assistant.player import MediaAssistantPlayer

APP_ID = "dev"
PLAYER_ID = "ROKU_TEST0001"


def _make_player() -> MediaAssistantPlayer:
    """Create a MediaAssistantPlayer with a mocked Roku that has the app running."""
    player = MediaAssistantPlayer.__new__(MediaAssistantPlayer)
    mass = MagicMock()
    mass.streams.resolve_stream_url = AsyncMock(return_value="http://192.0.2.10:8097/s.flac")
    provider = MagicMock()
    provider.mass = mass
    provider.config.get_value.return_value = APP_ID
    player.mass = mass
    player._provider = provider
    player.logger = logging.getLogger("test.roku_media_assistant.player")
    player._player_id = PLAYER_ID
    player._cache = {}
    player._config = MagicMock()
    player._config.get_value.return_value = False
    player._config.name = None
    player._attr_name = "Roku"
    player._attr_supported_features = {PlayerFeature.PLAY_MEDIA, PlayerFeature.ENQUEUE}
    player._attr_powered = True
    player._attr_playback_state = PlaybackState.PLAYING
    player._attr_current_media = None
    player._attr_elapsed_time = None
    player._attr_elapsed_time_last_updated = None
    player.queued = None
    device_info = MagicMock()
    device_info.app.app_id = APP_ID
    device_info.app.screensaver = False
    player.roku = MagicMock()
    player.roku.update = AsyncMock(return_value=device_info)
    player.roku._get_media_state = AsyncMock()
    player.roku_input = AsyncMock()  # type: ignore[method-assign]
    player.update_state = MagicMock()  # type: ignore[misc, method-assign]
    return player


def _media(title: str) -> PlayerMedia:
    """Return a queue item for the given title."""
    return PlayerMedia(
        uri=f"library://track/{title}",
        media_type=MediaType.TRACK,
        title=title,
        duration=300,
    )


async def _poll_at(player: MediaAssistantPlayer, seconds: float) -> None:
    """Poll the player while the Roku reports the given playback position."""
    state: dict[str, Any] = {"@state": "play", "position": f"{int(seconds * 1000)} ms"}
    player.roku._get_media_state.return_value = state  # type: ignore[attr-defined]
    await player.poll()


async def test_new_stream_is_not_mistaken_for_advancing_to_queued_item() -> None:
    """Starting a new stream (or seeking) resets the position without a track change."""
    player = _make_player()
    first, queued = _media("first"), _media("queued")
    await player.play_media(first)
    player.queued = queued
    await _poll_at(player, 200)

    # A seek back to the start is a new stream of the same item.
    await player.play_media(first)
    await _poll_at(player, 1)

    assert player._attr_current_media is first


async def test_advance_to_queued_item_is_detected() -> None:
    """A jump in position without a new stream means the Roku played the queued item."""
    player = _make_player()
    first, queued = _media("first"), _media("queued")
    await player.play_media(first)
    player.queued = queued
    await _poll_at(player, 200)

    await _poll_at(player, 1)

    assert player._attr_current_media is queued
