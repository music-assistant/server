"""Tests for a track of a queue with crossfade that a player fetches as a file."""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from types import MethodType, SimpleNamespace
from typing import TYPE_CHECKING, Any, NamedTuple, cast
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from music_assistant_models.enums import (
    ContentType,
    CrossfadeMode,
    MediaType,
    PlayerFeature,
    RepeatMode,
    StreamType,
)
from music_assistant_models.errors import QueueEmpty
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.controllers.streams.audio import CrossfadeHandover, StreamsAudio
from music_assistant.controllers.streams.audio_buffer import AudioBuffer
from music_assistant.controllers.streams.controller import StreamsController
from music_assistant.controllers.streams.file_stream import (
    FileKey,
    FileStream,
    FileStreams,
    mp3_frame_count,
    mp3_frame_offset,
)
from music_assistant.controllers.streams.mixed_track import SMART_FADE_STRETCH, MixedTrack
from music_assistant.controllers.streams.smart_fades.models import CrossfadeTimingInfo
from music_assistant.helpers.ffmpeg import check_ffmpeg_version
from tests.controllers.streams.test_crossfade_transition import (
    STANDARD_CROSSFADE_DURATION,
    TEST_PCM_FORMAT,
    _queue_item,
    _reported,
)
from tests.controllers.streams.test_file_stream_route import (
    DITHER,
    HEADER_SIZE,
    KIB_16,
    PLAYER_ID,
    QUEUE_ID,
    SESSION_ID,
    _Download,
    _no_audio,
    _samples,
)
from tests.controllers.streams.test_item_voice_over import _item_audio

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Iterator

RATE = 44100
PCM_F32 = AudioFormat(content_type=ContentType.PCM_F32LE, sample_rate=RATE, bit_depth=32)
PCM_16 = AudioFormat(content_type=ContentType.PCM_S16LE, sample_rate=RATE, bit_depth=16)
WAV_16 = AudioFormat(content_type=ContentType.WAV, sample_rate=RATE, bit_depth=16)
WAV_24 = AudioFormat(content_type=ContentType.WAV, sample_rate=RATE, bit_depth=24)
MP3 = AudioFormat(content_type=ContentType.MP3, sample_rate=RATE, bit_depth=16)
MP3_BYTES_PER_SECOND = 40000
# the crossfade of the queue, and what its mix cuts off the end of a track before it blends
WINDOW = 4
TRIMMED = 0.2 - 1 / RATE
TRACK_SECONDS = 12
# the tracks whose parts are joined: long enough to seek in before a mix starts
JOIN_SECONDS = 16


def _tone(seconds: float, seed: int, pcm_format: AudioFormat = PCM_F32) -> bytes:
    """
    Make audio that differs from one moment to the next, and from one track to the next.

    :param seconds: Length of the audio.
    :param seed: What makes the audio of one track differ from another.
    :param pcm_format: The format of the audio.
    """
    rate = pcm_format.sample_rate
    time = np.arange(round(seconds * rate)) / rate
    signal = sum(
        np.sin(2 * np.pi * frequency * (1 + seed / 7) * time + phase)
        * (1 + 0.5 * np.sin(2 * np.pi * beat * time))
        for frequency, phase, beat in ((110, 0.0, 0.31), (990, 1.0, 0.47), (6200, 2.0, 0.73))
    )
    signal = signal / 4.5 * 0.25 + np.random.default_rng(seed).normal(0, 0.01, len(time))
    stereo = np.stack([signal, signal[::-1]], axis=1)
    if pcm_format.content_type == ContentType.PCM_F32LE:
        return stereo.astype("<f4").tobytes()
    if pcm_format.bit_depth == 16:
        return (stereo * 32767).astype("<i2").tobytes()
    samples = (stereo * 8388607).astype("<i4")
    return samples.view(np.uint8).reshape(-1, 4)[:, :3].tobytes()


