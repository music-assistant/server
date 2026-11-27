"""
Tests for the route a Sonos speaker gets a queue item over.

A queue item goes through the speaker's cloud queue. A radio station or live source in a
compressed codec goes over the speaker's stream url instead, as a queue in flow mode does.
"""

from types import MethodType
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.player import PlayerMedia

from music_assistant.constants import CONF_PLAYBACK_QUALITY, CONF_PREFER_WAV_FOR_LIVE_SOURCES
from music_assistant.controllers.streams import StreamsController
from music_assistant.providers.sonos.player import SonosPlayer
from tests.providers.sonos.test_cloud_queue import (
    QUEUE_ID,
    _loads_into_a_session,
    _make_player,
    _make_queue_item,
)

BASE_URL = "http://192.168.1.10:8097"
SINGLE_URL = f"{BASE_URL}/single/session-2/{QUEUE_ID}/item0/sonos_player"


def _make_speaker(
    monkeypatch: pytest.MonkeyPatch,
    playback_quality: str | None = None,
    *,
    flow_mode: bool = False,
    live_sources_uncompressed: bool = False,
) -> tuple[SonosPlayer, MagicMock]:
    """
    Create a speaker whose stream urls come from the real streams controller.

    :param monkeypatch: The fixture that sets the flow mode of the speaker.
    :param playback_quality: The playback quality level picked for the speaker, None for
        its default.
    :param flow_mode: Whether the speaker plays its queue in flow mode.
    :param live_sources_uncompressed: Whether the speaker gets live sources uncompressed.
    """
    player, _ = _make_player([_make_queue_item("item0")])
    settings = {
        CONF_PLAYBACK_QUALITY: playback_quality,
        CONF_PREFER_WAV_FOR_LIVE_SOURCES: live_sources_uncompressed,
    }
    cast("MagicMock", player.config).get_value = MagicMock(
        side_effect=lambda key, default=None: settings.get(key) or default
    )
    monkeypatch.setattr(SonosPlayer, "flow_mode", property(lambda _self: flow_mode))
    mass = cast("MagicMock", player.mass)
    mass.players.get_output_protocol.return_value = player
    mass.streams.base_url = BASE_URL
    mass.streams._needs_flow_mode.return_value = False
    queue = mass.player_queues.queue
    queue.crossfade_enabled = False
    queue.overlay_enabled = False
    mass.streams.resolve_stream_url = MethodType(StreamsController.resolve_stream_url, mass.streams)
    player._provider = MagicMock()
    player._provider.mass = mass
    group = MagicMock()
    group.active_session_id = None
    _loads_into_a_session(group)

    async def _play_stream_url(*_args: object, **_kwargs: object) -> None:
        group.active_session_id = "stream-session"

    group.play_stream_url = AsyncMock(side_effect=_play_stream_url)
    client = MagicMock()
    client.player.is_passive = False
    client.player.group = group
    player.client = client
    return player, group


def _media(media_type: MediaType) -> PlayerMedia:
    """Return the media of the queue item to play."""
    return PlayerMedia(
        uri="library://item/0",
        media_type=media_type,
        title="Item",
        source_id=QUEUE_ID,
        queue_item_id="item0",
        queue_session_id="session-2",
    )


@pytest.mark.parametrize("media_type", [MediaType.RADIO, MediaType.AUDIO_SOURCE])
async def test_a_compressed_item_without_an_end_plays_over_the_stream_url(
    monkeypatch: pytest.MonkeyPatch, media_type: MediaType
) -> None:
    """A radio station or live source in a compressed codec is one stream in that codec."""
    player, group = _make_speaker(monkeypatch, "compressed")

    await player.play_media(_media(media_type))

    group.play_cloud_queue.assert_not_awaited()
    group.play_stream_url.assert_awaited_once()
    assert group.play_stream_url.await_args.args[0] == f"{SINGLE_URL}.mp3"
    assert player.cloud_queue_id is None
    assert player._playing_queue_id() == QUEUE_ID
    assert player._queue_session_id == "stream-session"


