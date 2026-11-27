"""Tests for the stream route when a player fetches a track as a file."""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from types import MethodType
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import aiohttp
import numpy as np
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer, make_mocked_request
from music_assistant_models.enums import (
    ContentType,
    CrossfadeMode,
    MediaType,
    RepeatMode,
    StreamType,
)
from music_assistant_models.errors import AudioError
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import DLNA_CONTENT_FEATURES, DLNA_CONTENT_FEATURES_FILE
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.controllers.streams.audio_buffer import AudioBuffer
from music_assistant.controllers.streams.controller import StreamsController
from music_assistant.controllers.streams.file_stream import (
    FileStreams,
    mp3_frame_count,
    mp3_frame_offset,
)
from music_assistant.helpers.ffmpeg import check_ffmpeg_version, get_ffmpeg_stream

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Iterator

QUEUE_ID = "q1"
SESSION_ID = "sess1"
PLAYER_ID = "player1"
ITEM_ID = "item1"
BASE_URL = "http://192.168.1.10:8097"
PCM_16 = AudioFormat(content_type=ContentType.PCM_S16LE, sample_rate=44100, bit_depth=16)
WAV_16 = AudioFormat(content_type=ContentType.WAV, sample_rate=44100, bit_depth=16)
BYTES_PER_SECOND = 44100 * 4
HEADER_SIZE = 44
KIB_16 = 16384
MP3_44K = AudioFormat(content_type=ContentType.MP3, sample_rate=44100, bit_depth=16)
MP3_48K = AudioFormat(content_type=ContentType.MP3, sample_rate=48000, bit_depth=16)
MP3_BYTES_PER_SECOND = 40000
# the 49 frames an encode of an MP3 at 44.1 kHz can start on every time: their bytes and time
MP3_BLOCK_SIZE, MP3_BLOCK_MS = 51200, 1280


def _tone(seconds: float, pcm_format: AudioFormat = PCM_16) -> bytes:
    """
    Make audio that differs from one moment to the next, with tones over the whole spectrum.

    :param seconds: Length of the audio.
    :param pcm_format: The format of the audio, 16 or 24 bit.
    """
    rate = pcm_format.sample_rate
    time = np.arange(round(seconds * rate)) / rate
    signal = sum(
        np.sin(2 * np.pi * frequency * time + phase) * (1 + 0.5 * np.sin(2 * np.pi * beat * time))
        for frequency, phase, beat in ((110, 0.0, 0.31), (990, 1.0, 0.47), (6200, 2.0, 0.73))
    )
    signal = signal / 4.5 * 0.25 + np.random.default_rng(7).normal(0, 0.01, len(time))
    stereo = np.stack([signal, signal[::-1]], axis=1)
    if pcm_format.bit_depth == 16:
        return (stereo * 32767).astype("<i2").tobytes()
    samples = (stereo * 8388607).astype("<i4")
    return samples.view(np.uint8).reshape(-1, 4)[:, :3].tobytes()


async def _no_audio() -> AsyncGenerator[bytes]:
    """Yield nothing."""
    nothing: tuple[bytes, ...] = ()
    for chunk in nothing:
        yield chunk


class _Download:
    """One request of a player, which hangs up once it has the bytes it wants."""

    def __init__(self, wanted: int | None = None, stall_at: int | None = None) -> None:
        """
        Initialize the download.

        :param wanted: The bytes after which the player hangs up, None for all it gets.
        :param stall_at: The bytes after which the player stops reading until `resume` is set.
        """
        self._wanted = wanted
        self._stall_at = stall_at
        self.body = bytearray()
        self.transport = Mock()
        self.transport.get_extra_info.return_value = None
        self.transport.is_closing.return_value = False
        self.transport.abort.side_effect = self.hang_up
        self.protocol = Mock()
        self.writer = Mock()
        self.writer.write_headers = AsyncMock()
        self.writer.write = AsyncMock(side_effect=self._write)
        self.writer.write_eof = AsyncMock()
        self.writer.drain = AsyncMock()
        self.stalled = asyncio.Event()
        self.resume = asyncio.Event()

    @property
    def connected(self) -> bool:
        """Return whether the player still holds its connection."""
        return self.protocol.transport is not None

    def hang_up(self) -> None:
        """Close the connection, as a player does when it pauses or seeks."""
        # what aiohttp does as soon as it reads the close
        self.protocol.transport = None

    async def _write(self, data: bytes) -> None:
        if self._stall_at is not None and len(self.body) >= self._stall_at:
            self.stalled.set()
            await self.resume.wait()
        if not self.connected:
            raise ConnectionResetError("Cannot write to closing transport")
        self.body += data
        if self._wanted is not None and len(self.body) >= self._wanted:
            del self.body[self._wanted :]
            self.hang_up()


