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
from copy import copy
from typing import TYPE_CHECKING, Final, NamedTuple

from music_assistant_models.enums import ContentType

from music_assistant.controllers.streams.constants import REALTIME_BURST
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

# Seconds of audio a file hands over ahead of playback, and how much of that one encode
# gets: the burst of any other stream. Every part a player asks for is paced like any
# other stream, so a player that seeks often ends up at playback speed. See the usage
# policy.
HEAD_START_PER_FILE: Final[int] = 30
HEAD_START_PER_ENCODE: Final[int] = REALTIME_BURST

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

    Exact once the whole item has been decoded, else the item's duration plus one second:
    durations are whole seconds, and a file that is declared too long only ends early
    while one that is declared too short cuts music.

    :param queue_item: The queue item, with its stream details resolved.
    :param sample_rate: Sample rate of the file.
    """
    streamdetails = queue_item.streamdetails
    assert streamdetails is not None
    assert queue_item.duration is not None
    buffer = streamdetails.buffer
    seconds: float = queue_item.duration + 1
    if buffer is not None and buffer.eof and not buffer.has_error:
        seconds = buffer.first_buffered_chunk + buffer.duration_available
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

    def __init__(self, pcm_format: AudioFormat, output_format: AudioFormat, frames: int) -> None:
        """
        Initialize the file.

        :param pcm_format: PCM format the audio is read in.
        :param output_format: Format of the file.
        :param frames: Length of the audio the file declares, in frames.
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
        self._head_start = HEAD_START_PER_FILE
        # the head start the running encode of each address took and has not handed out yet
        self._head_start_held: dict[str | None, int] = {}
        # the request an encode of this file runs for, by the address it comes from
        self.readers: dict[str | None, web.Request] = {}
        # the offsets the file has sent in one go since a request last took it over: from
        # where that request started up to the furthest byte handed to a player since
        self._sent: range | None = None
        # what it had sent when that request took it over
        self._sent_before: range | None = None
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
        run when its part starts in what the file has sent since a request last took it
        over: it gets audio that left before, and then runs along. A request for any other
        part takes the file over, which ends every encode that runs. When that part starts
        in what the file had sent before the last take-over, it goes on from that.

        :param request: The request the encode runs for.
        :param offset: Offset in the file of the first byte the encode delivers.
        """
        # audio that was not sent yet only leaves through the encodes that read on from what
        # was, at playback pace: no two addresses read different parts of a track at once.
        # See the usage policy
        takes_over = self._sent is None or offset not in self._sent
        if takes_over and self._sent_before is not None and offset in self._sent_before:
            # a player that a take-over ended asks again for where it was. One of the two
            # counts as sent at a time, so the pace holds
            self._sent, self._sent_before = self._sent_before, self._sent
        elif takes_over:
            self._sent, self._sent_before = range(offset, offset + 1), self._sent
        # a player that asks for another part has left the one it was reading. Aborted
        # rather than closed, as a graceful close waits for the very send buffer the player
        # stopped reading
        for address, running in self.readers.items():
            if (takes_over or address == request.remote) and running.transport is not None:
                running.transport.abort()
        self.readers[request.remote] = request

    def sent_up_to(self, offset: int) -> None:
        """
        Take note of how far an encode that runs has got.

        :param offset: Offset in the file up to where its bytes were handed to its player.
        """
        assert self._sent is not None
        # a player asks again for the byte after the last one it got
        self._sent = range(self._sent.start, max(self._sent.stop, offset + 1))

    def remove_reader(self, request: web.Request) -> None:
        """
        Let go of a request whose encode ended, with the head start it did not hand out.

        :param request: The request the encode ran for.
        """
        if self.readers.get(request.remote) is request:
            del self.readers[request.remote]
            self._return_head_start(request.remote)

    def take_head_start(self, address: str | None) -> int:
        """
        Return the seconds of head start a new encode gets, out of what the file has left.

        The head start of the encode it replaces goes back to the file when that one handed
        no audio to its player.

        :param address: The address the request of the encode comes from.
        """
        self._return_head_start(address)
        seconds = min(HEAD_START_PER_ENCODE, self._head_start)
        self._head_start -= seconds
        self._head_start_held[address] = seconds
        return seconds

    def head_start_handed_out(self, address: str | None) -> None:
        """
        Mark that the encode of an address handed audio to its player, so it keeps its head start.

        :param address: The address the request of the encode comes from.
        """
        self._head_start_held.pop(address, None)

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

    def _return_head_start(self, address: str | None) -> None:
        """
        Give the head start of the encode of an address back, as it handed no audio out.

        :param address: The address the request of the encode comes from.
        """
        self._head_start += self._head_start_held.pop(address, 0)

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
