"""Unit tests for the Sonos S1 player."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import MediaType, PlaybackState

from music_assistant.models.player import PlayerMedia
from music_assistant.providers.sonos_s1.constants import POLL_INTERVAL, TRANSITION_POLL_INTERVAL
from music_assistant.providers.sonos_s1.player import SonosPlayer

STREAM_URL = "http://192.168.1.2:8097/single/sessionabc/queue1/item1/player1.flac"


@pytest.fixture
def sonos_player() -> SonosPlayer:
    """Create a SonosPlayer with a mocked soco device and provider."""
    soco = MagicMock()
    soco.uid = "RINCON_000E58AAAAAA01400"
    soco.household_id = "Sonos_household"
    soco.player_name = "Test Sonos"
    soco.ip_address = "127.0.0.1"
    soco.speaker_info = {"model_name": "Sonos Play:1"}
    provider = MagicMock()
    provider.mass.streams.resolve_stream_url = AsyncMock(return_value=STREAM_URL)
    return SonosPlayer(provider=provider, soco=soco)


def _make_media() -> PlayerMedia:
    """Return PlayerMedia as built by the queue controller, with an MA media uri."""
    return PlayerMedia(
        uri="library://track/123",
        media_type=MediaType.TRACK,
        title="Test Track",
        artist="Test Artist",
        album="Test Album",
        duration=180,
        source_id="queue1",
        queue_item_id="item1",
    )


async def test_enqueue_next_media_builds_didl_from_stream_url(
    sonos_player: SonosPlayer,
) -> None:
    """The enqueue metadata res element must contain the stream url, not the MA media uri."""
    await sonos_player.enqueue_next_media(_make_media())
    call_args = sonos_player.soco.avTransport.SetNextAVTransportURI.call_args
    args = dict(call_args.args[0])
    assert args["NextURI"] == STREAM_URL
    assert STREAM_URL in args["NextURIMetaData"]
    assert "library://track/123" not in args["NextURIMetaData"]


async def test_play_media_builds_didl_from_stream_url(sonos_player: SonosPlayer) -> None:
    """The play metadata res element must contain the stream url, not the MA media uri."""
    await sonos_player.play_media(_make_media())
    call_args = sonos_player.soco.play_uri.call_args
    assert call_args.args[0] == STREAM_URL
    assert STREAM_URL in call_args.kwargs["meta"]
    assert "library://track/123" not in call_args.kwargs["meta"]


def _set_transport_state(sonos_player: SonosPlayer, state: str) -> None:
    """Make the mocked speaker report the given transport state."""
    sonos_player.soco.get_current_transport_info.return_value = {"current_transport_state": state}


def test_transitional_state_shortens_the_poll_interval(sonos_player: SonosPlayer) -> None:
    """A transitional transport state keeps the last known state and is watched closely."""
    sonos_player._attr_playback_state = PlaybackState.IDLE
    _set_transport_state(sonos_player, "TRANSITIONING")

    sonos_player.poll_media()

    assert sonos_player._attr_playback_state == PlaybackState.IDLE
    assert sonos_player.poll_interval == TRANSITION_POLL_INTERVAL


def test_settled_state_restores_the_poll_interval(sonos_player: SonosPlayer) -> None:
    """A usable transport state returns the speaker to the regular poll interval."""
    sonos_player._attr_poll_interval = TRANSITION_POLL_INTERVAL

    _set_transport_state(sonos_player, "PLAYING")
    with (
        patch.object(sonos_player, "_set_basic_track_info"),
        patch.object(sonos_player, "update_player"),
    ):
        sonos_player.poll_media()

    assert sonos_player.poll_interval == POLL_INTERVAL


def test_transitional_event_shortens_the_poll_interval(sonos_player: SonosPlayer) -> None:
    """A transitional state delivered by subscription event is watched closely too."""
    event = MagicMock()
    event.variables = {"transport_state": "TRANSITIONING"}

    sonos_player._handle_avtransport_event(event)

    assert sonos_player.poll_interval == TRANSITION_POLL_INTERVAL