class _Rig:
    """A streams controller that serves one track from a decoded buffer to one player."""

    def __init__(
        self,
        pcm: bytes,
        *,
        duration: int,
        pcm_format: AudioFormat = PCM_16,
        output_format: AudioFormat = WAV_16,
        filter_params: tuple[str, ...] = (),
        seek_position: int = 0,
    ) -> None:
        """
        Initialize the rig.

        :param pcm: The decoded audio of the whole track.
        :param duration: The duration of the track as its provider gives it.
        :param pcm_format: Format of the decoded audio.
        :param output_format: Format the player gets.
        :param filter_params: The filters of the player.
        :param seek_position: The position the queue item starts at.
        """
        self.pcm = pcm
        self.pcm_format = pcm_format
        self.fmt = output_format.content_type.value
        self.buffer = AudioBuffer(pcm_format)
        self.queue_item = QueueItem(
            queue_id=QUEUE_ID, queue_item_id=ITEM_ID, name=ITEM_ID, duration=duration
        )
        self.queue_item.streamdetails = StreamDetails(
            provider="local--1",
            item_id=ITEM_ID,
            audio_format=AudioFormat(content_type=ContentType.FLAC),
            media_type=MediaType.TRACK,
            stream_type=StreamType.LOCAL_FILE,
            path="/music/item1.flac",
            duration=duration,
            allow_seek=True,
        )
        self.queue_item.streamdetails.seek_position = seek_position
        self.queue_item.streamdetails.buffer = self.buffer
        self.queue_data = PlayerQueueData(
            queue=MagicMock(
                queue_id=QUEUE_ID, display_name="Queue", repeat_mode=RepeatMode.OFF, items=3
            ),
            items=[self.queue_item],
            session_id=SESSION_ID,
        )
        self.player = MagicMock(protocol_id=PLAYER_ID, group_members=[], fetches_files=True)
        # the pacing arguments of every encode that was started
        self.pacing: list[list[str]] = []

        ctrl = self.ctrl = MagicMock()
        ctrl.base_url = BASE_URL
        ctrl._open_item_streams = {}
        ctrl._ended_item_streams = {}
        ctrl._active_output_streams = 0
        ctrl._requested_item_ids.return_value = set()
        ctrl._release_abandoned_item_sources = AsyncMock()
        ctrl._file_streams = FileStreams()
        # bind the real methods: the mocked self would silently swallow them otherwise
        for name in (
            "serves_as_file",
            "stream_url_serves_as_file",
            "_select_item_crossfade_mode",
            "_serve_file",
            "_send_file_part",
            "_encode_file",
        ):
            setattr(ctrl, name, MethodType(getattr(StreamsController, name), ctrl))
        ctrl.get_crossfade_mode.return_value = CrossfadeMode.DISABLED
        ctrl.mass.player_queues.get.return_value = self.queue_data.queue
        ctrl.mass.player_queues.queue_data.return_value = self.queue_data
        ctrl.mass.player_queues.get_item.return_value = self.queue_item
        ctrl.mass.players.get_output_protocol.return_value = self.player
        ctrl.mass.config.get_raw_core_config_value.return_value = 8
        ctrl.audio.select_pcm_format = AsyncMock(return_value=pcm_format)
        ctrl.audio.get_output_format = AsyncMock(return_value=output_format)
        ctrl.audio.fade_into_pending.return_value = False
        ctrl.audio.get_player_output_plan.return_value = MagicMock(
            filter_params=list(filter_params)
        )
        ctrl.audio.get_queue_item_stream = MagicMock(side_effect=self._item_stream)

    @property
    def encodes(self) -> int:
        """Return how many encodes were started."""
        return int(self.ctrl.audio.get_queue_item_stream.call_count)

    async def decode(self, *, finished: bool = True) -> None:
        """
        Put the audio of the track in its buffer.

        :param finished: Whether the source was read out, so the buffer knows it holds it all.
        """
        second = self.pcm_format.pcm_sample_size
        for start in range(0, len(self.pcm), second):
            await self.buffer._put(self.pcm[start : start + second])
        if finished:
            await self.buffer._set_eof()

    def request(
        self,
        byte_range: str | None = None,
        *,
        method: str = "GET",
        wanted: int | None = None,
        stall_at: int | None = None,
    ) -> tuple[web.Request, _Download]:
        """
        Build a request of the player for the track.

        :param byte_range: The value of its Range header, if it has one.
        :param method: The method of the request.
        :param wanted: The bytes after which the player hangs up, None for all it gets.
        :param stall_at: The bytes after which the player stops reading for a while.
        """
        download = _Download(wanted, stall_at)
        request = make_mocked_request(
            method,
            f"/single/{SESSION_ID}/{QUEUE_ID}/{ITEM_ID}/{PLAYER_ID}.{self.fmt}",
            headers={"Range": byte_range} if byte_range else None,
            match_info={
                "session_id": SESSION_ID,
                "queue_id": QUEUE_ID,
                "queue_item_id": ITEM_ID,
                "player_id": PLAYER_ID,
                "fmt": self.fmt,
            },
            writer=download.writer,
            protocol=download.protocol,
            transport=download.transport,
        )
        return request, download

    async def fetch(
        self, byte_range: str | None = None, *, method: str = "GET", wanted: int | None = None
    ) -> tuple[web.StreamResponse, bytes]:
        """
        Let the player fetch the track, or a part of it, and return the response and its body.

        :param byte_range: The value of the Range header, if the request has one.
        :param method: The method of the request.
        :param wanted: The bytes after which the player hangs up, None for all it gets.
        """
        request, download = self.request(byte_range, method=method, wanted=wanted)
        resp = await StreamsController.serve_queue_item_stream(self.ctrl, request)
        return resp, bytes(download.body)

    def _item_stream(
        self, queue_item: QueueItem, pcm_format: AudioFormat, seek_position_ms: int, **kwargs: Any
    ) -> AsyncGenerator[bytes]:
        assert queue_item is self.queue_item
        if not kwargs["consumer_connected"]():
            # nothing is read for a player that has gone, and that is no failure
            return _no_audio()
        return self.buffer.get_stream(
            output_format=pcm_format, seek_position_ms=seek_position_ms, exact_seek=True
        )


@pytest.fixture
def rig() -> _Rig:
    """Return a rig with a track of twelve seconds, which is a little over two megabytes."""
    return _Rig(_tone(12), duration=12)


@pytest.fixture
def passthrough_encoder(rig: _Rig) -> Iterator[None]:
    """Hand the audio to the player as it comes, without an encoder in between."""

    async def _passthrough(
        audio_input: AsyncGenerator[bytes], extra_input_args: list[str], **_kwargs: Any
    ) -> AsyncGenerator[bytes]:
        rig.pacing.append(extra_input_args)
        async with aclosing(audio_input):
            async for chunk in audio_input:
                yield chunk

    with (
        patch("music_assistant.controllers.streams.controller.get_ffmpeg_stream", _passthrough),
        patch("music_assistant.controllers.streams.file_stream.PROBE_GRACE", 0.05),
    ):
        yield


def _burst(pacing: list[str]) -> float:
    """Return the seconds of audio an encode hands over at once when it starts."""
    assert pacing[:2] == ["-readrate", "1.0"]
    assert pacing[4:] == ["-readrate_catchup", "2"]
    assert pacing[2] == "-readrate_initial_burst"
    return float(pacing[3])


@pytest.mark.usefixtures("passthrough_encoder")
async def test_a_plain_request_gets_the_whole_file(rig: _Rig) -> None:
    """A request without a range gets the header and the whole track, with its length."""
    await rig.decode()

    resp, body = await rig.fetch()

    assert resp.status == 200
    assert resp.content_length == len(body) == HEADER_SIZE + len(rig.pcm)
    assert resp.headers["Accept-Ranges"] == "bytes"
    assert resp.headers["contentFeatures.dlna.org"] == DLNA_CONTENT_FEATURES_FILE
    assert resp.headers["Content-Type"].startswith("audio/wav")
    assert "Content-Range" not in resp.headers
    assert body[:4] == b"RIFF"
    assert body[HEADER_SIZE:] == rig.pcm
    rig.ctrl.mass.player_queues.track_loaded_in_buffer.assert_called_once_with(QUEUE_ID, ITEM_ID)
    assert rig.ctrl._active_output_streams == 0
    assert rig.ctrl._open_item_streams == {}


@pytest.mark.usefixtures("passthrough_encoder")
@pytest.mark.parametrize(
    ("byte_range", "first", "last"),
    [
        ("bytes=0-", 0, None),
        # where a Sonos asks to go on, and where a WiiM lands after a seek
        (f"bytes={100 * KIB_16}-", 100 * KIB_16, None),
        ("bytes=1500001-", 1500001, None),
        ("bytes=1800000-1899999", 1800000, 1899999),
        ("bytes=20-99", 20, 99),
        ("bytes=-128", HEADER_SIZE + 12 * BYTES_PER_SECOND - 128, None),
    ],
)
async def test_a_range_request_gets_that_part_of_the_file(
    rig: _Rig, byte_range: str, first: int, last: int | None
) -> None:
    """Every part is cut out of the same file, whatever byte it starts on."""
    await rig.decode()
    _, whole_file = await rig.fetch()
    size = len(whole_file)
    last = size - 1 if last is None else last

    resp, body = await rig.fetch(byte_range)

    assert resp.status == 206
    assert resp.headers["Content-Range"] == f"bytes {first}-{last}/{size}"
    assert resp.headers["Accept-Ranges"] == "bytes"
    assert resp.content_length == last - first + 1
    assert body == whole_file[first : last + 1]


