"""
A track served to a player as a file.

Some players are built around files: they want the length of a track up front, and they
pause, resume and seek by asking for a part of the file. For such a player a track becomes
a WAV or an MP3 file with a declared length. The file is virtual: every part is encoded
while it is sent, and only the start of the file is kept in memory. A track that is mixed
with its neighbours also keeps the audio of those mixes, see `MixedTrack`.
"""

from __future__ import annotations

import asyncio
import re
import time
from copy import copy
from typing import TYPE_CHECKING, Final, NamedTuple

from music_assistant_models.enums import ContentType

from music_assistant.helpers.audio import create_streaming_wave_header
from music_assistant.helpers.ffmpeg import DEFAULT_MP3_BIT_RATE

if TYPE_CHECKING:
    from aiohttp import web
    from music_assistant_models.media_items import AudioFormat
    from music_assistant_models.queue_item import QueueItem

    from music_assistant.controllers.streams.mixed_track import MixedTrack

# Bytes of the start of a file that are kept in memory. Players ask for the start again
# before they resume or seek, and mostly drop that request.
HEAD_SIZE: Final[int] = 1024 * 1024

# How fast the allowance of a file grows, in seconds of audio per second since its first
# request. A file sends audio it never sent before no further ahead than its allowance, its
# head start plus this rate times those seconds, and audio it sent before as fast as the
# player takes it. An encode goes out at playback speed after its burst, so a tenth of a
# second of credit builds per second of listening, for the next request that needs audio
# the file never sent: a seek forward after a minute finds six seconds. However a player
# asks for its parts, a file can never be pulled faster than (its length - its head start)
# / 1.1. See the usage policy.
ALLOWANCE_RATE: Final[float] = 1.1

# Seconds an encode starts before the part it is asked for. Filters in the chain (DSP, a
# resampler) need that audio to settle, so the part comes out as it would in one pass. An
# MP3 encoder needs its first three frames for the same.
RUN_IN_SECONDS: Final[int] = 1

# The codecs a file can have: their size is known before the audio is encoded.
FILE_CODECS: Final = (ContentType.WAV, ContentType.MP3)

# An MP3 file is a row of frames at a constant bit rate, without a tag or a Xing header in
# front of them. Without the bit reservoir no frame holds audio of another one, so a part
# that is encoded by itself is made of the same bytes as the whole file.
MP3_ENCODER_ARGS: Final = ("-write_xing", "0", "-id3v2_version", "0", "-reservoir", "0")
MP3_FRAME_SAMPLES: Final[int] = 1152
# the size of a frame in bytes, times the sample rate
_MP3_FRAME_SLOTS: Final[int] = 144 * DEFAULT_MP3_BIT_RATE * 1000

# How many frames apart the frames of an MP3 are that an encode can start on, by sample
# rate. An MP3 is 44.1 or 48 kHz: no player takes a lower rate, and a lossy codec gets no
# higher one. Such a frame starts on a whole frame of every sample rate the audio passes
# through, and on a whole number of bytes at the bit rate of the file, which is where the
# encoder starts over with how it pads its frames: every 1.28 seconds (51200 bytes) at
# 44.1 kHz and every 0.12 seconds (4800 bytes) at 48 kHz.
MP3_BLOCK_FRAMES: Final[dict[int, int]] = {44100: 49, 48000: 5}

# The last bytes of an MP3 file, where a renderer looks for an ID3v1 tag.
ID3V1_TAG_SIZE: Final[int] = 128

# The track that plays and its neighbours.
MAX_FILES_PER_PLAYER: Final[int] = 3

# The track that plays and the one after it: the files that keep the audio of their mixes.
MAX_MIXED_FILES_PER_PLAYER: Final[int] = 2

# Seconds a player gets to hang up, or to ask for another part, after the start of a file
# was sent to it from memory.
PROBE_GRACE: Final[float] = 0.3

_RANGE_HEADER: Final = re.compile(r"bytes=(\d*)-(\d*)")


def parse_range(header: str | None, size: int) -> range | None:
    """
    Return the bytes of a file that a Range header asks for.

    :param header: Value of the Range header, if the request has one.
    :param size: Size of the file in bytes.
    :return: The bytes asked for, empty when the part starts at or past the end of the
        file, or None when the header names no part.
    """
    match = _RANGE_HEADER.fullmatch(header.strip()) if header else None
    if match is None or not (match[1] or match[2]):
        return None
    if not match[1]:
        # the last bytes of the file
        return range(max(size - int(match[2]), 0), size)
    stop = min(int(match[2]) + 1, size) if match[2] else size
    return range(int(match[1]), stop)


