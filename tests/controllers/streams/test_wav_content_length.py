"""Tests for the WAV header and length the item stream announces with a forced content length."""

from __future__ import annotations

import struct
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from aiohttp.test_utils import make_mocked_request
from music_assistant_models.enums import ContentType, CrossfadeMode, MediaType, StreamType
from music_assistant_models.errors import AudioError
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.controllers.streams.controller import StreamsController

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

QUEUE_ID = "q1"
SESSION_ID = "sess1"
PLAYER_ID = "player1"
ITEM_ID = "item1"
DURATION = 128
WAV_24 = AudioFormat(content_type=ContentType.WAV, sample_rate=44100, bit_depth=24, channels=2)


def _controller(
    output_format: AudioFormat,
    http_profile: str,
    duration: int = DURATION,
    playback_speed: float = 1.0,
) -> MagicMock:
    """
    Build a bare streams controller serving one queue item to a player.

    :param output_format: The format the player gets.
    :param http_profile: The player's HTTP profile.
    :param duration: The item's duration in seconds.
    :param playback_speed: The playback speed of the queue item.
    """
    queue_item = QueueItem(
        queue_id=QUEUE_ID, queue_item_id=ITEM_ID, name=ITEM_ID, duration=duration
    )
    queue_item.extra_attributes["playback_speed"] = playback_speed
    queue_item.streamdetails = StreamDetails(
        provider="local--1",
        item_id=ITEM_ID,
        audio_format=AudioFormat(content_type=ContentType.FLAC),
        media_type=MediaType.TRACK,
        stream_type=StreamType.LOCAL_FILE,
        path="/music/item1.flac",
        duration=duration,
    )
    queue_data = PlayerQueueData(
        queue=MagicMock(queue_id=QUEUE_ID, display_name="Queue"),
        items=[queue_item],
        session_id=SESSION_ID,
    )
    player = MagicMock()
    player.stop_called = False
    player.get_config_value.return_value = http_profile

    ctrl = MagicMock()
    ctrl._open_item_streams = {}
    ctrl._ended_item_streams = {}
    ctrl._active_output_streams = 0
    ctrl._requested_item_ids.return_value = set()
    ctrl._release_abandoned_item_sources = AsyncMock()
    ctrl.get_crossfade_mode.return_value = CrossfadeMode.DISABLED
    ctrl.mass.player_queues.get.return_value = queue_data.queue
    ctrl.mass.player_queues.queue_data.return_value = queue_data
    ctrl.mass.player_queues.get_item.return_value = queue_item
    ctrl.mass.players.get_player.return_value = player
    ctrl.mass.config.get_raw_core_config_value.return_value = 8
    ctrl.mass.create_task.side_effect = lambda coro, **_kwargs: coro.close()
    ctrl.audio.select_pcm_format = AsyncMock(
        return_value=AudioFormat(content_type=ContentType.PCM_S16LE, sample_rate=44100)
    )
    ctrl.audio.get_output_format = AsyncMock(return_value=output_format)
    ctrl.audio.fade_into_pending.return_value = False
    return ctrl


async def _serve(
    ctrl: MagicMock, fmt: str, encoded: bytes, fail_before_audio: bool = False
) -> tuple[int | None, bytes, AudioFormat]:
    """
    Serve the item to a player that reads it whole.

    :param ctrl: The streams controller.
    :param fmt: The format the stream URL asks for.
    :param encoded: What the encoder produces.
    :param fail_before_audio: Whether the encoder fails before producing anything.
    :return: The announced content length, the body the player received and the format the
        encoder was asked for.
    """
    received = bytearray()
    encoder_formats: list[AudioFormat] = []
    writer = Mock()
    writer.write_headers = AsyncMock()
    writer.write_eof = AsyncMock()
    writer.drain = AsyncMock()

    async def _write(data: bytes) -> None:
        received.extend(data)

    writer.write = AsyncMock(side_effect=_write)
    request = make_mocked_request(
        "GET",
        f"/single/{SESSION_ID}/{QUEUE_ID}/{ITEM_ID}/{PLAYER_ID}.{fmt}",
        match_info={
            "session_id": SESSION_ID,
            "queue_id": QUEUE_ID,
            "queue_item_id": ITEM_ID,
            "player_id": PLAYER_ID,
            "fmt": fmt,
        },
        writer=writer,
    )

    async def _encoded(**kwargs: Any) -> AsyncGenerator[bytes]:
        encoder_formats.append(kwargs["output_format"])
        if fail_before_audio:
            raise AudioError("no audio")
        yield encoded[:30]
        yield encoded[30:]

    with (
        patch("music_assistant.controllers.streams.controller.get_ffmpeg_stream", _encoded),
        patch(
            "music_assistant.controllers.streams.controller.get_content_length",
            AsyncMock(return_value=1234567),
        ),
    ):
        resp = await StreamsController.serve_queue_item_stream(ctrl, request)
    return resp.content_length, bytes(received), encoder_formats[0]


