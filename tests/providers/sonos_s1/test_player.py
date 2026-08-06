"""Unit tests for the Sonos S1 player."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import MediaType, PlaybackState

from music_assistant.models.player import PlayerMedia
from music_assistant.providers.sonos_s1.constants import TRANSITION_POLL_DELAY
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


def _mock_mass(sonos_player: SonosPlayer) -> MagicMock:
    """Return the player's mocked mass, running the threadsafe hop inline so it stays assertable."""
    mass = cast("MagicMock", sonos_player.mass)
    mass.loop.call_soon_threadsafe = MagicMock(side_effect=lambda callback: callback())
    return mass


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


def test_transitional_state_schedules_a_single_settle_poll(sonos_player: SonosPlayer) -> None:
    """A transitional transport state keeps the last known state and polls again shortly."""
    mass = _mock_mass(sonos_player)
    sonos_player._attr_playback_state = PlaybackState.IDLE
    sonos_player.soco.get_current_transport_info.return_value = {
        "current_transport_state": "TRANSITIONING"
    }

    sonos_player.poll_media()
    sonos_player.poll_media()

    assert sonos_player._attr_playback_state == PlaybackState.IDLE
    assert mass.call_later.call_count == 1
    assert mass.call_later.call_args.args[0] == TRANSITION_POLL_DELAY
    assert mass.call_later.call_args.args[1] == sonos_player._settled_state_poll
    # poll_media runs in a worker thread, so scheduling must hop to the event loop
    assert mass.loop.call_soon_threadsafe.called


def test_settled_state_re_arms_the_settle_poll(sonos_player: SonosPlayer) -> None:
    """Once a usable state is reported again, a later transition may schedule a new poll."""
    mass = _mock_mass(sonos_player)
    sonos_player.soco.get_current_transport_info.return_value = {
        "current_transport_state": "TRANSITIONING"
    }
    sonos_player.poll_media()

    sonos_player.soco.get_current_transport_info.return_value = {
        "current_transport_state": "PLAYING"
    }
    with (
        patch.object(sonos_player, "_set_basic_track_info"),
        patch.object(sonos_player, "update_player"),
    ):
        sonos_player.poll_media()

    sonos_player.soco.get_current_transport_info.return_value = {
        "current_transport_state": "TRANSITIONING"
    }
    sonos_player.poll_media()

    assert mass.call_later.call_count == 2


def test_transitional_event_schedules_a_settle_poll(sonos_player: SonosPlayer) -> None:
    """A transitional state delivered by subscription event also triggers a follow-up poll."""
    mass = _mock_mass(sonos_player)
    event = MagicMock()
    event.variables = {"transport_state": "TRANSITIONING"}

    sonos_player._handle_avtransport_event(event)

    assert mass.call_later.call_count == 1
    assert mass.call_later.call_args.args[1] == sonos_player._settled_state_poll


async def test_settle_poll_is_skipped_when_the_state_already_arrived(
    sonos_player: SonosPlayer,
) -> None:
    """A settled state reported before the follow-up poll runs makes that poll a no-op."""
    with patch.object(sonos_player, "poll", new=AsyncMock()) as poll:
        sonos_player._awaiting_settled_state = False
        await sonos_player._settled_state_poll()
        poll.assert_not_awaited()

        sonos_player._awaiting_settled_state = True
        await sonos_player._settled_state_poll()
        poll.assert_awaited_once()