class _Rig:
    """A streams controller that serves the tracks of a queue with crossfade to one player."""

    def __init__(
        self,
        tracks: dict[str, bytes],
        *,
        output_format: AudioFormat,
        pcm_format: AudioFormat = PCM_F32,
        buffer_format: AudioFormat | None = None,
        track_filters: tuple[str, ...] = (),
        player_filters: tuple[str, ...] = (),
        crossfade_mode: CrossfadeMode = CrossfadeMode.STANDARD_CROSSFADE,
    ) -> None:
        """
        Initialize the rig.

        :param tracks: The decoded audio of every track of the queue, by queue item id.
        :param output_format: Format the player gets.
        :param pcm_format: Format the streams of the tracks are made in.
        :param buffer_format: Format of the decoded audio, when it differs from that.
        :param track_filters: Filters between the decoded audio and the stream of a track.
        :param player_filters: The filters of the player.
        :param crossfade_mode: The crossfade of the queue.
        """
        self.tracks = tracks
        self.pcm_format = pcm_format
        self.buffer_format = buffer_format or pcm_format
        self.output_format = output_format
        self.track_filters = track_filters
        self.items: dict[str, QueueItem] = {}
        # every read of the decoded audio of a track: the item and the position in it
        self.opened: list[tuple[str, int]] = []
        for item_id, pcm in tracks.items():
            duration = len(pcm) // self.buffer_format.pcm_sample_size
            item = QueueItem(
                queue_id=QUEUE_ID, queue_item_id=item_id, name=item_id, duration=duration
            )
            item.streamdetails = StreamDetails(
                provider="local--1",
                item_id=item_id,
                audio_format=AudioFormat(content_type=ContentType.FLAC),
                media_type=MediaType.TRACK,
                stream_type=StreamType.LOCAL_FILE,
                path=f"/music/{item_id}.flac",
                duration=duration,
                allow_seek=True,
            )
            item.streamdetails.buffer = AudioBuffer(self.buffer_format)
            self.items[item_id] = item
        self.queue_data = PlayerQueueData(
            queue=MagicMock(
                queue_id=QUEUE_ID,
                display_name="Queue",
                repeat_mode=RepeatMode.OFF,
                items=len(tracks),
            ),
            items=list(self.items.values()),
            session_id=SESSION_ID,
        )
        self.player = MagicMock(
            protocol_id=PLAYER_ID,
            group_members=[],
            fetches_files=True,
            supported_features={PlayerFeature.GAPLESS_PLAYBACK},
        )

        mass = self.mass = MagicMock()
        mass.player_queues.get.return_value = self.queue_data.queue
        mass.player_queues.queue_data.return_value = self.queue_data
        mass.player_queues.get_item.side_effect = lambda _queue_id, item_id: self.items.get(item_id)
        mass.player_queues.load_next_queue_item = AsyncMock(side_effect=self._load_next)
        mass.player_queues.prepare_next_audio_buffer.return_value = None
        mass.players.get_output_protocol.return_value = self.player
        mass.config.get_raw_core_config_value.return_value = WINDOW
        mass.create_task.side_effect = lambda coro, **_kwargs: coro.close()
        # no track has been analysed
        mass.streams.audio_analysis.get_audio_analysis = AsyncMock(return_value=None)

        # the real stream of a track with its mixes, and the real mixer
        audio = self.audio = StreamsAudio(cast("Any", mass))
        audio.setup()
        audio.select_pcm_format = AsyncMock(return_value=pcm_format)  # type: ignore[method-assign]
        audio.get_output_format = AsyncMock(return_value=output_format)  # type: ignore[method-assign]
        audio.get_player_output_plan = MagicMock(  # type: ignore[method-assign]
            return_value=MagicMock(filter_params=list(player_filters))
        )
        audio.get_queue_item_stream = self._item_stream  # type: ignore[method-assign,assignment]
        self.streams = MagicMock(wraps=audio.get_queue_item_stream_with_smartfade)
        audio.get_queue_item_stream_with_smartfade = self.streams  # type: ignore[method-assign]
        self.mixes = AsyncMock(wraps=audio.smart_fades_mixer.build)
        audio.smart_fades_mixer.build = self.mixes  # type: ignore[method-assign]

        ctrl = self.ctrl = MagicMock()
        ctrl.mass = mass
        ctrl.audio = audio
        ctrl._open_item_streams = {}
        ctrl._ended_item_streams = {}
        ctrl._active_output_streams = 0
        ctrl._requested_item_ids.return_value = set()
        ctrl._release_abandoned_item_sources = AsyncMock()
        ctrl._file_streams = FileStreams()
        # bind the real methods: the mocked self would silently swallow them otherwise
        for name in (
            "serves_as_file",
            "_select_item_crossfade_mode",
            "_serve_file",
            "_send_file_part",
            "_encode_file",
        ):
            setattr(ctrl, name, MethodType(getattr(StreamsController, name), ctrl))
        ctrl.get_crossfade_mode.return_value = crossfade_mode

    @property
    def passes(self) -> list[int | None]:
        """Return the position every pass over a track started at, None for its start."""
        return [call.kwargs["start_ms"] for call in self.streams.call_args_list]

    async def decode(self) -> None:
        """Put the audio of every track in its buffer."""
        second = self.buffer_format.pcm_sample_size
        for item_id, pcm in self.tracks.items():
            streamdetails = self.items[item_id].streamdetails
            assert streamdetails is not None
            for start in range(0, len(pcm), second):
                await streamdetails.buffer._put(pcm[start : start + second])
            await streamdetails.buffer._set_eof()

    def file(self, item_id: str) -> FileStream:
        """
        Return the file the player fetches a track as.

        :param item_id: The queue item id of the track.
        """
        file = self.ctrl._file_streams.get(PLAYER_ID, FileKey(SESSION_ID, QUEUE_ID, item_id, 0))
        assert isinstance(file, FileStream)
        return file

    def offset(self, seconds: float) -> int:
        """
        Return the offset in a file of a moment in its stream.

        :param seconds: The moment, in seconds from the start of the stream.
        """
        return HEADER_SIZE + round(seconds * RATE) * (self.output_format.bit_depth // 8) * 2

    def request(
        self,
        item_id: str,
        byte_range: str | None = None,
        *,
        method: str = "GET",
        wanted: int | None = None,
        stall_at: int | None = None,
    ) -> tuple[web.Request, _Download]:
        """
        Build a request of the player for a track.

        :param item_id: The queue item id of the track.
        :param byte_range: The value of its Range header, if it has one.
        :param method: The method of the request.
        :param wanted: The bytes after which the player hangs up, None for all it gets.
        :param stall_at: The bytes after which the player stops reading for a while.
        """
        download = _Download(wanted, stall_at)
        fmt = self.output_format.content_type.value
        request = make_mocked_request(
            method,
            f"/single/{SESSION_ID}/{QUEUE_ID}/{item_id}/{PLAYER_ID}.{fmt}",
            headers={"Range": byte_range} if byte_range else None,
            match_info={
                "session_id": SESSION_ID,
                "queue_id": QUEUE_ID,
                "queue_item_id": item_id,
                "player_id": PLAYER_ID,
                "fmt": fmt,
            },
            writer=download.writer,
            protocol=download.protocol,
            transport=download.transport,
        )
        return request, download

    async def fetch(
        self,
        item_id: str,
        byte_range: str | None = None,
        *,
        method: str = "GET",
        wanted: int | None = None,
    ) -> tuple[web.StreamResponse, bytes]:
        """
        Let the player fetch a track, or a part of it, and return the response and its body.

        :param item_id: The queue item id of the track.
        :param byte_range: The value of the Range header, if the request has one.
        :param method: The method of the request.
        :param wanted: The bytes after which the player hangs up, None for all it gets.
        """
        request, download = self.request(item_id, byte_range, method=method, wanted=wanted)
        resp = await StreamsController.serve_queue_item_stream(self.ctrl, request)
        return resp, bytes(download.body)

    async def _load_next(self, _queue_id: str, item_id: str) -> QueueItem:
        ids = list(self.items)
        if (index := ids.index(item_id) + 1) == len(ids):
            raise QueueEmpty("No more tracks left in the queue.")
        return self.items[ids[index]]

    def _item_stream(
        self,
        queue_item: QueueItem,
        pcm_format: AudioFormat,
        seek_position: float = 0,
        **kwargs: Any,
    ) -> AsyncGenerator[bytes]:
        connected = kwargs.get("consumer_connected")
        if connected is not None and not connected():
            # nothing is read for a player that has gone, and that is no failure
            return _no_audio()
        position_ms = kwargs.get("seek_position_ms")
        exact_seek = kwargs.get("exact_seek", False) or position_ms is not None
        if position_ms is None:
            position_ms = int(seek_position * 1000)
        self.opened.append((queue_item.queue_item_id, position_ms))
        assert queue_item.streamdetails is not None
        return cast(
            "AsyncGenerator[bytes]",
            queue_item.streamdetails.buffer.get_stream(
                output_format=pcm_format,
                seek_position_ms=position_ms,
                filter_params=list(self.track_filters) or None,
                exact_seek=exact_seek,
            ),
        )


def _pair(pcm_format: AudioFormat = PCM_F32, seconds: float = TRACK_SECONDS) -> dict[str, bytes]:
    """
    Return the audio of two tracks that are mixed into each other.

    :param pcm_format: Format of the audio.
    :param seconds: Length of each track.
    """
    return {"a": _tone(seconds, 1, pcm_format), "b": _tone(seconds, 2, pcm_format)}


@pytest.fixture
def passthrough_encoder() -> Iterator[list[list[str]]]:
    """
    Hand the audio to the player as it comes, without an encoder in between.

    Yields the pacing arguments of every encode that was started.
    """
    pacing: list[list[str]] = []

    async def _passthrough(
        audio_input: AsyncGenerator[bytes], extra_input_args: list[str], **_kwargs: Any
    ) -> AsyncGenerator[bytes]:
        pacing.append(extra_input_args)
        async with aclosing(audio_input):
            async for chunk in audio_input:
                yield chunk

    with (
        patch("music_assistant.controllers.streams.controller.get_ffmpeg_stream", _passthrough),
        patch("music_assistant.controllers.streams.file_stream.PROBE_GRACE", 0.05),
    ):
        yield pacing


@pytest.fixture
async def rig(request: pytest.FixtureRequest) -> _Rig:
    """Return a rig with two decoded tracks, whose streams reach the player as they are made."""
    request.getfixturevalue("passthrough_encoder")
    await check_ffmpeg_version()
    rig = _Rig(_pair(PCM_16), output_format=WAV_16, pcm_format=PCM_16)
    await rig.decode()
    return rig


class _JoinCase(NamedTuple):
    """Two mixed tracks, and how far a join of their parts may be off one download."""

    output_format: AudioFormat
    tolerance: int
    buffer_format: AudioFormat = PCM_F32
    track_filters: tuple[str, ...] = ()
    player_filters: tuple[str, ...] = ()
    crossfade_mode: CrossfadeMode = CrossfadeMode.STANDARD_CROSSFADE
    # the seconds of a track that are held back for its mix, and of the next one it reads
    window: int = WINDOW


JOIN_CASES = {
    # nothing between the mix and the player but the bit depth: the same bytes
    "24-bit": _JoinCase(WAV_24, 0),
    # every encode adds its own dither
    "16-bit": _JoinCase(WAV_16, DITHER),
    # a track that is resampled and limited before its mix, and equalized after it
    "processed": _JoinCase(
        WAV_16,
        DITHER,
        buffer_format=AudioFormat(
            content_type=ContentType.PCM_S24LE, sample_rate=96000, bit_depth=24
        ),
        track_filters=("volume=2.5dB", "alimiter=limit=-1dB:level=false:asc=true:latency=true"),
        player_filters=("lowshelf=f=120:g=4", "equalizer=f=1000:t=q:w=1.4:g=-5"),
    ),
    # smart fades hold back more of a track than they blend, and read on in the next track
    # past the blend: a mix with a part before its blend, and a lead-in of seconds. Without
    # analysis of the tracks the blend itself is a standard crossfade.
    "smart-fades": _JoinCase(
        WAV_24, 0, crossfade_mode=CrossfadeMode.SMART_CROSSFADE, window=JOIN_SECONDS // 2
    ),
}


@pytest.mark.parametrize("name", JOIN_CASES)
async def test_parts_of_two_mixed_tracks_join_into_one_download(name: str) -> None:
    """
    The same offset gives the same audio, also in the mixes a track starts and ends with.

    One player fetches two tracks that are mixed into each other in one go. Another fetches
    them the way a player does that is paused and seeks: the start, a part further on, the
    part in between, a part of the end of the first track, and the second track from a part
    of the lead-in it starts with.
    """
    case = JOIN_CASES[name]
    await check_ffmpeg_version()
    # a length that is no whole number of seconds, as tracks have
    length = JOIN_SECONDS + 0.37
    tracks = _pair(case.buffer_format, length)

    async def _rig() -> _Rig:
        rig = _Rig(
            tracks,
            output_format=case.output_format,
            buffer_format=case.buffer_format,
            track_filters=case.track_filters,
            player_filters=case.player_filters,
            crossfade_mode=case.crossfade_mode,
        )
        await rig.decode()
        return rig

    # the pace is not what this is about, and would make the test last as long as the tracks
    with patch(
        "music_assistant.controllers.streams.controller.output_pacing_args", return_value=[]
    ):
        whole = await _rig()
        _, download_a = await whole.fetch("a")
        _, download_b = await whole.fetch("b")
        assert whole.passes == [None, None]

        parts = await _rig()
        kept, ahead, piece = 30 * KIB_16, parts.offset(4.5), 16 * KIB_16
        _, start = await parts.fetch("a", wanted=kept)
        _, further_on = await parts.fetch("a", f"bytes={ahead}-", wanted=piece)
        _, between = await parts.fetch("a", f"bytes={kept}-", wanted=ahead - kept)
        # no request has reached the end of the track yet: the next one makes it
        mix = parts.file("a").mix
        assert mix is not None
        assert mix.size is None
        _, rest = await parts.fetch("a", f"bytes={ahead + piece}-")
        # every request so far ran a pass from its own position
        assert parts.passes[0] is None
        assert len(parts.passes) == 4
        assert all(position is not None for position in parts.passes[1:])
        in_end = parts.offset(length - case.window + 1.5)
        _, inside_end = await parts.fetch("a", f"bytes={in_end}-", wanted=2 * KIB_16)

        lead_in = parts.offset(TRIMMED / 2)
        _, from_lead_in = await parts.fetch("b", f"bytes={lead_in}-", wanted=KIB_16)
        _, start_b = await parts.fetch("b", wanted=lead_in)
        _, rest_b = await parts.fetch("b", f"bytes={lead_in + KIB_16}-")

    joined_a = start + between + further_on + rest
    joined_b = start_b + from_lead_in + rest_b
    if not case.track_filters:
        # the mix cut the end of the first track off before it blended, and read the second
        # track as far as it held the first one back
        blend = min(WINDOW, case.window - TRIMMED)
        assert len(download_a) == whole.offset(length - TRIMMED)
        assert len(download_b) == whole.offset(length - blend)
        kept_end, kept_lead_in = whole.file("a").mix, whole.file("b").mix
        assert kept_end is not None
        assert kept_lead_in is not None
        frame_size = PCM_F32.pcm_sample_size // RATE
        assert len(kept_end._end) == round((case.window - TRIMMED) * RATE) * frame_size
        assert len(kept_lead_in._lead_in) == round((case.window - blend) * RATE) * frame_size
    for joined, download in (
        (joined_a[HEADER_SIZE:], download_a[HEADER_SIZE:]),
        (joined_b[HEADER_SIZE:], download_b[HEADER_SIZE:]),
        (inside_end, download_a[in_end : in_end + 2 * KIB_16]),
    ):
        assert len(joined) == len(download)
        if case.tolerance == 0:
            assert joined == download
        else:
            assert np.abs(_samples(joined) - _samples(download)).max() <= case.tolerance
    assert joined_a[:HEADER_SIZE] == download_a[:HEADER_SIZE]
    assert joined_b[:HEADER_SIZE] == download_b[:HEADER_SIZE]


def _mp3_size(seconds: float) -> int:
    """
    Return the size of an MP3 file.

    :param seconds: Length of the audio of the file.
    """
    return mp3_frame_offset(mp3_frame_count(round(seconds * RATE)), RATE)


async def test_parts_of_two_mixed_mp3_tracks_join_into_one_download() -> None:
    """
    Two tracks that are mixed into each other are MP3 files as well.

    A mix is made once and kept as it goes to the encoder, so every part of a file is
    encoded from the same audio, and its frames are those of the file fetched in one go.
    """
    await check_ffmpeg_version()
    length = JOIN_SECONDS + 0.37
    tracks = _pair(seconds=length)

    async def _rig() -> _Rig:
        rig = _Rig(tracks, output_format=MP3)
        await rig.decode()
        return rig

    # the pace is not what this is about, and would make the test last as long as the tracks
    with patch(
        "music_assistant.controllers.streams.controller.output_pacing_args", return_value=[]
    ):
        whole = await _rig()
        resp_a, download_a = await whole.fetch("a")
        _, download_b = await whole.fetch("b")
        assert whole.passes == [None, None]
        # the file declared the whole track, and ends with the last frame of its mix
        assert resp_a.content_length == _mp3_size(length)
        assert len(download_a) == _mp3_size(length - TRIMMED)
        assert len(download_b) == _mp3_size(length - WINDOW + TRIMMED)
        with pytest.raises(web.HTTPRequestRangeNotSatisfiable) as refusal:
            await whole.fetch("a", f"bytes={len(download_a)}-")
        assert refusal.value.headers["Content-Range"] == f"bytes */{len(download_a)}"

        parts = await _rig()
        kept, ahead, piece = 6 * KIB_16, 12 * KIB_16, 3 * KIB_16
        _, start = await parts.fetch("a", wanted=kept)
        _, further_on = await parts.fetch("a", f"bytes={ahead}-", wanted=piece)
        _, between = await parts.fetch("a", f"bytes={kept}-", wanted=ahead - kept)
        _, rest = await parts.fetch("a", f"bytes={ahead + piece}-")
        # every request so far ran a pass from its own position
        assert parts.passes[0] is None
        assert len(parts.passes) == 4
        in_end = round((length - WINDOW + 1.5) * MP3_BYTES_PER_SECOND)
        _, inside_end = await parts.fetch("a", f"bytes={in_end}-", wanted=2 * KIB_16)

        lead_in = round(TRIMMED / 2 * MP3_BYTES_PER_SECOND)
        _, from_lead_in = await parts.fetch("b", f"bytes={lead_in}-", wanted=KIB_16)
        _, start_b = await parts.fetch("b", wanted=lead_in)
        _, rest_b = await parts.fetch("b", f"bytes={lead_in + KIB_16}-")

    assert start + between + further_on + rest == download_a
    assert inside_end == download_a[in_end : in_end + 2 * KIB_16]
    assert start_b + from_lead_in + rest_b == download_b


@pytest.mark.parametrize(
    ("crossfade_mode", "stretch"),
    [(CrossfadeMode.STANDARD_CROSSFADE, 0), (CrossfadeMode.SMART_CROSSFADE, SMART_FADE_STRETCH)],
)
async def test_a_mixed_mp3_file_declares_the_longest_its_stream_can_be(
    crossfade_mode: CrossfadeMode, stretch: int
) -> None:
    """The seconds a smart fade can add to a track are frames of the MP3 it is served as."""
    rig = _Rig(_pair(PCM_16), output_format=MP3, pcm_format=PCM_16, crossfade_mode=crossfade_mode)
    await rig.decode()

    resp, _ = await rig.fetch("a", method="HEAD")

    assert resp.content_length == _mp3_size(TRACK_SECONDS + stretch)
    assert rig.passes == []


async def test_a_file_is_the_stream_of_a_plain_playthrough(rig: _Rig) -> None:
    """A track that is fetched in one go as a file is the stream a player gets without files."""
    stream_rig = _Rig(_pair(PCM_16), output_format=WAV_16, pcm_format=PCM_16)
    await stream_rig.decode()
    stream_rig.player.fetches_files = False
    stream_rig.player.requires_content_length = False

    for item_id in ("a", "b"):
        _, stream = await stream_rig.fetch(item_id)
        _, file = await rig.fetch(item_id)
        assert file[HEADER_SIZE:] == stream

    # one pass per track made the stream, and one mix between them
    assert rig.passes == [None, None]
    assert rig.mixes.await_count == stream_rig.mixes.await_count == 1


async def test_the_mix_of_a_plain_playthrough_starts_a_crossfade_before_the_end(rig: _Rig) -> None:
    """The mix takes the last seconds of a track, and waits for the player to get there."""
    second = PCM_16.pcm_sample_size
    request, download = rig.request("a", stall_at=3 * second)
    task = asyncio.create_task(StreamsController.serve_queue_item_stream(rig.ctrl, request))
    await download.stalled.wait()
    await asyncio.sleep(0.2)

    # the track is only read as far as the player takes it
    rig.mass.player_queues.load_next_queue_item.assert_not_awaited()
    rig.mixes.assert_not_awaited()

    download.resume.set()
    await task

    rig.mixes.assert_awaited_once()
    assert rig.mixes.await_args is not None
    assert len(rig.mixes.await_args.kwargs["fade_out_data"]) == WINDOW * second
    body, track = bytes(download.body[HEADER_SIZE:]), rig.tracks["a"]
    mix_start, in_mix = (TRACK_SECONDS - WINDOW) * second, (TRACK_SECONDS - WINDOW + 2) * second
    assert body[:mix_start] == track[:mix_start]
    assert body[in_mix : in_mix + second] != track[in_mix : in_mix + second]


async def test_a_track_ends_where_its_mix_ends(rig: _Rig) -> None:
    """The file declares the whole track; its body ends with the mix, and then it has that size."""
    declared = rig.offset(TRACK_SECONDS)
    real = rig.offset(TRACK_SECONDS - TRIMMED)

    head, _ = await rig.fetch("a", method="HEAD")
    resp, body = await rig.fetch("a")

    assert head.content_length == resp.content_length == declared
    assert len(body) == real
    # no silence was added, so the connection is closed on the missing rest
    assert not resp.keep_alive
    reads = len(rig.opened)
    for start in (real, declared - 1):
        with pytest.raises(web.HTTPRequestRangeNotSatisfiable) as refusal:
            await rig.fetch("a", f"bytes={start}-")
        assert refusal.value.headers["Content-Range"] == f"bytes */{real}"
    head, _ = await rig.fetch("a", method="HEAD")
    assert head.content_length == real
    resp, tail = await rig.fetch("a", f"bytes={real - 5000}-")
    assert resp.headers["Content-Range"] == f"bytes {real - 5000}-{real - 1}/{real}"
    assert tail == body[-5000:]
    # the end was kept: none of this read the track, or ran a pass
    assert len(rig.opened) == reads
    assert rig.passes == [None]


async def test_every_part_of_a_mixed_track_is_paced(
    rig: _Rig, passthrough_encoder: list[list[str]]
) -> None:
    """What a pass made ahead only fills memory: every part is sent at playback pace."""
    await rig.fetch("a")
    # a part of the end that is kept, a second and a half past the whole second it is read from
    await rig.fetch("a", f"bytes={rig.offset(TRACK_SECONDS - WINDOW + 2.5)}-")
    await rig.fetch("b")

    assert passthrough_encoder == [
        ["-readrate", "1.0", "-readrate_initial_burst", burst, "-readrate_catchup", "2"]
        for burst in ("6", "7.5", "6")
    ]


async def test_a_part_past_what_is_left_of_a_track_is_refused_at_once(rig: _Rig) -> None:
    """Once the fade into a track is known, a part past the end of the track opens nothing."""
    await rig.fetch("a")
    left = rig.offset(TRACK_SECONDS - WINDOW + TRIMMED)

    # the player has read past the fade into the track
    await rig.fetch("b", wanted=rig.offset(1))
    reads = len(rig.opened)
    with pytest.raises(web.HTTPRequestRangeNotSatisfiable) as refusal:
        await rig.fetch("b", f"bytes={left + KIB_16}-")

    assert refusal.value.headers["Content-Range"] == f"bytes */{left}"
    assert len(rig.opened) == reads
    assert rig.passes == [None, None]


async def test_a_fade_into_a_track_is_kept_for_the_requests_after_the_first(rig: _Rig) -> None:
    """The fade into a track passes once, and is there for every request that asks for it."""
    _, whole_a = await rig.fetch("a")
    assert rig.audio.fade_into_pending(QUEUE_ID, "b")

    # a player asks for the start of the next track and drops that request
    _, probe = await rig.fetch("b", wanted=KIB_16)
    assert not rig.audio.fade_into_pending(QUEUE_ID, "b")
    _, whole_b = await rig.fetch("b", "bytes=0-")
    _, part = await rig.fetch("b", f"bytes={3 * KIB_16}-")

    second = PCM_16.pcm_sample_size
    lead_in = round(TRIMMED * second)
    assert probe == whole_b[:KIB_16]
    assert part == whole_b[3 * KIB_16 :]
    # the mix ended in the first track's file, on what it cut off the end of that track
    assert len(whole_a) == rig.offset(TRACK_SECONDS - TRIMMED)
    # the lead-in is what the mix read of the track past its blend, and the track goes on
    # where the mix stopped reading
    assert whole_b[HEADER_SIZE:] == rig.tracks["b"][WINDOW * second - lead_in :]
    # the file is found again after the fade moved the position its track starts at
    streamdetails = rig.items["b"].streamdetails
    assert streamdetails is not None
    assert streamdetails.seek_position == pytest.approx(WINDOW - TRIMMED)
    assert rig.file("b").mix is not None


async def test_a_request_before_the_end_is_made_runs_a_pass_of_its_own(rig: _Rig) -> None:
    """A player that seeks gets a pass from its position, which makes the end of the track."""
    second = PCM_16.pcm_sample_size
    seek = rig.offset(5.5)

    await rig.fetch("a", wanted=KIB_16)
    _, body = await rig.fetch("a", f"bytes={seek}-")

    # the encode starts a second before the part, and the track is read a second before
    # that: the filters of the player and those of the track both settle
    assert rig.passes == [None, 3000]
    assert rig.opened[:2] == [("a", 0), ("a", 3000)]
    assert (
        body[: (TRACK_SECONDS - WINDOW) * second - (seek - HEADER_SIZE)]
        == (rig.tracks["a"][seek - HEADER_SIZE : (TRACK_SECONDS - WINDOW) * second])
    )
    assert len(body) == rig.offset(TRACK_SECONDS - TRIMMED) - seek
    # it made the mix into the next track, which takes it as its lead-in
    rig.mixes.assert_awaited_once()
    assert rig.audio.fade_into_pending(QUEUE_ID, "b")


async def test_a_request_into_the_end_of_a_track_makes_the_whole_mix(rig: _Rig) -> None:
    """A player that seeks into the last seconds of a track gets the mix a playthrough makes."""
    playthrough = _Rig(_pair(PCM_16), output_format=WAV_16, pcm_format=PCM_16)
    await playthrough.decode()
    _, whole = await playthrough.fetch("a")
    seek = rig.offset(TRACK_SECONDS - 0.5)

    await rig.fetch("a", wanted=KIB_16)
    _, end = await rig.fetch("a", f"bytes={seek}-")
    _, again = await rig.fetch("a")

    assert end == whole[seek:]
    assert again == whole
    # the track was read from before the audio its mix is made from
    assert rig.passes == [None, (TRACK_SECONDS - WINDOW - 2) * 1000]


async def test_a_pass_ends_with_the_request_that_drives_it(rig: _Rig) -> None:
    """A player that hangs up in the middle of a track leaves nothing running."""
    request, download = rig.request("a", stall_at=3 * PCM_16.pcm_sample_size)
    task = asyncio.create_task(StreamsController.serve_queue_item_stream(rig.ctrl, request))
    await download.stalled.wait()
    mix = rig.file("a").mix
    assert mix is not None
    running = mix._pass
    assert running is not None
    assert running.task is not None

    download.hang_up()
    download.resume.set()
    await task
    await asyncio.wait({running.task})

    assert running.task.cancelled()
    assert mix.size is None
    rig.mixes.assert_not_awaited()
    streamdetails = rig.items["a"].streamdetails
    assert streamdetails is not None
    assert streamdetails.buffer.users == ()


async def test_a_pass_that_reached_the_end_of_its_track_runs_on_by_itself(rig: _Rig) -> None:
    """From the end of the track on, the mix is made whether its player reads on or not."""
    second = PCM_16.pcm_sample_size
    request, download = rig.request("a", stall_at=(TRACK_SECONDS - WINDOW + 1) * second)
    task = asyncio.create_task(StreamsController.serve_queue_item_stream(rig.ctrl, request))
    await download.stalled.wait()
    mix = rig.file("a").mix
    assert mix is not None
    running = mix._pass
    assert running is not None
    assert running.task is not None

    download.hang_up()
    download.resume.set()
    await task
    await running.task

    assert mix.size == round((TRACK_SECONDS - TRIMMED) * second)
    assert rig.audio.fade_into_pending(QUEUE_ID, "b")
    # the player comes back for the rest, which is there
    start = rig.offset(TRACK_SECONDS - WINDOW + 2)
    _, rest = await rig.fetch("a", f"bytes={start}-")
    assert len(rest) == rig.offset(TRACK_SECONDS - TRIMMED) - start
    assert rig.passes == [None]


@pytest.mark.usefixtures("passthrough_encoder")
async def test_what_is_kept_goes_when_a_track_is_no_longer_playing_or_next() -> None:
    """The audio of a mix is kept for the track that plays and the one after it."""
    await check_ffmpeg_version()
    second = PCM_16.pcm_sample_size
    tracks = {**_pair(PCM_16), "c": _tone(TRACK_SECONDS, 3, PCM_16)}
    rig = _Rig(tracks, output_format=WAV_16, pcm_format=PCM_16)
    await rig.decode()

    await rig.fetch("a")
    await rig.fetch("b")
    file_a, file_b = rig.file("a"), rig.file("b")
    assert file_a.mix is not None
    assert file_b.mix is not None
    assert len(file_a.mix._end) == round((WINDOW - TRIMMED) * second)
    assert len(file_b.mix._lead_in) == round(TRIMMED * second)
    assert len(file_b.mix._end) == round((WINDOW - TRIMMED) * second)

    # the player asks for the third track while the second plays
    await rig.fetch("c", method="HEAD")

    assert rig.ctrl._file_streams.get(PLAYER_ID, FileKey(SESSION_ID, QUEUE_ID, "a", 0)) is None
    assert not file_a.mix._end
    assert rig.file("b") is file_b
    assert file_b.mix._lead_in
    assert file_b.mix._end

    # and the rest goes with the session
    rig.ctrl._file_streams.release(QUEUE_ID, None)
    assert not file_b.mix._lead_in
    assert not file_b.mix._end


async def test_the_files_of_a_session_end_the_pass_that_runs(rig: _Rig) -> None:
    """A queue that stops ends the pass of a track a player was reading."""
    request, download = rig.request("a", stall_at=3 * PCM_16.pcm_sample_size)
    task = asyncio.create_task(StreamsController.serve_queue_item_stream(rig.ctrl, request))
    await download.stalled.wait()
    mix = rig.file("a").mix
    assert mix is not None
    running = mix._pass
    assert running is not None
    assert running.task is not None

    rig.queue_data.session_id = None
    rig.ctrl._file_streams.release(QUEUE_ID, None)
    download.resume.set()
    resp = await task
    await asyncio.wait({running.task})

    assert running.task.cancelled()
    assert not resp.keep_alive
    rig.ctrl.logger.error.assert_not_called()


async def test_a_fade_into_a_track_of_a_queue_without_crossfade(rig: _Rig) -> None:
    """A fade that waits for a track is its lead-in, whatever the queue does at its end."""
    rig.ctrl.get_crossfade_mode.return_value = CrossfadeMode.DISABLED
    second = PCM_16.pcm_sample_size
    fade = _tone(1.5, 9, PCM_16)

    async def _fade() -> AsyncGenerator[bytes]:
        yield fade

    rig.audio._crossfade_handover[QUEUE_ID] = CrossfadeHandover(
        stream=_fade(),
        fade_in_media_duration=3.5,
        pcm_format=PCM_16,
        queue_item_id="b",
        crossfade_mode=CrossfadeMode.VOICE_OVER,
        elapsed_time_offset=2.0,
        session_id=SESSION_ID,
    )

    _, body = await rig.fetch("b")
    _, part = await rig.fetch("b", f"bytes={rig.offset(1.0)}-")

    # the fade, and the track from where the fade left it to its end
    assert body[HEADER_SIZE:] == fade + rig.tracks["b"][round(3.5 * second) :]
    assert part == body[rig.offset(1.0) :]
    rig.mixes.assert_not_awaited()
    assert rig.passes == [None]


@pytest.mark.parametrize(
    ("crossfade_mode", "stretch"),
    [(CrossfadeMode.STANDARD_CROSSFADE, 0), (CrossfadeMode.SMART_CROSSFADE, SMART_FADE_STRETCH)],
)
async def test_a_mixed_file_declares_the_longest_its_stream_can_be(
    rig: _Rig, crossfade_mode: CrossfadeMode, stretch: int
) -> None:
    """A smart fade can slow the end of a track down, which makes its stream longer."""
    rig.ctrl.get_crossfade_mode.return_value = crossfade_mode

    resp, _ = await rig.fetch("a", method="HEAD")

    assert resp.content_length == rig.offset(TRACK_SECONDS + stretch)
    assert rig.passes == []


async def test_a_pass_from_a_position_takes_no_fade_and_stores_no_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stream that goes on mid-item leaves the fade into the item, and its length, alone."""
    track = _queue_item("item-1", "Track", duration=40)
    audio, mass = _item_audio(monkeypatch, next_item=track, load_next=[QueueEmpty])
    opened: list[dict[str, Any]] = []

    async def _item_stream(*_args: object, **kwargs: Any) -> AsyncGenerator[bytes]:
        opened.append(kwargs)
        for _ in range(39):
            yield bytes(TEST_PCM_FORMAT.pcm_sample_size)

    monkeypatch.setattr(audio, "get_queue_item_stream", _item_stream)
    handover = CrossfadeHandover(
        stream=None,
        fade_in_media_duration=8.0,
        pcm_format=TEST_PCM_FORMAT,
        queue_item_id="item-1",
        session_id="session-1",
    )
    audio._crossfade_handover["queue-1"] = handover
    marks = MagicMock()

    output = bytearray()
    async for chunk in audio.get_queue_item_stream_with_smartfade(
        cast("Any", SimpleNamespace(protocol_id="player-1", name="Player")),
        cast("Any", track),
        TEST_PCM_FORMAT,
        crossfade_mode=CrossfadeMode.STANDARD_CROSSFADE,
        standard_crossfade_duration=STANDARD_CROSSFADE_DURATION,
        session_id="session-1",
        start_ms=1001,
        marks=marks,
    ):
        output += chunk

    assert len(output) == 39 * TEST_PCM_FORMAT.pcm_sample_size
    # the position goes in as it is: as float seconds it would lose a millisecond
    assert opened[0]["seek_position_ms"] == 1001
    assert opened[0]["exact_seek"] is True
    # the fade into the item still waits for the stream of its start
    assert audio._crossfade_handover["queue-1"] is handover
    assert track.streamdetails.seek_position == 0
    # all but a second of the item was streamed, which a stream from the start would
    # take for its length
    assert track.streamdetails.duration == track.duration == 40
    assert _reported(mass) == []
    marks.lead_in_ended.assert_not_called()
    # all but the held tail came before the end of the item
    marks.source_read_out.assert_called_once_with(
        (39 - STANDARD_CROSSFADE_DURATION) * TEST_PCM_FORMAT.pcm_sample_size
    )


@pytest.mark.parametrize("chunk_size", [TEST_PCM_FORMAT.pcm_sample_size, 2731, 40001])
async def test_the_held_tail_is_cut_at_exactly_the_fade_window(
    monkeypatch: pytest.MonkeyPatch, chunk_size: int
) -> None:
    """Where a fade starts does not depend on the pieces the audio of a track comes in."""
    second = TEST_PCM_FORMAT.pcm_sample_size
    track = _queue_item("item-1", "Track", duration=20)
    following = _queue_item("item-2", "Following")
    audio, _mass = _item_audio(monkeypatch, next_item=following, load_next=[following])
    # a track of 20.4 seconds
    audio_bytes = bytes(range(256)) * (round(20.4 * second) // 256)

    async def _item_stream(
        queue_item: SimpleNamespace, *_args: object, **_kwargs: Any
    ) -> AsyncGenerator[bytes]:
        data = audio_bytes if queue_item is track else bytes(40 * second)
        for start in range(0, len(data), chunk_size):
            yield data[start : start + chunk_size]

    monkeypatch.setattr(audio, "get_queue_item_stream", _item_stream)
    build = cast("Any", audio.smart_fades_mixer.build)
    build.side_effect = None
    build.return_value = SimpleNamespace(
        timing_info=CrossfadeTimingInfo(crossfade_duration=float(STANDARD_CROSSFADE_DURATION))
    )
    marks = MagicMock()

    output = bytearray()
    async for chunk in audio.get_queue_item_stream_with_smartfade(
        cast("Any", SimpleNamespace(protocol_id="player-1", name="Player")),
        cast("Any", track),
        TEST_PCM_FORMAT,
        crossfade_mode=CrossfadeMode.STANDARD_CROSSFADE,
        standard_crossfade_duration=STANDARD_CROSSFADE_DURATION,
        session_id="session-1",
        marks=marks,
    ):
        output += chunk

    tail_start = len(audio_bytes) - STANDARD_CROSSFADE_DURATION * second
    assert build.await_args.kwargs["fade_out_data"] == audio_bytes[tail_start:]
    assert bytes(output[:tail_start]) == audio_bytes[:tail_start]
    marks.lead_in_ended.assert_called_once_with(0, 0)
    marks.source_read_out.assert_called_once_with(tail_start)
    await audio._crossfade_handover.pop("queue-1").close()


def test_a_file_whose_track_start_moved_is_found_by_its_track() -> None:
    """A fade into a track moves the position it starts at, which a plain file is made of."""
    files = FileStreams()
    mixed, plain = FileStream(PCM_16, WAV_16, RATE), FileStream(PCM_16, WAV_16, RATE)
    mixed.mix = MixedTrack(PCM_16, 1000, 0, MagicMock(), MagicMock())
    files.add(PLAYER_ID, FileKey(SESSION_ID, QUEUE_ID, "a", 0), mixed)
    files.add(PLAYER_ID, FileKey(SESSION_ID, QUEUE_ID, "b", 0), plain)

    assert files.get(PLAYER_ID, FileKey(SESSION_ID, QUEUE_ID, "a", 3)) is mixed
    assert files.get(PLAYER_ID, FileKey(SESSION_ID, QUEUE_ID, "b", 3)) is None
    assert files.get(PLAYER_ID, FileKey("sess2", QUEUE_ID, "a", 3)) is None
