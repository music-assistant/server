"""Tests for the Bose SoundTouch player."""

from __future__ import annotations

from unittest.mock import MagicMock

from music_assistant_models.enums import PlayerFeature

from music_assistant.providers.bose_soundtouch.client.schema.enums import PlayStatus
from music_assistant.providers.bose_soundtouch.client.schema.models import (
    ContentItem,
    Info,
    NowPlaying,
)
from music_assistant.providers.bose_soundtouch.player import BoseSoundTouchPlayer


def _make_player() -> BoseSoundTouchPlayer:
    provider = MagicMock()
    provider.instance_id = "bose_soundtouch_test"
    provider.get_setup_value = MagicMock(return_value=None)
    config = MagicMock()
    config.name = None
    config.get_value = MagicMock(return_value=None)
    provider.mass.config.get_base_player_config.return_value = config
    info = Info(device_id="AABBCCDDEEFF", name="Kitchen")
    player = BoseSoundTouchPlayer(provider, "soundtouch_AABBCCDDEEFF", MagicMock(), info)
    player.set_static_attributes(info)
    return player


def test_pause_withheld_while_music_assistant_streams() -> None:
    """Resuming a paused UPnP stream stalls on the speaker, so pause is only offered natively."""
    player = _make_player()

    player._update_state_from_now_playing(
        NowPlaying(play_status=PlayStatus.PLAY_STATE, content_item=ContentItem(source="UPNP"))
    )
    assert PlayerFeature.PAUSE not in player._attr_supported_features

    player._update_state_from_now_playing(
        NowPlaying(play_status=PlayStatus.PLAY_STATE, content_item=ContentItem(source="BLUETOOTH"))
    )
    assert PlayerFeature.PAUSE in player._attr_supported_features
