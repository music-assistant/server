"""Tests for player-specific live source output codec selection."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.player import PlayerMedia

from music_assistant.constants import (
    CONF_OUTPUT_CODEC,
    CONF_PREFER_WAV_FOR_LIVE_SOURCES,
)
from music_assistant.controllers.streams import StreamsController
from music_assistant.providers.chromecast.constants import CAST_PLAYER_CONFIG_ENTRIES
from music_assistant.providers.sonos.player import SonosPlayer
from music_assistant.providers.sonos_s1.player import SonosPlayer as SonosS1Player


def _controller(output_codec: str, prefer_wav_for_live_sources: bool) -> StreamsController:
    """Return a streams controller with one configured HTTP player."""
    controller = StreamsController.__new__(StreamsController)
    controller._base_url = "http://mass:8097"
    controller.mass = mass = MagicMock()
    player = MagicMock()
    values = {
        CONF_OUTPUT_CODEC: output_codec,
        CONF_PREFER_WAV_FOR_LIVE_SOURCES: prefer_wav_for_live_sources,
    }
    player.config.get_value.side_effect = lambda key, default=None: values.get(key, default)
    player.flow_mode = False
    player.supports_gapless = True
    mass.players.get_player.return_value = player
    mass.player_queues.get.return_value = SimpleNamespace(
        crossfade_enabled=False,
        overlay_enabled=False,
        overlay_source=None,
    )
    return controller


def _media(media_type: MediaType) -> PlayerMedia:
    """Return queue media with the data needed to resolve its stream URL."""
    return PlayerMedia(
        uri="fake_plugin://audio_source/main",
        media_type=media_type,
        source_id="queue-1",
        queue_item_id="item-1",
        custom_data={"session_id": "session-1"},
    )


@pytest.mark.parametrize(
    ("prefer_wav_for_live_sources", "expected_extension"),
    [(False, "mp3"), (True, "wav")],
)
async def test_audio_source_url_respects_wav_preference(
    prefer_wav_for_live_sources: bool,
    expected_extension: str,
) -> None:
    """Live sources use WAV only when the player opts into the low-latency path."""
    controller = _controller("mp3", prefer_wav_for_live_sources)

    url = await controller.resolve_stream_url("player-1", _media(MediaType.AUDIO_SOURCE))

    assert url == (
        f"http://mass:8097/single/session-1/queue-1/item-1/player-1.{expected_extension}"
    )


async def test_wav_preference_does_not_change_regular_tracks() -> None:
    """The live source preference leaves regular output codec selection unchanged."""
    controller = _controller("aac", True)

    url = await controller.resolve_stream_url("player-1", _media(MediaType.TRACK))

    assert url == "http://mass:8097/single/session-1/queue-1/item-1/player-1.aac"


def test_chromecast_prefers_wav_for_live_sources_by_default() -> None:
    """Cast players default to the known-compatible low-latency WAV path."""
    entry = next(
        entry
        for entry in CAST_PLAYER_CONFIG_ENTRIES
        if entry.key == CONF_PREFER_WAV_FOR_LIVE_SOURCES
    )

    assert entry.default_value is True


@pytest.mark.parametrize("player_class", [SonosPlayer, SonosS1Player])
async def test_sonos_prefers_wav_for_live_sources_by_default(
    player_class: type[SonosPlayer | SonosS1Player],
) -> None:
    """Sonos players default to the known-compatible low-latency WAV path."""
    player = player_class.__new__(player_class)

    entries = await player.get_config_entries()
    entry = next(entry for entry in entries if entry.key == CONF_PREFER_WAV_FOR_LIVE_SOURCES)

    assert entry.default_value is True
