"""Tests for which source a player is resolved to be playing."""

from __future__ import annotations

from unittest.mock import MagicMock

from music_assistant_models.enums import PlaybackState
from music_assistant_models.player import PlayerSource

from tests.common import MockPlayer, MockProvider

PLAYER_ID = "player_1"


def _create_player() -> MockPlayer:
    """Create a player that was playing the Music Assistant queue."""
    mass = MagicMock()
    mass.closing = False
    mass.config.get_raw_player_config_value = MagicMock(
        side_effect=lambda _player_id, _key, default=None: default
    )
    mass.player_queues.get = MagicMock(return_value=None)
    mass.players.get_audio_source_session = MagicMock(return_value=None)
    mass.players.scale_volume_from_device = MagicMock(side_effect=lambda _player_id, volume: volume)
    provider = MockProvider("test_provider", mass=mass)
    player = MockPlayer(provider, PLAYER_ID, "Player 1")
    player.set_active_mass_source(PLAYER_ID)
    return player


def test_a_source_a_trusting_player_lists_itself_takes_over_from_the_ma_queue() -> None:
    """Test a source the player reports as its own is what it is playing, if it is trusted."""
    player = _create_player()
    player._attr_trusts_reported_source = True
    # "YouTube Music" is deliberately a name that is absent from EXTERNAL_SOURCES
    player._attr_source_list = [
        PlayerSource(id="YouTube Music", name="YouTube Music", passive=True)
    ]
    player._attr_active_source = "YouTube Music"
    player._attr_playback_state = PlaybackState.PLAYING

    player.update_state(signal_event=False)

    assert player.state.active_source == "YouTube Music"


def test_a_listed_device_input_does_not_take_over_when_the_player_is_not_trusted() -> None:
    """Test a source of an untrusted player does not end the remembered MA queue."""
    player = _create_player()
    # "Wi-Fi" is the transport our own stream arrives on for some devices, which they
    # report as soon as that stream pauses - here right after MA stopped playback
    player._attr_source_list = [PlayerSource(id="Wi-Fi", name="Wi-Fi")]
    player._attr_active_source = "Wi-Fi"
    player._attr_playback_state = PlaybackState.PAUSED

    player.update_state(signal_event=False)

    assert player.state.active_source == PLAYER_ID


def test_an_unlisted_source_does_not_take_over_from_the_ma_queue() -> None:
    """Test a source nothing knows about is not trusted, since many players misreport."""
    player = _create_player()
    player._attr_source_list = []
    player._attr_active_source = "Some Service"
    player._attr_playback_state = PlaybackState.PLAYING

    player.update_state(signal_event=False)

    assert player.state.active_source == PLAYER_ID


def test_a_known_external_source_takes_over_from_the_ma_queue() -> None:
    """Test a source from the explicit list takes the player over."""
    player = _create_player()
    player._attr_source_list = []
    player._attr_active_source = "qobuz"
    player._attr_playback_state = PlaybackState.PLAYING

    player.update_state(signal_event=False)

    assert player.state.active_source == "qobuz"