@pytest.mark.usefixtures("passthrough_encoder")
@pytest.mark.parametrize("past_the_end", [0, 5000])
async def test_a_part_at_or_past_the_end_is_refused_with_the_size(
    rig: _Rig, past_the_end: int
) -> None:
    """A range that starts at or past the end gets a 416 that tells the size of the file."""
    await rig.decode()
    size = HEADER_SIZE + len(rig.pcm)

    with pytest.raises(web.HTTPRequestRangeNotSatisfiable) as refusal:
        await rig.fetch(f"bytes={size + past_the_end}-")

    assert refusal.value.headers["Content-Range"] == f"bytes */{size}"
    assert rig.encodes == 0


@pytest.mark.usefixtures("passthrough_encoder")
@pytest.mark.parametrize("byte_range", [None, "bytes=100000-"])
async def test_a_head_request_gets_the_length_without_opening_the_track(
    rig: _Rig, byte_range: str | None
) -> None:
    """A HEAD request is answered like its GET, without a body and without any audio read."""
    await rig.decode()
    size = HEADER_SIZE + len(rig.pcm)

    resp, body = await rig.fetch(byte_range, method="HEAD")

    assert resp.status == (206 if byte_range else 200)
    assert resp.content_length == (size - 100000 if byte_range else size)
    assert resp.headers["Accept-Ranges"] == "bytes"
    assert body == b""
    assert rig.encodes == 0
    rig.ctrl.audio.claim_item.assert_not_called()


def _play(rig: _Rig, queue_item: QueueItem) -> None:
    """
    Let the queue of the rig play the given item, and run what the stream hands to the loop.

    :param rig: The rig.
    :param queue_item: The item the queue plays now.
    """
    rig.queue_data.queue.current_item = queue_item
    rig.ctrl.mass.loop = asyncio.get_running_loop()


@pytest.mark.usefixtures("passthrough_encoder")
@pytest.mark.parametrize("byte_range", [None, f"bytes={100 * KIB_16}-", "bytes=-128"])
async def test_a_player_that_asks_again_for_the_file_it_plays_is_heard(
    rig: _Rig, byte_range: str | None
) -> None:
    """A player that comes back for the file of the item it plays has resumed or seeked."""
    await rig.decode()
    _play(rig, rig.queue_item)

    await rig.fetch()
    await asyncio.sleep(0)
    rig.player.on_file_requested_again.assert_not_called()

    await rig.fetch(byte_range)
    await asyncio.sleep(0)
    rig.player.on_file_requested_again.assert_called_once_with()


@pytest.mark.usefixtures("passthrough_encoder")
async def test_a_file_fetched_ahead_again_is_no_resume_or_seek(rig: _Rig) -> None:
    """A player that asks again for the file of the next item does not resume or seek."""
    await rig.decode()
    _play(rig, QueueItem(queue_id=QUEUE_ID, queue_item_id="item0", name="item0", duration=60))

    await rig.fetch()
    await rig.fetch("bytes=-128")
    await asyncio.sleep(0)

    rig.player.on_file_requested_again.assert_not_called()


@pytest.mark.usefixtures("passthrough_encoder")
async def test_a_head_or_refused_request_again_is_no_resume_or_seek(rig: _Rig) -> None:
    """A HEAD request and a part past the end that gets a 416 do not count as a request again."""
    await rig.decode()
    _play(rig, rig.queue_item)
    await rig.fetch()

    await rig.fetch(f"bytes={100 * KIB_16}-", method="HEAD")
    with pytest.raises(web.HTTPRequestRangeNotSatisfiable):
        await rig.fetch(f"bytes={HEADER_SIZE + len(rig.pcm)}-")
    await asyncio.sleep(0)

    rig.player.on_file_requested_again.assert_not_called()


@pytest.mark.usefixtures("passthrough_encoder")
async def test_a_player_that_does_not_fetch_files_keeps_its_stream(rig: _Rig) -> None:
    """Without the property a range is not answered, and no length or part is announced."""
    await rig.decode()
    rig.player.fetches_files = False
    rig.player.requires_content_length = False

    resp, _ = await rig.fetch("bytes=100000-", method="HEAD")

    assert resp.status == 200
    assert resp.content_length is None
    assert resp.headers["Accept-Ranges"] == "none"
    assert resp.headers["contentFeatures.dlna.org"] == DLNA_CONTENT_FEATURES
    assert "Content-Range" not in resp.headers


@pytest.mark.usefixtures("passthrough_encoder")
async def test_a_track_that_ends_short_of_its_declared_length(rig: _Rig) -> None:
    """The body ends where the audio ends, and from then on the file has its real size."""
    rig.pcm = rig.pcm[: round(11.6 * BYTES_PER_SECOND)]
    # not decoded to its end yet, so the file declares the duration plus a second
    await rig.decode(finished=False)
    declared = HEADER_SIZE + 13 * BYTES_PER_SECOND
    real = HEADER_SIZE + len(rig.pcm)

    head, _ = await rig.fetch(method="HEAD")
    await rig.buffer._set_eof()
    resp, body = await rig.fetch()

    assert head.content_length == resp.content_length == declared
    assert len(body) == real
    assert body[HEADER_SIZE:] == rig.pcm
    # no silence was added, so the connection is closed on the missing rest
    assert not resp.keep_alive

    for start in (real, declared - 1):
        with pytest.raises(web.HTTPRequestRangeNotSatisfiable) as refusal:
            await rig.fetch(f"bytes={start}-")
        assert refusal.value.headers["Content-Range"] == f"bytes */{real}"

    resp, tail = await rig.fetch(f"bytes={real - 5000}-")
    assert resp.headers["Content-Range"] == f"bytes {real - 5000}-{real - 1}/{real}"
    assert tail == body[-5000:]


@pytest.mark.usefixtures("passthrough_encoder")
async def test_a_part_that_starts_past_the_real_end_finds_that_end(rig: _Rig) -> None:
    """A player that asks for a part the audio never reaches gets nothing, and then the size."""
    rig.pcm = rig.pcm[: round(11.6 * BYTES_PER_SECOND)]
    await rig.decode(finished=False)
    await rig.fetch(method="HEAD")
    await rig.buffer._set_eof()
    real = HEADER_SIZE + len(rig.pcm)

    _, body = await rig.fetch(f"bytes={real + 40000}-")

    assert body == b""
    with pytest.raises(web.HTTPRequestRangeNotSatisfiable) as refusal:
        await rig.fetch(f"bytes={real}-")
    assert refusal.value.headers["Content-Range"] == f"bytes */{real}"


@pytest.mark.usefixtures("passthrough_encoder")
async def test_more_audio_than_declared_is_cut_off() -> None:
    """A track that runs longer than its file declared ends at the declared length."""
    rig = _Rig(_tone(12), duration=10)
    await rig.decode(finished=False)
    declared = HEADER_SIZE + 11 * BYTES_PER_SECOND
    # the file is made before the track is decoded to its end
    await rig.fetch(method="HEAD")
    await rig.buffer._set_eof()

    resp, body = await rig.fetch()

    assert resp.content_length == len(body) == declared
    assert body[HEADER_SIZE:] == rig.pcm[: 11 * BYTES_PER_SECOND]