@pytest.mark.asyncio
async def test_a_forced_wav_length_is_its_header_plus_whole_samples_and_matches_the_header() -> (
    None
):
    """The announced length and the header sent agree, and hold whole samples of raw PCM."""
    pcm = b"\x02" * 1200
    length, body, encoder_format = await _serve(
        _controller(WAV_24, "forced_content_length"), "wav", pcm
    )
    # the stored duration is rounded down, so the stream is announced a second longer
    data_size = 44100 * 6 * (DURATION + 1)
    assert encoder_format.content_type == ContentType.PCM_S24LE
    assert length == 44 + data_size
    assert body[:4] == b"RIFF"
    assert struct.unpack("<L", body[4:8])[0] == length - 8
    assert struct.unpack("<HHLLHH", body[20:36]) == (1, 2, 44100, 44100 * 6, 6, 24)
    assert body[36:40] == b"data"
    assert struct.unpack("<L", body[40:44])[0] == data_size
    assert body[44:] == pcm


@pytest.mark.asyncio
async def test_a_forced_wav_at_half_speed_covers_the_rounded_off_second_twice() -> None:
    """At half speed the second a duration was rounded down by lasts two stream seconds."""
    wav_16 = AudioFormat(content_type=ContentType.WAV, sample_rate=44100, bit_depth=16, channels=2)
    length, body, _encoder_format = await _serve(
        _controller(wav_16, "forced_content_length", playback_speed=0.5), "wav", b"\x02" * 400
    )
    # a 128.9 s track stored as 128 s streams 257.8 s of audio at speed 0.5
    data_size = 44100 * 4 * 258
    assert length == 44 + data_size
    assert struct.unpack("<L", body[40:44])[0] == data_size


@pytest.mark.asyncio
async def test_a_forced_wav_whose_source_fails_sends_no_header_and_is_not_served() -> None:
    """The header goes out with the first audio, so an item without any is not marked served."""
    ctrl = _controller(WAV_24, "forced_content_length")
    _length, body, _encoder_format = await _serve(ctrl, "wav", b"", fail_before_audio=True)
    assert body == b""
    ctrl.mass.player_queues.track_loaded_in_buffer.assert_not_called()


@pytest.mark.asyncio
async def test_other_codecs_keep_the_estimated_length_and_their_own_bytes() -> None:
    """Only WAV changes: a FLAC stream keeps the estimate and is passed on untouched."""
    flac_format = AudioFormat(content_type=ContentType.FLAC, sample_rate=44100, bit_depth=24)
    encoded = b"fLaC" + b"\x03" * 500
    length, body, encoder_format = await _serve(
        _controller(flac_format, "forced_content_length"), "flac", encoded
    )
    assert encoder_format == flac_format
    assert length == 1234567
    assert body == encoded[:length]


@pytest.mark.asyncio
async def test_a_wav_too_long_for_its_header_keeps_the_estimate() -> None:
    """A WAV item past the 4 GB a header can describe keeps the encoder's WAV and the estimate."""
    wav_16 = AudioFormat(content_type=ContentType.WAV, sample_rate=44100, bit_depth=16, channels=2)
    encoded = b"RIFF" + b"\x04" * 500
    length, body, encoder_format = await _serve(
        _controller(wav_16, "forced_content_length", duration=7 * 3600), "wav", encoded
    )
    assert encoder_format == wav_16
    assert length == 1234567
    assert body == encoded
