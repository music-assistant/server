"""Tests for the active source ChromecastPlayer reports for the app running on the device."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import PlaybackState, PlayerType
from music_assistant_models.player import PlayerSource

from music_assistant.providers.chromecast.constants import APP_MEDIA_RECEIVER
from music_assistant.providers.chromecast.player import ChromecastPlayer
from tests.common import MockPlayer, MockProvider
from tests.providers.chromecast.test_media_status_updates import _fake_player, _media_status

STREAM_BASE_URL = "http://192.168.1.10:8097"
PLAYER_ID = "cast_group_1"


def _fake_media_receiver_player() -> Any:
    """Build a Cast group running the Default Media Receiver, with no source reported yet."""
    fake = _fake_player(player_type=PlayerType.GROUP, powered=True)
    fake.mass.streams.base_url = STREAM_BASE_URL
    fake.cc = MagicMock(app_id=APP_MEDIA_RECEIVER, app_display_name="Default Media Receiver")
    fake._attr_active_source = None
    fake._attr_source_list = []
    return fake


def test_foreign_stream_on_media_receiver_is_a_foreign_source() -> None:
    """Another app casting through the Default Media Receiver shows up as its own source."""
    fake = _fake_media_receiver_player()

    ChromecastPlayer._handle_media_status(
        fake, _media_status(playing=True, content_id="https://radio.example.com/live.mp3")
    )

    assert fake._attr_active_source == "default_media_receiver"
    assert [source.id for source in fake._attr_source_list] == ["default_media_receiver"]


@pytest.mark.parametrize(
    "content_id",
    [f"{STREAM_BASE_URL}/flow/session/{PLAYER_ID}/item.flac", ""],
    ids=["own_stream", "nothing_loaded"],
)
def test_own_session_on_media_receiver_is_no_foreign_source(content_id: str) -> None:
    """Our own stream on the Default Media Receiver, or none loaded yet, stays MA's session."""
    fake = _fake_media_receiver_player()

    ChromecastPlayer._handle_media_status(
        fake, _media_status(playing=bool(content_id), content_id=content_id)
    )

    assert fake._attr_active_source is None
    assert fake._attr_source_list == []


def test_foreign_cast_app_takes_over_the_ma_queue() -> None:
    """A foreign app the Cast player reports as source releases the MA queue it played."""
    mass = MagicMock()
    mass.closing = False
    mass.config.get_raw_player_config_value = MagicMock(
        side_effect=lambda _player_id, _key, default=None: default
    )
    mass.player_queues.get = MagicMock(return_value=None)
    mass.players.get_audio_source_session = MagicMock(return_value=None)
    mass.players.scale_volume_from_device = MagicMock(side_effect=lambda _pid, volume: volume)
    player = MockPlayer(MockProvider("chromecast", mass=mass), PLAYER_ID, "Cast group")
    player.set_active_mass_source(PLAYER_ID)
    # the trust flag and source entry a real ChromecastPlayer carries for a foreign app
    player._attr_trusts_reported_source = ChromecastPlayer._attr_trusts_reported_source
    player._attr_source_list = [
        PlayerSource(id="bbc_sounds", name="BBC Sounds", passive=True),
    ]
    player._attr_active_source = "bbc_sounds"
    player._attr_playback_state = PlaybackState.PLAYING

    player.update_state(signal_event=False)

    assert player.state.active_source == "bbc_sounds"


def test_a_known_app_in_its_own_spelling_gets_controls() -> None:
    """An app that names itself in its own spelling ("TIDAL") is offered its controls."""
    fake = _fake_media_receiver_player()
    fake.cc = MagicMock(app_id="tidal_app", app_display_name="TIDAL")
    status = _media_status(playing=True, content_id="tidal://track/1")
    # no media session, so only recognising the app can offer the controls
    status.media_session_id = None

    ChromecastPlayer._handle_media_status(fake, status)

    source = next(source for source in fake._attr_source_list if source.id == "tidal")
    assert source.name == "TIDAL"
    assert source.can_play_pause
    assert source.can_next_previous


def test_an_unknown_app_gets_the_controls_it_reports() -> None:
    """An app outside the known ones is offered the controls its media status announces."""
    fake = _fake_media_receiver_player()
    fake.cc = MagicMock(app_id="tunein_app", app_display_name="TuneIn Free")
    status = _media_status(playing=True, content_id="https://radio.example.com/live.mp3")
    status.media_session_id = 1
    status.supports_pause = True
    status.supports_seek = False
    # next and previous share one control, so it needs both
    status.supports_queue_next = True
    status.supports_queue_prev = False

    ChromecastPlayer._handle_media_status(fake, status)

    source = next(source for source in fake._attr_source_list if source.id == "tunein_free")
    assert source.can_play_pause
    assert not source.can_seek
    assert not source.can_next_previous


def test_an_unknown_app_without_media_gets_no_controls() -> None:
    """An app that has not loaded media yet is offered no controls, until it reports them."""
    fake = _fake_media_receiver_player()
    fake.cc = MagicMock(app_id="tunein_app", app_display_name="TuneIn Free")
    status = _media_status(playing=False, content_id="")
    status.media_session_id = None

    ChromecastPlayer._handle_media_status(fake, status)

    source = next(source for source in fake._attr_source_list if source.id == "tunein_free")
    assert not source.can_play_pause

    status = _media_status(playing=True, content_id="https://radio.example.com/live.mp3")
    status.media_session_id = 1
    status.supports_pause = True
    status.supports_seek = False
    status.supports_queue_next = False
    ChromecastPlayer._handle_media_status(fake, status)

    source = next(source for source in fake._attr_source_list if source.id == "tunein_free")
    assert source.can_play_pause