@pytest.mark.usefixtures("passthrough_encoder")
@pytest.mark.usefixtures("passthrough_encoder")
@pytest.mark.parametrize(
    ("byte_range", "wanted"),
    [
        # the header of the file, and gone
        (None, HEADER_SIZE),
        # gone before anything of the part was sent
        (f"bytes={HEADER_SIZE + 6 * BYTES_PER_SECOND}-", 0),
    ],
)
async def test_a_request_of_a_player_that_has_gone_keeps_the_size(
    rig: _Rig, byte_range: str | None, wanted: int
) -> None:
    """An encode that reads nothing, because its player hung up, is not the end of the track."""
    await rig.decode()
    size = HEADER_SIZE + len(rig.pcm)
    request, download = rig.request(byte_range, wanted=wanted)
    if not wanted:
        download.hang_up()

    await StreamsController.serve_queue_item_stream(rig.ctrl, request)

    assert rig.encodes == 1

    resp, _ = await rig.fetch(method="HEAD")
    assert resp.content_length == size
    _, body = await rig.fetch(f"bytes={size - 5000}-")
    assert body == rig.pcm[-5000:]


async def test_a_track_that_stops_before_its_end_keeps_its_declared_size(rig: _Rig) -> None:
    """Audio that is taken away is not the end of the track."""
    await rig.decode(finished=False)
    declared = HEADER_SIZE + 13 * BYTES_PER_SECOND
    request, download = rig.request(stall_at=3 * BYTES_PER_SECOND)
    task = asyncio.create_task(StreamsController.serve_queue_item_stream(rig.ctrl, request))
    await download.stalled.wait()

    # the queue gives up the audio of the track while the player reads it
    await rig.buffer.clear()
    download.resume.set()
    await task

    assert len(download.body) < declared
    resp, _ = await rig.fetch(method="HEAD")
    assert resp.content_length == declared


@pytest.mark.usefixtures("passthrough_encoder")
async def test_a_source_that_fails_ends_the_body_and_leaves_the_declared_size(rig: _Rig) -> None:
    """Audio that breaks off is reported, and says nothing about where the track ends."""
    await rig.decode()
    size = HEADER_SIZE + len(rig.pcm)

    async def _failing_stream(**_kwargs: Any) -> AsyncGenerator[bytes]:
        yield rig.pcm[:BYTES_PER_SECOND]
        raise AudioError("the source broke off")

    rig.ctrl.audio.get_queue_item_stream = MagicMock(side_effect=_failing_stream)

    resp, body = await rig.fetch()

    assert len(body) == HEADER_SIZE + BYTES_PER_SECOND
    assert not resp.keep_alive
    rig.ctrl.logger.error.assert_called_once()
    assert rig.ctrl._active_output_streams == 0
    resp, _ = await rig.fetch(method="HEAD")
    assert resp.content_length == size


@pytest.mark.usefixtures("passthrough_encoder")
async def test_a_file_of_an_item_that_starts_at_a_position() -> None:
    """After a seek by Music Assistant the file is the rest of the track."""
    rig = _Rig(_tone(12), duration=12, seek_position=5)
    await rig.decode()
    rest = rig.pcm[5 * BYTES_PER_SECOND :]

    resp, body = await rig.fetch()
    _, part = await rig.fetch(f"bytes={HEADER_SIZE + 4 * BYTES_PER_SECOND + 2}-")

    assert resp.content_length == HEADER_SIZE + len(rest)
    assert body[HEADER_SIZE:] == rest
    assert part == rest[4 * BYTES_PER_SECOND + 2 :]


@pytest.mark.usefixtures("passthrough_encoder")
async def test_a_second_request_ends_the_encode_of_the_first(rig: _Rig) -> None:
    """A player that asks for another part has left the part it was reading."""
    await rig.decode()
    first_request, first = rig.request(stall_at=3 * BYTES_PER_SECOND)
    task = asyncio.create_task(StreamsController.serve_queue_item_stream(rig.ctrl, first_request))
    await first.stalled.wait()
    first.transport.abort.assert_not_called()

    resp, body = await rig.fetch(f"bytes={100 * KIB_16}-")

    first.transport.abort.assert_called_once()
    first.resume.set()
    first_resp = await task
    assert resp.content_length == len(body)
    assert len(first.body) < HEADER_SIZE + len(rig.pcm)
    assert not first_resp.keep_alive
    assert rig.encodes == 2
    assert rig.ctrl._active_output_streams == 0


@pytest.mark.usefixtures("passthrough_encoder")
async def test_the_start_of_a_file_is_answered_from_memory(rig: _Rig) -> None:
    """A player that asks for the start again and drops that request costs no encode."""
    await rig.decode()
    _, whole_file = await rig.fetch()

    # a Sonos asks from the start before it resumes or seeks, and hangs up at once
    _, probe = await rig.fetch(wanted=KIB_16)
    # a WiiM reads up to 750 KiB of the start first
    _, read_ahead = await rig.fetch("bytes=0-", wanted=750 * 1024)
    _, inside = await rig.fetch("bytes=300000-399999")

    assert probe == whole_file[:KIB_16]
    assert read_ahead == whole_file[: 750 * 1024]
    assert inside == whole_file[300000:400000]
    assert rig.encodes == 1


@pytest.mark.usefixtures("passthrough_encoder")
async def test_the_queue_is_told_of_every_part_that_is_encoded(rig: _Rig) -> None:
    """A player that seeks or plays a track again gets its next track handed over again."""
    await rig.decode()
    loaded = rig.ctrl.mass.player_queues.track_loaded_in_buffer
    await rig.fetch()
    assert loaded.call_count == 1

    # the start again, dropped at once: nothing is encoded for it
    await rig.fetch(wanted=KIB_16)
    assert loaded.call_count == 1

    await rig.fetch(f"bytes={100 * KIB_16}-")
    await rig.fetch()

    assert loaded.call_count == 3
    loaded.assert_called_with(QUEUE_ID, ITEM_ID)


@pytest.mark.usefixtures("passthrough_encoder")
async def test_a_player_that_reads_on_past_the_start_gets_an_encode(rig: _Rig) -> None:
    """What memory does not hold is encoded for a player that stays."""
    await rig.decode()
    _, whole_file = await rig.fetch()

    _, again = await rig.fetch()
    _, from_inside = await rig.fetch("bytes=1000000-")

    assert again == whole_file
    assert from_inside == whole_file[1000000:]
    assert rig.encodes == 3


@pytest.mark.usefixtures("passthrough_encoder")
async def test_a_restart_goes_on_when_the_request_before_it_ends(rig: _Rig) -> None:
    """A player that starts the file over and then drops the request it had gets all of it."""
    await rig.decode()
    first_request, first = rig.request(stall_at=3 * BYTES_PER_SECOND)
    first_task = asyncio.create_task(
        StreamsController.serve_queue_item_stream(rig.ctrl, first_request)
    )
    await first.stalled.wait()

    restart_request, restart = rig.request("bytes=0-")
    restart_task = asyncio.create_task(
        StreamsController.serve_queue_item_stream(rig.ctrl, restart_request)
    )
    await asyncio.sleep(0.01)
    first.hang_up()
    first.resume.set()
    await first_task
    await restart_task

    assert bytes(restart.body[HEADER_SIZE:]) == rig.pcm
    assert rig.encodes == 2