def declared_frames(queue_item: QueueItem, sample_rate: int) -> int:
    """
    Return the length a file declares for a queue item, in frames.

    Exact once the whole item has been decoded. Else the longest of the item's duration,
    the duration in its stream details and the length its source reports, plus one second:
    all are whole seconds, and a file that is declared too long only ends early while one
    that is declared too short cuts music.

    :param queue_item: The queue item, with its stream details resolved.
    :param sample_rate: Sample rate of the file.
    """
    streamdetails = queue_item.streamdetails
    assert streamdetails is not None
    assert queue_item.duration is not None
    buffer = streamdetails.buffer
    seconds: float
    if buffer is not None and buffer.eof and not buffer.has_error:
        seconds = buffer.first_buffered_chunk + buffer.duration_available
    else:
        # the item can list a shorter duration than the audio that plays has
        source_duration = buffer.source_duration if buffer is not None else None
        seconds = max(queue_item.duration, streamdetails.duration or 0, source_duration or 0) + 1
    return max(round((seconds - int(streamdetails.seek_position)) * sample_rate), 0)


def mp3_frame_count(samples: int) -> int:
    """
    Return the number of frames an MP3 file has for an amount of audio.

    :param samples: Length of the audio in samples.
    """
    # the last frame of the audio is filled up, and the encoder adds one for its delay
    return -(-samples // MP3_FRAME_SAMPLES) + 1


def mp3_frame_offset(frame: int, sample_rate: int) -> int:
    """
    Return the offset in an MP3 file of one of its frames.

    :param frame: Number of the frame, 0 for the first.
    :param sample_rate: Sample rate of the file.
    """
    # where the size of a frame is no whole number of bytes, the encoder cuts the first
    # frame down to one and from there adds a byte to every frame that would leave the
    # file behind its bit rate without it
    fraction = _MP3_FRAME_SLOTS % sample_rate
    return -((fraction - frame * _MP3_FRAME_SLOTS) // sample_rate)


def mp3_frame_at(offset: int, sample_rate: int) -> int:
    """
    Return the number of the frame of an MP3 file that holds a byte.

    :param offset: Offset of the byte in the file.
    :param sample_rate: Sample rate of the file.
    """
    fraction = _MP3_FRAME_SLOTS % sample_rate
    return (offset * sample_rate + fraction) // _MP3_FRAME_SLOTS


def is_connected(request: web.Request) -> bool:
    """
    Return whether a player still holds the connection of its request.

    :param request: The request of the player.
    """
    return (transport := request.transport) is not None and not transport.is_closing()


async def player_hangs_up(request: web.Request) -> bool:
    """
    Return whether a player closes its connection in the moment it gets for that.

    :param request: The request of the player.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + PROBE_GRACE
    while is_connected(request):
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(0.02)
    return True


class FileStream:
    """
    One track as the WAV or MP3 file a player fetches.

    Its formats and its declared size are fixed when it is made, so every part a player
    asks for is cut from the same file.
    """

    def __init__(
        self, pcm_format: AudioFormat, output_format: AudioFormat, frames: int, head_start: int
    ) -> None:
        """
        Initialize the file, on the first request for it.

        :param pcm_format: PCM format the audio is read in.
        :param output_format: Format of the file.
        :param frames: Length of the audio the file declares, in frames.
        :param head_start: Seconds of audio the file may send ahead of playback.
        """
        self.pcm_format = pcm_format
        self.output_format = output_format
        self._is_mp3 = output_format.content_type == ContentType.MP3
        # the format an encode of the file is asked for, and the arguments of its encoder
        self.encode_format = copy(output_format)
        self.encoder_args: list[str] = []
        header = b""
        if self._is_mp3:
            self.byte_rate = DEFAULT_MP3_BIT_RATE * 1000 // 8
            self.encoder_args = [*MP3_ENCODER_ARGS]
            # the encoder gets the audio in the depth it is read in: brought down to 16 bit
            # first it would get dither, which differs from one encode to the next
            self.encode_format.bit_depth = pcm_format.bit_depth
        else:
            self.byte_rate = output_format.pcm_sample_size
            data_size = frames * output_format.channels * (output_format.bit_depth // 8)
            header = create_streaming_wave_header(output_format, data_size)
            # ffmpeg only delivers the samples: the header is the file's own
            self.encode_format.content_type = ContentType.from_bit_depth(output_format.bit_depth)
        self._head = bytearray(header)
        self.header_size = len(header)
        self.size = self._size_of(frames)
        # the sizes the file had before its audio ended short of them
        self._earlier_sizes: list[int] = []
        self._head_start = head_start
        self._first_request = time.monotonic()
        # the request an encode of this file runs for, by the address it comes from
        self.readers: dict[str | None, web.Request] = {}
        # the offsets the file has handed to players, in order and apart from each other
        self._sent: list[range] = []
        # where the request that last took the file over starts: the encodes that run read
        # in what was sent there
        self._front: int | None = None
        # the stream the file is cut from, for a track that is mixed with its neighbours
        self.mix: MixedTrack | None = None

    @property
    def has_audio_in_memory(self) -> bool:
        """Return whether the start that is kept in memory holds more than the header."""
        return len(self._head) > self.header_size

    def head(self, part: range) -> bytes:
        """
        Return the start of a part, as far as it is known without an encode.

        :param part: The bytes of the file a player asks for.
        """
        if self._is_mp3 and part.start >= self.size - ID3V1_TAG_SIZE:
            # a renderer looks here for a tag before it plays or seeks: the file has none
            return bytes(len(part))
        return bytes(self._head[part.start : part.stop])

    def keep(self, offset: int, data: bytes) -> None:
        """
        Keep bytes that were sent when they continue the start that is held in memory.

        :param offset: Offset in the file of the first of these bytes.
        :param data: The bytes.
        """
        if offset == len(self._head) < HEAD_SIZE:
            self._head += data[: HEAD_SIZE - offset]

    def add_reader(self, request: web.Request, offset: int) -> None:
        """
        Let an encode of the file run for a request, which ends the one of its address.

        A request from another address is another player. It reads beside the encodes that
        run when its part starts in the stretch of sent audio they read in: it gets audio
        that left before, and then runs along. A request for any other part takes the file
        over, which ends every encode that runs.

        :param request: The request the encode runs for.
        :param offset: Offset in the file of the first byte the encode delivers.
        """
        # audio that was not sent yet only leaves at the end of one stretch, through
        # encodes that each keep to the allowance of the file: no two addresses read
        # different parts of a track at once. See the usage policy
        front = self._stretch_at(self._front) if self._front is not None else None
        takes_over = front is None or not front.start <= offset <= front.stop
        if takes_over:
            self._front = offset
        # a player that asks for another part has left the one it was reading. Aborted
        # rather than closed, as a graceful close waits for the very send buffer the player
        # stopped reading
        for address, running in self.readers.items():
            if (takes_over or address == request.remote) and running.transport is not None:
                running.transport.abort()
        self.readers[request.remote] = request

    def mark_sent(self, part: range) -> None:
        """
        Take note of bytes an encode handed to its player.

        :param part: The offsets of those bytes in the file.
        """
        if not part:
            return
        start, stop = part.start, part.stop
        apart: list[range] = []
        for sent in self._sent:
            if sent.stop < start or sent.start > stop:
                apart.append(sent)
            else:
                start, stop = min(start, sent.start), max(stop, sent.stop)
        self._sent = sorted([*apart, range(start, stop)], key=lambda sent: sent.start)

    def burst(self, offset: int) -> float:
        """
        Return the seconds of audio an encode from an offset hands to its player at once.

        That is the audio up to where the stretch the file sent there ends, which left
        before, and the credit of the file for audio it never sent: what is left of its
        allowance, at most its head start. After that the encode runs at playback speed.

        :param offset: Offset in the file of the first byte the encode delivers.
        """
        new_audio = sum(len(sent) for sent in self._sent) / self.byte_rate
        allowance = self._head_start + ALLOWANCE_RATE * (time.monotonic() - self._first_request)
        credit = min(max(allowance - new_audio, 0), self._head_start)
        return (self._stretch_at(offset).stop - offset) / self.byte_rate + credit

    def remove_reader(self, request: web.Request) -> None:
        """
        Let go of a request whose encode ended.

        :param request: The request the encode ran for.
        """
        if self.readers.get(request.remote) is request:
            del self.readers[request.remote]

    def encode_start(self, offset: int) -> tuple[int, int]:
        """
        Return where an encode starts that delivers the file from an offset.

        An encode starts on a whole frame in every format the audio passes through, so a
        part is cut out of the same samples as one pass over the file: on a whole second,
        and for an MP3 on a frame from where the encoder makes the same bytes as well.

        :param offset: Offset in the file of the first byte to deliver, past the header.
        :return: The millisecond of the file the encode starts at, and the bytes of its
            output that come before the offset.
        """
        if self._is_mp3:
            rate = self.output_format.sample_rate
            run_in_frames = -(-RUN_IN_SECONDS * rate // MP3_FRAME_SAMPLES)
            block = MP3_BLOCK_FRAMES[rate]
            frame = max(mp3_frame_at(offset, rate) - run_in_frames, 0) // block * block
            position_ms = frame * MP3_FRAME_SAMPLES * 1000 // rate
            return position_ms, offset - mp3_frame_offset(frame, rate)
        data_offset = offset - self.header_size
        second = max(data_offset // self.byte_rate - RUN_IN_SECONDS, 0)
        return second * 1000, data_offset - second * self.byte_rate

    def offset_of(self, pcm_size: int) -> int:
        """
        Return the offset in the file where an amount of the audio it is read from ends.

        :param pcm_size: Bytes of audio, in the PCM format the file reads.
        """
        return self._size_of(
            pcm_size * self.output_format.sample_rate // self.pcm_format.pcm_sample_size
        )

    def size_for(self, range_header: str | None) -> int:
        """
        Return the size of the file that a request is answered against.

        That is its size, unless a renderer looks for a tag at the end of a size the file
        had when it asked for the start.

        :param range_header: Value of the Range header, if the request has one.
        """
        if self._is_mp3 and (part := parse_range(range_header, self.size)) is not None:
            for size in self._earlier_sizes:
                if part.start == size - ID3V1_TAG_SIZE:
                    return size
        return self.size

    def end_at(self, size: int) -> None:
        """
        Set the real size of the file, when its audio ended before the declared size.

        :param size: Offset in the file where the audio ended.
        """
        if size < self.size:
            self._earlier_sizes.append(self.size)
            self.size = size

    def close(self) -> None:
        """Give up what the file keeps of the stream it is cut from."""
        if self.mix is not None:
            self.mix.close()

    def _stretch_at(self, offset: int) -> range:
        """
        Return the stretch of what the file has sent that an offset lies in.

        A player asks again for the byte after the last one it got, so that byte counts as
        well. An offset outside of what was sent lies in an empty stretch of its own.

        :param offset: Offset in the file.
        """
        return next(
            (sent for sent in self._sent if sent.start <= offset <= sent.stop),
            range(offset, offset),
        )

    def _size_of(self, frames: int) -> int:
        """
        Return the size of the file for an amount of audio.

        :param frames: Length of the audio in frames.
        """
        sample_rate = self.output_format.sample_rate
        if self._is_mp3:
            return mp3_frame_offset(mp3_frame_count(frames), sample_rate)
        return self.header_size + frames * self.byte_rate // sample_rate


class FileKey(NamedTuple):
    """What makes a file: a track of a playback session, from the position it starts at."""

    session_id: str
    queue_id: str
    queue_item_id: str
    start_position: int


class FileStreams:
    """The files of the tracks that players fetch."""

    def __init__(self) -> None:
        """Initialize instance."""
        self._files: dict[str, dict[FileKey, FileStream]] = {}

    def get(self, player_id: str, key: FileKey) -> FileStream | None:
        """
        Return the file a player fetches a track as, if it exists.

        :param player_id: The id the player fetches the file under.
        :param key: The track and the playback it belongs to.
        """
        files = self._files.get(player_id, {})
        if (file := files.get(key)) is not None:
            return file
        # a fade into a track moves the position the track starts at
        return next(
            (
                file
                for known, file in files.items()
                if file.mix is not None and known[:3] == key[:3]
            ),
            None,
        )

    def add(self, player_id: str, key: FileKey, file: FileStream) -> None:
        """
        Add the file a player fetches a track as, in place of the oldest it has too many.

        :param player_id: The id the player fetches the file under.
        :param key: The track and the playback it belongs to.
        :param file: The file.
        """
        files = self._files.setdefault(player_id, {})
        # the track that plays has a request open, and keeps its file
        unread = [known for known, other in files.items() if not other.readers]
        files[key] = file
        for known in unread[: max(len(files) - MAX_FILES_PER_PLAYER, 0)]:
            files.pop(known).close()
        # the audio of a mix is only kept for the track that plays and the one after it
        for known in list(files)[:-MAX_MIXED_FILES_PER_PLAYER]:
            if files[known].mix is not None and known in unread:
                files.pop(known).close()

    def release(self, queue_id: str, current_session_id: str | None) -> None:
        """
        Release the files of a queue that belong to a session it moved past.

        :param queue_id: The queue whose files to release.
        :param current_session_id: The session that owns playback now, if any.
        """
        for player_id, files in list(self._files.items()):
            for key in list(files):
                if key.queue_id == queue_id and key.session_id != current_session_id:
                    files.pop(key).close()
            if not files:
                del self._files[player_id]
