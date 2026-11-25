"""
A track served to a player as a file.

Some players are built around files: they want the length of a track up front, and they
pause, resume and seek by asking for a part of the file. For such a player a track becomes
a WAV file with a declared length. The file is virtual: every part is encoded while it is
sent, and only the start of the file is kept in memory.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Final, NamedTuple

from music_assistant.helpers.audio import create_streaming_wave_header

if TYPE_CHECKING:
    from aiohttp import web
    from music_assistant_models.media_items import AudioFormat
    from music_assistant_models.queue_item import QueueItem

# Bytes of the start of a file that are kept in memory. Players ask for the start again
# before they resume or seek, and mostly drop that request.
HEAD_SIZE: Final[int] = 1024 * 1024

# Seconds of audio a file hands over ahead of playback, and how much of that one encode
# gets. Every part a player asks for is paced like any other stream, so a player that
# seeks often ends up at playback speed. See the usage policy.
HEAD_START_PER_FILE: Final[int] = 30
HEAD_START_PER_ENCODE: Final[int] = 10

# Seconds an encode starts before the part it is asked for. Filters in the chain (DSP, a
# resampler) need that audio to settle, so the part comes out as it would in one pass.
RUN_IN_SECONDS: Final[int] = 1

# The track that plays and its neighbours.
MAX_FILES_PER_PLAYER: Final[int] = 3

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
    One track as the WAV file a player fetches.

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
        self.byte_rate = output_format.pcm_sample_size
        data_size = frames * output_format.channels * (output_format.bit_depth // 8)
        self._head = bytearray(create_streaming_wave_header(output_format, data_size))
        self.header_size = len(self._head)
        self.size = self.header_size + data_size
        self._head_start = HEAD_START_PER_FILE
        # the request an encode of this file runs for
        self.request: web.Request | None = None
        # whether the queue was told that the player loads this file
        self.loaded = False

    @property
    def has_audio_in_memory(self) -> bool:
        """Return whether the start that is kept in memory holds more than the header."""
        return len(self._head) > self.header_size

    def head(self, part: range) -> bytes:
        """
        Return the start of a part, as far as memory holds it.

        :param part: The bytes of the file a player asks for.
        """
        return bytes(self._head[part.start : part.stop])

    def keep(self, offset: int, data: bytes) -> None:
        """
        Keep bytes that were sent when they continue the start that is held in memory.

        :param offset: Offset in the file of the first of these bytes.
        :param data: The bytes.
        """
        if offset == len(self._head) < HEAD_SIZE:
            self._head += data[: HEAD_SIZE - offset]

    def take_head_start(self) -> int:
        """Return the seconds of head start a new encode gets, out of what the file has left."""
        seconds = min(HEAD_START_PER_ENCODE, self._head_start)
        self._head_start -= seconds
        return seconds

    def encode_start(self, offset: int) -> tuple[int, int]:
        """
        Return where an encode starts that delivers the file from an offset.

        An encode starts on a whole second: that is a whole frame in every format the audio
        passes through, so a part is cut out of the same samples as one pass over the file.

        :param offset: Offset in the file of the first byte to deliver, past the header.
        :return: The second of the file the encode starts at, and the bytes of its output
            that come before the offset.
        """
        data_offset = offset - self.header_size
        second = max(data_offset // self.byte_rate - RUN_IN_SECONDS, 0)
        return second, data_offset - second * self.byte_rate

    def end_at(self, size: int) -> None:
        """
        Set the real size of the file, when its audio ended before the declared size.

        :param size: Offset in the file where the audio ended.
        """
        self.size = min(self.size, size)


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

        :param player_id: The player that fetches the file.
        :param key: The track and the playback it belongs to.
        """
        return self._files.get(player_id, {}).get(key)

    def add(self, player_id: str, key: FileKey, file: FileStream) -> None:
        """
        Add the file a player fetches a track as, in place of the oldest it has too many.

        :param player_id: The player that fetches the file.
        :param key: The track and the playback it belongs to.
        :param file: The file.
        """
        files = self._files.setdefault(player_id, {})
        # the track that plays has a request open, and keeps its file
        unread = [known for known, other in files.items() if other.request is None]
        files[key] = file
        for known in unread[: max(len(files) - MAX_FILES_PER_PLAYER, 0)]:
            del files[known]

    def release(self, queue_id: str, current_session_id: str | None) -> None:
        """
        Release the files of a queue that belong to a session it moved past.

        :param queue_id: The queue whose files to release.
        :param current_session_id: The session that owns playback now, if any.
        """
        for player_id, files in list(self._files.items()):
            for key in list(files):
                if key.queue_id == queue_id and key.session_id != current_session_id:
                    del files[key]
            if not files:
                del self._files[player_id]