@pytest.mark.usefixtures("passthrough_encoder")
async def test_a_request_that_waits_after_the_start_leaves_the_file_to_a_newer_one(
    rig: _Rig,
) -> None:
    """A player that asks for its real part while its request for the start is open gets that."""
    await rig.decode()
    _, whole_file = await rig.fetch()
    start_request, start = rig.request("bytes=0-")
    task = asyncio.create_task(StreamsController.serve_queue_item_stream(rig.ctrl, start_request))
    await asyncio.sleep(0.01)

    part_request, part = rig.request(f"bytes={100 * KIB_16}-", stall_at=0)
    part_task = asyncio.create_task(
        StreamsController.serve_queue_item_stream(rig.ctrl, part_request)
    )
    await task

    # the request for the start went no further than memory, and left the other one alone
    assert len(start.body) == 1024 * 1024
    assert part.connected
    part.resume.set()
    await part_task
    assert bytes(part.body) == whole_file[100 * KIB_16 :]
    assert rig.encodes == 2


@pytest.mark.usefixtures("passthrough_encoder")
async def test_every_encode_is_paced_with_a_head_start_out_of_the_files_budget(rig: _Rig) -> None:
    """A file hands out thirty seconds of head start, and the dropped run-in costs none of it."""
    await rig.decode()
    seek = HEADER_SIZE + 6 * BYTES_PER_SECOND + BYTES_PER_SECOND // 2

    await rig.fetch(wanted=KIB_16)
    for _ in range(6):
        await rig.fetch(f"bytes={seek}-", wanted=KIB_16)

    # an encode starts a whole second before its part: 1.5 s that the player never gets
    assert [_burst(pacing) for pacing in rig.pacing] == [6, 7.5, 7.5, 7.5, 7.5, 1.5, 1.5]


@pytest.mark.usefixtures("passthrough_encoder")
async def test_an_encode_reads_the_track_a_second_before_its_part(rig: _Rig) -> None:
    """The audio before a part lets the filters settle, and is read from the exact position."""
    rig.queue_item.streamdetails.seek_position = 3  # type: ignore[union-attr]
    await rig.decode()

    await rig.fetch(f"bytes={HEADER_SIZE + 6 * BYTES_PER_SECOND + 100}-", wanted=KIB_16)

    stream = rig.ctrl.audio.get_queue_item_stream.call_args.kwargs
    assert stream["seek_position_ms"] == (3 + 5) * 1000
    assert stream["session_id"] == SESSION_ID


@pytest.mark.usefixtures("passthrough_encoder")
async def test_a_stream_ends_when_its_session_is_no_longer_current(rig: _Rig) -> None:
    """A file stream stops like any other when the queue moves to another session."""
    await rig.decode()
    request, download = rig.request(stall_at=3 * BYTES_PER_SECOND)
    task = asyncio.create_task(StreamsController.serve_queue_item_stream(rig.ctrl, request))
    await download.stalled.wait()

    rig.queue_data.session_id = "sess2"
    download.resume.set()
    resp = await task

    assert len(download.body) < HEADER_SIZE + len(rig.pcm)
    assert not resp.keep_alive


def _repeat(rig: _Rig, repeat_mode: RepeatMode, *, crossfade: bool, items: int = 1) -> None:
    """
    Let the queue of a rig repeat.

    :param rig: The rig.
    :param repeat_mode: What the queue repeats.
    :param crossfade: Whether the queue fades its tracks into each other.
    :param items: The number of items of the queue.
    """
    queue = rig.queue_data.queue
    queue.repeat_mode, queue.items = repeat_mode, items
    if crossfade:
        rig.ctrl.get_crossfade_mode.return_value = CrossfadeMode.STANDARD_CROSSFADE


@pytest.mark.parametrize(
    ("change", "served_as_file"),
    [
        (lambda _rig: None, True),
        (lambda rig: setattr(rig.player, "fetches_files", False), False),
        (lambda rig: setattr(rig.queue_item, "duration", None), False),
        (lambda rig: setattr(rig.queue_item.streamdetails, "media_type", MediaType.RADIO), False),
        (lambda rig: rig.queue_item.extra_attributes.update(playback_speed=1.25), False),
        (lambda rig: rig.queue_item.extra_attributes.update(playback_speed=1.0), True),
        # a track that is mixed with its neighbours is a file as well
        (
            lambda rig: setattr(
                rig.ctrl.get_crossfade_mode, "return_value", CrossfadeMode.STANDARD_CROSSFADE
            ),
            True,
        ),
        (
            lambda rig: setattr(
                rig.ctrl.get_crossfade_mode, "return_value", CrossfadeMode.SMART_CROSSFADE
            ),
            True,
        ),
        (lambda rig: setattr(rig.ctrl.audio.fade_into_pending, "return_value", True), True),
        # a track that repeats is a file, unless the play before it left its mixes in that file
        (lambda rig: _repeat(rig, RepeatMode.ONE, crossfade=False), True),
        (lambda rig: _repeat(rig, RepeatMode.ONE, crossfade=True), False),
        (lambda rig: _repeat(rig, RepeatMode.ALL, crossfade=True, items=2), False),
        (lambda rig: _repeat(rig, RepeatMode.ALL, crossfade=True, items=3), True),
        (lambda rig: setattr(rig.queue_item.streamdetails, "fade_in", True), False),
        (lambda rig: setattr(rig.queue_item.streamdetails, "tail_overlap", Mock()), False),
        (lambda rig: setattr(rig.queue_item.streamdetails, "allow_seek", False), False),
        # the next track is asked about before its stream details are there
        (lambda rig: setattr(rig.queue_item, "streamdetails", None), False),
        (lambda rig: setattr(rig.ctrl.mass.player_queues.get, "return_value", None), False),
    ],
    ids=[
        "track",
        "player-fetches-no-files",
        "no-duration",
        "radio",
        "playback-speed",
        "normal-speed",
        "crossfade",
        "smart-fade",
        "fade-into-the-item-pending",
        "repeat-one",
        "repeat-one-with-crossfade",
        "repeat-of-two-with-crossfade",
        "repeat-of-three-with-crossfade",
        "fade-in",
        "voice-over",
        "not-seekable",
        "no-stream-details",
        "no-queue",
    ],
)
def test_what_is_served_as_a_file(rig: _Rig, change: Any, served_as_file: bool) -> None:
    """Only a track at normal speed, without a fade-in or a voice-over, is served as a file."""
    change(rig)

    assert rig.ctrl.serves_as_file(rig.player, rig.queue_item, ContentType.WAV) is served_as_file


@pytest.mark.parametrize(
    ("codec", "served_as_file"),
    [
        (ContentType.WAV, True),
        (ContentType.MP3, True),
        (ContentType.FLAC, False),
        (ContentType.AAC, False),
        (ContentType.PCM_S16LE, False),
    ],
)
def test_only_wav_and_mp3_are_served_as_a_file(
    rig: _Rig, codec: ContentType, served_as_file: bool
) -> None:
    """A codec whose size is not known up front keeps today's stream."""
    assert rig.ctrl.serves_as_file(rig.player, rig.queue_item, codec) is served_as_file


def test_a_track_without_stream_details_can_be_served_as_a_file(rig: _Rig) -> None:
    """A track that is enqueued before its stream details are resolved counts as a file."""
    rig.queue_item.media_item = MagicMock(media_type=MediaType.TRACK)
    rig.queue_item.streamdetails = None

    assert rig.ctrl.serves_as_file(rig.player, rig.queue_item, ContentType.WAV)


