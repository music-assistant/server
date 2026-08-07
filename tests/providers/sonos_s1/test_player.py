"""Unit tests for the Sonos S1 player."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import MediaType

from music_assistant.models.player import PlayerMedia
from music_assistant.providers.sonos_s1.constants import SUBSCRIPTION_SERVICES
from music_assistant.providers.sonos_s1.player import SonosPlayer

STREAM_URL = "http://192.168.1.2:8097/single/sessionabc/queue1/item1/player1.flac"


def _make_soco(uid: str = "RINCON_000E58AAAAAA01400", name: str = "Test Sonos") -> MagicMock:
    """Create a mocked soco device."""
    soco = MagicMock()
    soco.uid = uid
    soco.household_id = "Sonos_household"
    soco.player_name = name
    soco.ip_address = "127.0.0.1"
    soco.speaker_info = {"model_name": "Sonos Play:1"}
    return soco


@pytest.fixture
def sonos_player() -> SonosPlayer:
    """Create a SonosPlayer with a mocked soco device and provider."""
    provider = MagicMock()
    provider.mass.streams.resolve_stream_url = AsyncMock(return_value=STREAM_URL)
    return SonosPlayer(provider=provider, soco=_make_soco())


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


async def _subscribe_with_failing_speaker(player: SonosPlayer) -> None:
    """Let the given player attempt to subscribe to a speaker that cannot be reached."""
    with (
        patch.object(player, "_subscribe_target", AsyncMock(side_effect=OSError("unreachable"))),
        patch.object(player, "update_state"),
    ):
        async with asyncio.timeout(5):
            await player.subscribe()


async def test_failed_subscribe_marks_the_speaker_offline() -> None:
    """A failed subscription must take the speaker offline and release the lock."""
    player = SonosPlayer(provider=MagicMock(), soco=_make_soco())

    await _subscribe_with_failing_speaker(player)

    assert player.available is False
    assert not player._subscription_lock.locked()


async def test_speaker_can_resubscribe_after_a_failed_subscribe() -> None:
    """A speaker that failed to subscribe must still be able to subscribe later."""
    player = SonosPlayer(provider=MagicMock(), soco=_make_soco())
    await _subscribe_with_failing_speaker(player)

    with patch.object(player, "_subscribe_target", AsyncMock()) as subscribe_target:
        async with asyncio.timeout(5):
            await player.subscribe()

    assert subscribe_target.await_count == len(SUBSCRIPTION_SERVICES)


async def test_speaker_taken_offline_mid_subscribe_keeps_no_subscriptions() -> None:
    """A speaker that goes offline while subscribing must not keep the subscriptions it created."""
    player = SonosPlayer(provider=MagicMock(), soco=_make_soco())
    subscribing = asyncio.Event()
    speaker_responds = asyncio.Event()

    async def _slow_subscribe_target(_target: object, _callback: object) -> None:
        subscribing.set()
        await speaker_responds.wait()
        player._subscriptions.append(MagicMock(unsubscribe=AsyncMock()))

    with (
        patch.object(player, "_subscribe_target", _slow_subscribe_target),
        patch.object(player, "update_state"),
    ):
        subscribe_task = asyncio.create_task(player.subscribe())
        await subscribing.wait()

        offline_task = asyncio.create_task(player.offline())
        await asyncio.sleep(0)
        assert not offline_task.done()

        speaker_responds.set()
        async with asyncio.timeout(5):
            await asyncio.gather(subscribe_task, offline_task)

    assert player.available is False
    assert player._subscriptions == []
    assert player.missing_subscriptions == SUBSCRIPTION_SERVICES


async def test_speaker_going_offline_is_not_resubscribed_halfway() -> None:
    """No new subscriptions may be created while a speaker is still going offline."""
    player = SonosPlayer(provider=MagicMock(), soco=_make_soco())
    unsubscribing = asyncio.Event()
    speaker_responds = asyncio.Event()

    async def _slow_unsubscribe() -> None:
        unsubscribing.set()
        await speaker_responds.wait()

    player._subscriptions = [MagicMock(unsubscribe=_slow_unsubscribe)]

    with (
        patch.object(player, "_subscribe_target", AsyncMock()) as subscribe_target,
        patch.object(player, "update_state"),
    ):
        offline_task = asyncio.create_task(player.offline())
        await unsubscribing.wait()

        subscribe_task = asyncio.create_task(player.subscribe())
        await asyncio.sleep(0)
        subscribe_target.assert_not_called()

        speaker_responds.set()
        async with asyncio.timeout(5):
            await asyncio.gather(offline_task, subscribe_task)

    assert subscribe_target.await_count == len(SUBSCRIPTION_SERVICES)