@pytest.mark.parametrize(
    ("playback_quality", "extension"),
    [(None, "wav"), ("lossless_uncompressed", "wav"), ("lossless_compressed", "flac")],
)
async def test_a_lossless_radio_station_plays_out_of_the_cloud_queue(
    monkeypatch: pytest.MonkeyPatch, playback_quality: str | None, extension: str
) -> None:
    """A radio station in a lossless codec stays an item of the cloud queue, in that codec."""
    player, group = _make_speaker(monkeypatch, playback_quality)

    await player.play_media(_media(MediaType.RADIO))

    group.play_stream_url.assert_not_awaited()
    group.play_cloud_queue.assert_awaited_once()
    assert player.cloud_queue_id == QUEUE_ID
    window = await player.build_cloud_queue_window(None)
    assert window.items[0].uri.endswith(f".{extension}")


async def test_a_live_source_sent_uncompressed_plays_out_of_the_cloud_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The codec a stream really gets decides, not the playback quality level alone."""
    player, group = _make_speaker(monkeypatch, "compressed", live_sources_uncompressed=True)

    await player.play_media(_media(MediaType.AUDIO_SOURCE))

    group.play_stream_url.assert_not_awaited()
    group.play_cloud_queue.assert_awaited_once()


async def test_a_compressed_track_plays_out_of_the_cloud_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A track has an end, so it stays an item of the cloud queue in any codec."""
    player, group = _make_speaker(monkeypatch, "compressed")

    await player.play_media(_media(MediaType.TRACK))

    group.play_stream_url.assert_not_awaited()
    group.play_cloud_queue.assert_awaited_once()
    assert player.cloud_queue_id == QUEUE_ID


async def test_a_radio_station_in_flow_mode_keeps_its_stream_in_aac(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A speaker in flow mode gets a lossless radio station as one stream in AAC."""
    player, group = _make_speaker(monkeypatch, flow_mode=True)

    await player.play_media(_media(MediaType.RADIO))

    group.play_cloud_queue.assert_not_awaited()
    assert group.play_stream_url.await_args.args[0] == f"{SINGLE_URL}.aac"


async def test_an_announcement_at_the_compressed_level_plays_out_of_the_cloud_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An announcement has an end, so it stays a cloud queue of one item."""
    player, group = _make_speaker(monkeypatch, "compressed")
    cast("MagicMock", player.mass).streams.get_announcement_duration = AsyncMock(return_value=3)

    await player.play_media(
        PlayerMedia(uri=f"{BASE_URL}/announcement/x.mp3", media_type=MediaType.ANNOUNCEMENT)
    )

    group.play_stream_url.assert_not_awaited()
    group.play_cloud_queue.assert_awaited_once()


async def test_the_next_item_is_not_enqueued_on_a_speaker_that_plays_one_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A speaker that plays a stream has no cloud queue to read the next item from."""
    player, group = _make_speaker(monkeypatch, "compressed")
    refresh = AsyncMock()
    monkeypatch.setattr(player, "refresh_cloud_queue", refresh)
    await player.play_media(_media(MediaType.RADIO))

    next_media = _media(MediaType.TRACK)
    next_media.queue_item_id = "item1"
    await player.enqueue_next_media(next_media)

    refresh.assert_not_awaited()
    assert player.cloud_queue_id is None
    group.play_cloud_queue.assert_not_awaited()


async def test_a_track_started_from_a_playing_radio_stream_loads_the_cloud_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A step from a compressed radio station to a track goes back to the cloud queue."""
    player, group = _make_speaker(monkeypatch, "compressed")
    await player.play_media(_media(MediaType.RADIO))

    await player.play_media(_media(MediaType.TRACK))

    group.play_cloud_queue.assert_awaited_once()
    assert player.cloud_queue_id == QUEUE_ID
    assert player._flow_queue_id is None
    assert player._queue_session_id == "loaded-session"
