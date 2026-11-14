"""Tests for the Bose SoundTouch player."""

from __future__ import annotations

from unittest.mock import MagicMock

from music_assistant_models.config_entries import PlayerConfig
from music_assistant_models.enums import PlayerFeature

from music_assistant.models.player import Player
from music_assistant.providers.bose_soundtouch.client.schema.enums import PlayStatus
from music_assistant.providers.bose_soundtouch.client.schema.models import (
    ContentItem,
    Info,
    NowPlaying,
)
from music_assistant.providers.bose_soundtouch.player import BoseSoundTouchPlayer
from tests.common import MockProvider, mock_player_mass


def _make_player() -> tuple[Player, BoseSoundTouchPlayer]:
    """Return a speaker attached to a player of its own, without contacting it."""
    mass = mock_player_mass()
    provider = MockProvider("bose_soundtouch", instance_id="bose_soundtouch_test", mass=mass)
    provider.get_setup_value = MagicMock(return_value=None)  # type: ignore[attr-defined]
    info = Info(device_id="AABBCCDDEEFF", name="Kitchen")
    protocol = BoseSoundTouchPlayer(
        provider,  # type: ignore[arg-type]
        "soundtouch_AABBCCDDEEFF",
        MagicMock(),
        info,
    )
    protocol.set_static_attributes(info)
    player_id = "pl_soundtouch"
    player = Player(
        mass,
        player_id,
        PlayerConfig(
            values={}, provider=provider.instance_id, player_id=player_id, default_name="Kitchen"
        ),
    )
    mass.players._players[player_id] = player
    mass.players._protocols[protocol.protocol_id] = protocol
    player.add_protocol(protocol, protocol.role)
    protocol.set_player(player)
    player.set_initialized()
    return player, protocol


def test_pause_withheld_while_music_assistant_streams() -> None:
    """Resuming a paused UPnP stream stalls on the speaker, so pause is only offered natively."""
    player, protocol = _make_player()
    queue = MagicMock(current_item=MagicMock())
    protocol.mass.player_queues.get = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda queue_id: queue if queue_id == player.player_id else None
    )

    protocol._update_state_from_now_playing(
        NowPlaying(play_status=PlayStatus.PLAY_STATE, content_item=ContentItem(source="UPNP"))
    )
    assert PlayerFeature.PAUSE not in protocol._attr_supported_features
    assert protocol.active_source == protocol.protocol_id

    protocol._update_state_from_now_playing(
        NowPlaying(play_status=PlayStatus.PLAY_STATE, content_item=ContentItem(source="BLUETOOTH"))
    )
    assert PlayerFeature.PAUSE in protocol._attr_supported_features