@pytest.mark.parametrize(
    ("url", "served_as_file"),
    [
        (f"{BASE_URL}/single/{SESSION_ID}/{QUEUE_ID}/{ITEM_ID}/{PLAYER_ID}.wav", True),
        (f"{BASE_URL}/single/{SESSION_ID}/{QUEUE_ID}/{ITEM_ID}/{PLAYER_ID}.mp3", True),
        (f"{BASE_URL}/single/{SESSION_ID}/{QUEUE_ID}/{ITEM_ID}/{PLAYER_ID}.flac", False),
        (f"{BASE_URL}/flow/{SESSION_ID}/{QUEUE_ID}/{ITEM_ID}/{PLAYER_ID}.wav", False),
        (f"{BASE_URL}/single/{SESSION_ID}/{QUEUE_ID}/pos/3/{PLAYER_ID}.wav", True),
        (f"{BASE_URL}/single/{SESSION_ID}/{QUEUE_ID}/pos/x/{PLAYER_ID}.wav", False),
        (f"{BASE_URL}/announcement/{PLAYER_ID}.wav", False),
        ("http://radio.example.com/stream.wav", False),
    ],
    ids=[
        "single-wav",
        "single-mp3",
        "single-flac",
        "flow",
        "queue-position",
        "no-position",
        "announcement",
        "foreign",
    ],
)
def test_a_stream_url_tells_whether_it_is_answered_as_a_file(
    rig: _Rig, url: str, served_as_file: bool
) -> None:
    """Whoever hands a URL to a player can tell beforehand how it will be answered."""
    assert rig.ctrl.stream_url_serves_as_file(url) is served_as_file


def test_a_stream_url_of_an_unknown_player_or_item_is_no_file(rig: _Rig) -> None:
    """A URL that names a player or an item that is gone is not answered as a file."""
    url = f"{BASE_URL}/single/{SESSION_ID}/{QUEUE_ID}/{ITEM_ID}/{PLAYER_ID}.wav"

    rig.ctrl.mass.players.get_output_protocol.return_value = None
    assert not rig.ctrl.stream_url_serves_as_file(url)

    rig.ctrl.mass.players.get_output_protocol.return_value = rig.player
    rig.ctrl.mass.player_queues.get_item.return_value = None
    assert not rig.ctrl.stream_url_serves_as_file(url)


async def test_a_renderer_fetches_a_file_over_a_real_connection(rig: _Rig) -> None:
    """A renderer that hangs up, comes back for a part and asks again is answered as meant."""
    await rig.decode()

    async def _encoder(audio_input: AsyncGenerator[bytes], **_kwargs: Any) -> AsyncGenerator[bytes]:
        # in pieces and with a pause, so that a request is still being answered when the next comes
        async with aclosing(audio_input):
            async for chunk in audio_input:
                for start in range(0, len(chunk), KIB_16):
                    yield chunk[start : start + KIB_16]
                    await asyncio.sleep(0.002)

    async def _route(request: web.Request) -> web.StreamResponse:
        return await StreamsController.serve_queue_item_stream(rig.ctrl, request)

    app = web.Application()
    app.router.add_route(
        "*", "/single/{session_id}/{queue_id}/{queue_item_id}/{player_id}.{fmt}", _route
    )
    path = f"/single/{SESSION_ID}/{QUEUE_ID}/{ITEM_ID}/{PLAYER_ID}.wav"
    with patch("music_assistant.controllers.streams.controller.get_ffmpeg_stream", _encoder):
        async with TestServer(app) as server, aiohttp.ClientSession() as renderer:
            url = str(server.make_url(path))
            async with renderer.get(url) as resp:
                whole_file = await resp.read()
                assert resp.status == 200
                assert resp.content_length == len(whole_file) == HEADER_SIZE + len(rig.pcm)

            # the start again, dropped at once: a Sonos before it resumes or seeks
            async with renderer.get(url) as resp:
                assert await resp.content.readexactly(KIB_16) == whole_file[:KIB_16]
            # the start again, read for a while, then the part it wants: a WiiM that seeks
            async with renderer.get(url, headers={"Range": "bytes=0-"}) as resp:
                assert resp.status == 206
                await resp.content.readexactly(750 * 1024)
            await asyncio.sleep(0.5)
            assert rig.encodes == 1

            reading = await renderer.get(url, headers={"Range": f"bytes={100 * KIB_16}-"})
            assert await reading.content.readexactly(KIB_16)
            async with renderer.get(url, headers={"Range": f"bytes={110 * KIB_16}-"}) as resp:
                assert resp.headers["Content-Range"] == (
                    f"bytes {110 * KIB_16}-{len(whole_file) - 1}/{len(whole_file)}"
                )
                assert await resp.read() == whole_file[110 * KIB_16 :]
            # the part it was reading before ended there, short of its length
            with pytest.raises(aiohttp.ClientPayloadError):
                await reading.content.read()
            reading.close()
            assert rig.encodes == 3

            async with renderer.get(url, headers={"Range": f"bytes={len(whole_file)}-"}) as resp:
                assert resp.status == 416
                assert resp.headers["Content-Range"] == f"bytes */{len(whole_file)}"
            async with renderer.head(url) as resp:
                assert resp.status == 200
                assert resp.content_length == len(whole_file)
            assert rig.encodes == 3
    assert rig.ctrl._active_output_streams == 0
    assert rig.ctrl._open_item_streams == {}


def _samples(pcm: bytes) -> np.ndarray[Any, np.dtype[np.int32]]:
    """Return 16 bit audio as numbers."""
    return np.frombuffer(pcm, dtype="<i2").astype(np.int32)


# The most two encodes of the same audio differ by when they go from more bits to 16: each
# adds its own dither. A filter that has not settled at a join differs by fifty times that.
DITHER = 3

JOIN_CASES = {
    # nothing between the track and the player: the same bytes
    "source-format": (PCM_16, WAV_16, (), 0),
    "dither": (
        AudioFormat(content_type=ContentType.PCM_S24LE, sample_rate=48000, bit_depth=24),
        AudioFormat(content_type=ContentType.WAV, sample_rate=48000, bit_depth=16),
        (),
        DITHER,
    ),
    # filters carry what they heard before, which is what the run-in is for
    "equalizer": (
        AudioFormat(content_type=ContentType.PCM_S24LE, sample_rate=44100, bit_depth=24),
        WAV_16,
        ("lowshelf=f=120:g=4", "equalizer=f=1000:t=q:w=1.4:g=-5", "highshelf=f=8000:g=3"),
        DITHER,
    ),
    # a resampler does too, and an encode starts on a frame of both sample rates
    "resampled": (
        AudioFormat(content_type=ContentType.PCM_S24LE, sample_rate=96000, bit_depth=24),
        AudioFormat(content_type=ContentType.WAV, sample_rate=44100, bit_depth=16),
        (),
        DITHER,
    ),
}


