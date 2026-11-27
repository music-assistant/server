"""
Tests for the route a Sonos speaker gets a queue item over.

A queue item goes through the speaker's cloud queue. A stream without an end (a queue in
flow mode, a radio station, a live source) follows its codec: lossless it is an item of the
cloud queue, compressed it goes over the speaker's stream url.
"""

from types import MethodType
from typing import TYPE_CHECKING, Any, cast
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

if TYPE_CHECKING:
    from music_assistant_models.queue_item import QueueItem

BASE_URL = "http://192.168.1.10:8097"
SINGLE_URL = f"{BASE_URL}/single/session-2/{QUEUE_ID}/item0/sonos_player"
FLOW_URL = f"{BASE_URL}/flow/session-2/{QUEUE_ID}/item0/sonos_player"


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


def _media(media_type: MediaType, item_id: str = "item0") -> PlayerMedia:
    """Return the media of a queue item to play."""
    return PlayerMedia(
        uri="library://item/0",
        media_type=media_type,
        title="Item",
        source_id=QUEUE_ID,
        queue_item_id=item_id,
        queue_session_id="session-2",
    )


def _set_queue(player: SonosPlayer, *media_types: MediaType) -> None:
    """Let the queue of the speaker hold one item of each given media type, in that order."""
    queues = cast("Any", player.mass.player_queues)
    queues.items = [_make_queue_item(f"item{i}") for i in range(len(media_types))]
    types = {f"item{i}": media_type for i, media_type in enumerate(media_types)}

    async def _player_media(queue_item: QueueItem) -> PlayerMedia:
        return _media(types[queue_item.queue_item_id], queue_item.queue_item_id)

    queues.player_media_from_queue_item = _player_media


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


@pytest.mark.parametrize(
    ("extension", "plays_as_stream"), [("mp3", True), ("flac", False), ("wav", False)]
)
async def test_the_codec_is_read_from_the_path_of_the_stream_url(
    monkeypatch: pytest.MonkeyPatch, extension: str, plays_as_stream: bool
) -> None:
    """A query string behind the stream url does not hide the codec of the stream."""
    player, group = _make_speaker(monkeypatch)
    cast("MagicMock", player.mass).streams.resolve_stream_url = AsyncMock(
        return_value=f"{SINGLE_URL}.{extension}?player_id=sonos.player"
    )

    await player.play_media(_media(MediaType.RADIO))

    assert group.play_stream_url.await_count == (1 if plays_as_stream else 0)
    assert group.play_cloud_queue.await_count == (0 if plays_as_stream else 1)


async def test_a_compressed_track_plays_out_of_the_cloud_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A track has an end, so it stays an item of the cloud queue in any codec."""
    player, group = _make_speaker(monkeypatch, "compressed")

    await player.play_media(_media(MediaType.TRACK))

    group.play_stream_url.assert_not_awaited()
    group.play_cloud_queue.assert_awaited_once()
    assert player.cloud_queue_id == QUEUE_ID


async def test_a_lossless_radio_station_on_a_speaker_in_flow_mode_stays_lossless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A radio station is no part of the flow stream: it is a cloud queue item in its codec."""
    player, group = _make_speaker(monkeypatch, flow_mode=True)
    # the track behind it is a flow stream, which is a load of its own
    _set_queue(player, MediaType.RADIO, MediaType.TRACK)

    await player.play_media(_media(MediaType.RADIO))

    group.play_stream_url.assert_not_awaited()
    group.play_cloud_queue.assert_awaited_once()
    assert player.plays_flow_item is False
    window = await player.build_cloud_queue_window(None)
    assert window.includes_end is True
    assert [x.uri for x in window.items] == [
        f"{BASE_URL}/single/session-2/{QUEUE_ID}/pos/0/sonos_player.wav"
    ]


@pytest.mark.parametrize(
    ("playback_quality", "extension"),
    [(None, "wav"), ("lossless_uncompressed", "wav"), ("lossless_compressed", "flac")],
)
async def test_a_lossless_queue_in_flow_mode_plays_as_one_cloud_queue_item(
    monkeypatch: pytest.MonkeyPatch, playback_quality: str | None, extension: str
) -> None:
    """A flow stream in a lossless codec is the one item of the cloud queue, in that codec."""
    player, group = _make_speaker(monkeypatch, playback_quality, flow_mode=True)

    await player.play_media(_media(MediaType.TRACK))

    group.play_stream_url.assert_not_awaited()
    group.play_cloud_queue.assert_awaited_once()
    generation = player.cloud_queue_item_generation
    assert group.play_cloud_queue.await_args.kwargs["item_id"] == f"flow@{generation}"
    assert player.plays_flow_item is True
    assert player.cloud_queue_id == QUEUE_ID
    assert player._flow_queue_id is None
    assert player._queue_session_id == "loaded-session"
    window = await player.build_cloud_queue_window(f"flow@{generation}")
    assert window.wire_ids == [f"flow@{generation}"]
    assert (window.includes_beginning, window.includes_end) == (True, True)
    assert [x.uri for x in window.items] == [f"{FLOW_URL}.{extension}"]


async def test_a_compressed_queue_in_flow_mode_plays_over_the_stream_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A flow stream in a compressed codec is one stream over the stream url, in that codec."""
    player, group = _make_speaker(monkeypatch, "compressed", flow_mode=True)

    await player.play_media(_media(MediaType.TRACK))

    group.play_cloud_queue.assert_not_awaited()
    assert group.play_stream_url.await_args.args[0] == f"{FLOW_URL}.mp3"
    assert player.plays_flow_item is False
    assert player.cloud_queue_id is None
    assert player._playing_queue_id() == QUEUE_ID


async def test_media_that_is_no_queue_item_keeps_its_stream_in_aac(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live source that plays on the player itself still goes over the stream url as AAC."""
    player, group = _make_speaker(monkeypatch, live_sources_uncompressed=True)
    media = PlayerMedia(
        uri="plugin://audio_source/line_in",
        media_type=MediaType.AUDIO_SOURCE,
        title="Line-in",
        source_id="line_in",
        queue_session_id="source-session",
    )

    await player.play_media(media)

    group.play_cloud_queue.assert_not_awaited()
    assert group.play_stream_url.await_args.args[0] == (
        f"{BASE_URL}/source/source-session/line_in/sonos_player.aac"
    )


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