@pytest.mark.parametrize("case", JOIN_CASES)
async def test_parts_fetched_like_a_sonos_join_into_one_download(case: str) -> None:
    """
    The same offset always gives the same audio, whichever request it comes from.

    A Sonos puts the bytes of different requests next to each other at offsets it chooses:
    it keeps the start of the file, fetches a part further on for a seek ahead, and later
    asks to go on where its start ends, all on 16 KiB boundaries.
    """
    pcm_format, output_format, filter_params, tolerance = JOIN_CASES[case]
    await check_ffmpeg_version()
    pcm = _tone(20, pcm_format)
    size = HEADER_SIZE + 20 * output_format.pcm_sample_size
    kept, ahead = 30 * KIB_16, 150 * KIB_16
    assert kept < ahead < size

    async def _rig() -> _Rig:
        rig = _Rig(
            pcm,
            duration=20,
            pcm_format=pcm_format,
            output_format=output_format,
            filter_params=filter_params,
        )
        await rig.decode()
        return rig

    # the pace is not what this is about, and would make the test last as long as the track
    with patch(
        "music_assistant.controllers.streams.controller.output_pacing_args", return_value=[]
    ):
        resp, download = await (await _rig()).fetch()
        assert resp.content_length == len(download) == size

        sonos = await _rig()
        _, start = await sonos.fetch(wanted=kept)
        _, seek_ahead = await sonos.fetch(f"bytes={ahead}-")
        _, between = await sonos.fetch(f"bytes={kept}-", wanted=ahead - kept)
        assert sonos.encodes == 3

    joined = start + between + seek_ahead
    assert len(joined) == size
    assert joined[:HEADER_SIZE] == download[:HEADER_SIZE]
    if tolerance == 0:
        assert joined == download
        return
    difference = np.abs(_samples(joined[HEADER_SIZE:]) - _samples(download[HEADER_SIZE:]))
    assert difference.max() <= tolerance


@pytest.fixture
async def unpaced_encoder() -> AsyncIterator[list[list[str]]]:
    """
    Encode for real, as fast as the audio comes: at its pace a test lasts as long as its track.

    Yields the pacing arguments of every encode that was started.
    """
    await check_ffmpeg_version()
    pacing: list[list[str]] = []

    async def _unpaced(*, extra_input_args: list[str], **kwargs: Any) -> AsyncGenerator[bytes]:
        pacing.append(extra_input_args)
        async with aclosing(get_ffmpeg_stream(**kwargs)) as encoded:
            async for chunk in encoded:
                yield chunk

    with (
        patch("music_assistant.controllers.streams.controller.get_ffmpeg_stream", _unpaced),
        patch("music_assistant.controllers.streams.file_stream.PROBE_GRACE", 0.05),
    ):
        yield pacing


async def _encode_mp3(pcm: bytes, pcm_format: AudioFormat = PCM_16) -> bytes:
    """
    Encode audio to MP3 in one go, with the encoder of a track that is served as a file.

    :param pcm: The audio.
    :param pcm_format: Format of the audio.
    """
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-loglevel",
        "error",
        *("-f", pcm_format.content_type.value, "-ar", str(pcm_format.sample_rate), "-ac", "2"),
        *("-i", "-", "-f", "mp3", "-b:a", "320k"),
        *("-write_xing", "0", "-id3v2_version", "0", "-reservoir", "0", "-"),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    encoded, _ = await process.communicate(pcm)
    assert process.returncode == 0
    return encoded


def _mp3_frames(mp3: bytes, sample_rate: int) -> list[int]:
    """
    Return where every frame of an MP3 starts, as the frames themselves tell.

    :param mp3: An MP3 that is frames of 320 kbit/s from its first byte to its last.
    :param sample_rate: Sample rate of the MP3.
    """
    offsets: list[int] = []
    offset = 0
    while offset < len(mp3):
        header = mp3[offset : offset + 3]
        # MPEG-1 layer 3 at 320 kbit/s
        assert header[0] == 0xFF
        assert header[1] & 0xFE == 0xFA
        assert header[2] >> 4 == 14
        offsets.append(offset)
        # a frame says itself whether it is padded with a byte
        offset += 144 * 320000 // sample_rate + (header[2] >> 1 & 1)
    assert offset == len(mp3)
    return offsets


@pytest.mark.usefixtures("unpaced_encoder")
@pytest.mark.parametrize(
    ("pcm_format", "output_format", "samples"),
    [
        (PCM_16, MP3_44K, 460 * 1152),
        # audio that only just starts a frame gets that frame, and the one the encoder ends with
        (PCM_16, MP3_44K, 460 * 1152 + 20),
        (PCM_16, MP3_44K, 460 * 1152 + 600),
        (
            AudioFormat(content_type=ContentType.PCM_S16LE, sample_rate=48000, bit_depth=16),
            MP3_48K,
            500 * 1152 + 20,
        ),
    ],
    ids=["whole-frames", "just-past-a-frame", "half-a-frame", "48k"],
)
async def test_an_mp3_file_declares_the_size_it_is_encoded_to(
    pcm_format: AudioFormat, output_format: AudioFormat, samples: int
) -> None:
    """The size of an MP3 is known up front: its frames at a constant bit rate, and no more."""
    rate = output_format.sample_rate
    rig = _Rig(
        _tone(13, pcm_format)[: samples * 4],
        duration=samples // rate,
        pcm_format=pcm_format,
        output_format=output_format,
    )
    await rig.decode()
    encoded = await _encode_mp3(rig.pcm, pcm_format)

    head, _ = await rig.fetch(method="HEAD")
    resp, body = await rig.fetch()

    assert resp.status == 200
    assert head.content_length == resp.content_length == len(encoded)
    assert body == encoded
    assert resp.keep_alive
    assert resp.headers["Content-Type"].startswith("audio/mpeg")
    assert resp.headers["Accept-Ranges"] == "bytes"
    assert resp.headers["contentFeatures.dlna.org"] == DLNA_CONTENT_FEATURES_FILE
    # no tag and no Xing header in front: every frame is one of the audio, where it should be
    frames = _mp3_frames(body, rate)
    assert len(frames) == mp3_frame_count(samples)
    assert frames == [mp3_frame_offset(frame, rate) for frame in range(len(frames))]
    assert b"Xing" not in body[: frames[1]]
    assert b"Info" not in body[: frames[1]]


async def test_a_part_from_the_middle_of_an_mp3(unpaced_encoder: list[list[str]]) -> None:
    """A part is the output of an encode that starts on a block of frames before it."""
    rig = _Rig(_tone(12), duration=12, output_format=MP3_44K, seek_position=3)
    await rig.decode()
    encoded = await _encode_mp3(rig.pcm[3 * BYTES_PER_SECOND :])
    # in frame 287: the block that starts on frame 245 leaves it more than a second
    first, last = 300001, 349999

    resp, body = await rig.fetch(f"bytes={first}-{last}")

    assert resp.status == 206
    assert resp.headers["Content-Range"] == f"bytes {first}-{last}/{len(encoded)}"
    assert resp.content_length == last - first + 1
    assert body == encoded[first : last + 1]
    stream = rig.ctrl.audio.get_queue_item_stream.call_args.kwargs
    assert stream["seek_position_ms"] == 3000 + 5 * MP3_BLOCK_MS
    # the output that is dropped costs the player none of its head start
    dropped = (first - 5 * MP3_BLOCK_SIZE) / MP3_BYTES_PER_SECOND
    assert [_burst(pacing) for pacing in unpaced_encoder] == [pytest.approx(6 + dropped, abs=0.001)]
    rig.ctrl.mass.player_queues.track_loaded_in_buffer.assert_called_once_with(QUEUE_ID, ITEM_ID)


@pytest.mark.parametrize(
    ("byte_range", "length"),
    [("bytes={tail}-", 128), ("bytes=-128", 128), ("bytes={near_end}-{end}", 50)],
    ids=["from-the-tag", "last-bytes", "inside-the-tag"],
)
async def test_the_last_bytes_of_an_mp3_are_answered_without_its_audio(
    unpaced_encoder: list[list[str]], byte_range: str, length: int
) -> None:
    """A renderer looks for a tag at the end of an MP3 before it plays or seeks."""
    rig = _Rig(_tone(12), duration=12, output_format=MP3_44K)
    # nothing of the track is decoded yet, so the file declares the duration plus a second
    size = mp3_frame_offset(mp3_frame_count(13 * 44100), 44100)

    resp, body = await rig.fetch(
        byte_range.format(tail=size - 128, near_end=size - 100, end=size - 51)
    )

    assert resp.status == 206
    assert resp.headers["Content-Range"].endswith(f"/{size}")
    assert resp.content_length == len(body) == length
    assert body == bytes(length)
    assert rig.encodes == 0
    assert unpaced_encoder == []
    rig.ctrl.mass.player_queues.track_loaded_in_buffer.assert_not_called()


@pytest.mark.usefixtures("unpaced_encoder")
async def test_an_mp3_that_ends_short_of_its_declared_length() -> None:
    """The body ends with the last frame of the audio, and from then on the file has that size."""
    rig = _Rig(_tone(11.6), duration=12, output_format=MP3_44K)
    # not decoded to its end yet, so the file declares the duration plus a second
    await rig.decode(finished=False)
    declared = mp3_frame_offset(mp3_frame_count(13 * 44100), 44100)
    encoded = await _encode_mp3(rig.pcm)
    real = len(encoded)

    head, _ = await rig.fetch(method="HEAD")
    await rig.buffer._set_eof()
    resp, body = await rig.fetch()

    assert head.content_length == resp.content_length == declared
    assert body == encoded
    # no silence was added, so the connection is closed on the missing rest
    assert not resp.keep_alive

    for start in (real, declared - 1):
        with pytest.raises(web.HTTPRequestRangeNotSatisfiable) as refusal:
            await rig.fetch(f"bytes={start}-")
        assert refusal.value.headers["Content-Range"] == f"bytes */{real}"
    resp, _ = await rig.fetch(method="HEAD")
    assert resp.content_length == real


@pytest.mark.usefixtures("unpaced_encoder")
async def test_a_look_for_a_tag_at_the_end_of_the_size_an_mp3_declared() -> None:
    """A renderer that got the declared size with the start looks for a tag at its end."""
    rig = _Rig(_tone(11.6), duration=12, output_format=MP3_44K)
    # not decoded to its end yet, so the file declares the duration plus a second
    await rig.decode(finished=False)
    declared = mp3_frame_offset(mp3_frame_count(13 * 44100), 44100)
    head, _ = await rig.fetch(method="HEAD")
    await rig.buffer._set_eof()
    _, body = await rig.fetch()
    encodes = rig.encodes

    resp, tag = await rig.fetch(f"bytes={declared - 128}-")

    assert head.content_length == declared
    assert len(body) < declared
    assert resp.status == 206
    assert resp.headers["Content-Range"] == f"bytes {declared - 128}-{declared - 1}/{declared}"
    assert tag == bytes(128)
    assert rig.encodes == encodes


@pytest.mark.usefixtures("unpaced_encoder")
async def test_a_part_past_the_real_end_of_an_mp3_finds_that_end() -> None:
    """An encode that starts further on ends on the same last frame as one from the start."""
    rig = _Rig(_tone(11.6), duration=12, output_format=MP3_44K)
    await rig.decode(finished=False)
    await rig.fetch(method="HEAD")
    await rig.buffer._set_eof()
    encoded = await _encode_mp3(rig.pcm)
    real = len(encoded)

    _, tail = await rig.fetch(f"bytes={real - 30000}-")
    assert tail == encoded[-30000:]
    with pytest.raises(web.HTTPRequestRangeNotSatisfiable) as refusal:
        await rig.fetch(f"bytes={real}-")
    assert refusal.value.headers["Content-Range"] == f"bytes */{real}"

    # a player that asked for a part the audio never reaches
    late = _Rig(rig.pcm, duration=12, output_format=MP3_44K)
    await late.decode(finished=False)
    await late.fetch(method="HEAD")
    await late.buffer._set_eof()
    _, nothing = await late.fetch(f"bytes={real + 20000}-")
    assert nothing == b""
    head, _ = await late.fetch(method="HEAD")
    assert head.content_length == real


MP3_JOIN_CASES = {
    # nothing between the track and the encoder
    "source-format": (PCM_16, MP3_44K, ()),
    # audio of more than 16 bit goes to the encoder as it is: brought down to 16 bit first
    # it would get dither, which differs from one encode to the next
    "24-bit": (
        AudioFormat(content_type=ContentType.PCM_S24LE, sample_rate=48000, bit_depth=24),
        MP3_48K,
        ("volume=-6dB",),
    ),
    # a resampler in front of the encoder: an encode starts on a frame of both sample rates
    "resampled": (
        AudioFormat(content_type=ContentType.PCM_S24LE, sample_rate=88200, bit_depth=24),
        MP3_48K,
        (),
    ),
}


@pytest.mark.usefixtures("unpaced_encoder")
@pytest.mark.parametrize("case", MP3_JOIN_CASES)
async def test_parts_of_an_mp3_join_into_one_download(case: str) -> None:
    """
    The same offset gives the same bytes in an MP3, whichever request it comes from.

    Every part comes from an encoder that started by itself, on a block of frames a second
    or more before the part, and its frames are those of the file fetched in one go.
    """
    pcm_format, output_format, filter_params = MP3_JOIN_CASES[case]
    # a length that is no whole number of seconds, as tracks have
    pcm = _tone(20.37, pcm_format)
    kept, ahead = 8 * KIB_16, 30 * KIB_16

    async def _rig() -> _Rig:
        rig = _Rig(
            pcm,
            duration=20,
            pcm_format=pcm_format,
            output_format=output_format,
            filter_params=filter_params,
        )
        await rig.decode()
        return rig

    resp, download = await (await _rig()).fetch()
    assert resp.content_length == len(download)
    assert resp.keep_alive
    assert kept < ahead < len(download)

    # a Sonos keeps the start, fetches a part for a seek ahead, and asks to go on where
    # its start ends
    sonos = await _rig()
    _, start = await sonos.fetch(wanted=kept)
    _, seek_ahead = await sonos.fetch(f"bytes={ahead}-")
    _, between = await sonos.fetch(f"bytes={kept}-", wanted=ahead - kept)
    assert sonos.encodes == 3
    assert start + between + seek_ahead == download

    # a WiiM asks for the byte it seeks to, wherever in a frame that is
    wiim = await _rig()
    for offset in (100001, 3 * len(download) // 4, len(download) - 5000):
        _, part = await wiim.fetch(f"bytes={offset}-")
        assert part == download[offset:]
