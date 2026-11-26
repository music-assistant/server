"""Tests for the file a track is served as to a player that fetches files."""

from __future__ import annotations

import struct
from collections import Counter
from itertools import pairwise
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.streams.file_stream import (
    HEAD_SIZE,
    FileKey,
    FileStream,
    FileStreams,
    declared_frames,
    mp3_frame_at,
    mp3_frame_count,
    mp3_frame_offset,
    parse_range,
)
from music_assistant.helpers.ffmpeg import get_ffmpeg_args

FILE_SIZE = 1000
PCM_FORMAT = AudioFormat(content_type=ContentType.PCM_S16LE, sample_rate=44100, bit_depth=16)
WAV_FORMAT = AudioFormat(content_type=ContentType.WAV, sample_rate=44100, bit_depth=16)
BYTES_PER_SECOND = 44100 * 4
MP3_44K = AudioFormat(content_type=ContentType.MP3, sample_rate=44100, bit_depth=16)
MP3_48K = AudioFormat(content_type=ContentType.MP3, sample_rate=48000, bit_depth=16)
# what the padding of the frames of an MP3 comes round in at 44.1 kHz: 49 frames
MP3_44K_BLOCK = 51200
# every frame of an MP3 at 48 kHz
MP3_48K_FRAME = 960


def _file(
    frames: int = 60 * 44100,
    output_format: AudioFormat = WAV_FORMAT,
    pcm_format: AudioFormat = PCM_FORMAT,
) -> FileStream:
    """
    Build a file.

    :param frames: Length of the audio the file declares.
    :param output_format: Format of the file.
    :param pcm_format: Format the audio of the file is read in.
    """
    return FileStream(pcm_format, output_format, frames)


def _queue_item(
    duration: int, seek_position: int = 0, buffer: MagicMock | None = None
) -> QueueItem:
    """
    Build a queue item with resolved stream details.

    :param duration: The duration of the item, in whole seconds as a provider gives it.
    :param seek_position: The position the item starts at.
    :param buffer: The buffer that holds the decoded audio of the item, if any.
    """
    queue_item = QueueItem(queue_id="q1", queue_item_id="item1", name="item1", duration=duration)
    queue_item.streamdetails = StreamDetails(
        provider="local--1",
        item_id="item1",
        audio_format=AudioFormat(content_type=ContentType.FLAC),
        media_type=MediaType.TRACK,
        stream_type=StreamType.LOCAL_FILE,
        path="/music/item1.flac",
        duration=duration,
    )
    queue_item.streamdetails.seek_position = seek_position
    queue_item.streamdetails.buffer = buffer
    return queue_item


def _buffer(seconds: float, *, eof: bool = True, has_error: bool = False) -> MagicMock:
    """
    Build a buffer that holds decoded audio from the start of a track.

    :param seconds: The audio the buffer holds.
    :param eof: Whether the source of the buffer was read out.
    :param has_error: Whether that source failed.
    """
    return MagicMock(
        eof=eof, has_error=has_error, first_buffered_chunk=0, duration_available=seconds
    )


@pytest.mark.parametrize(
    ("header", "part"),
    [
        ("bytes=0-", range(FILE_SIZE)),
        ("bytes=100-", range(100, FILE_SIZE)),
        ("bytes=100-199", range(100, 200)),
        ("bytes=100-100", range(100, 101)),
        ("bytes=100-5000", range(100, FILE_SIZE)),
        ("bytes=-128", range(FILE_SIZE - 128, FILE_SIZE)),
        ("bytes=-5000", range(FILE_SIZE)),
        (" bytes=16384- ", range(16384, FILE_SIZE)),
    ],
)
def test_a_range_header_names_a_part_of_the_file(header: str, part: range) -> None:
    """A part is asked from a byte on, as a closed range or as the last bytes of the file."""
    assert parse_range(header, FILE_SIZE) == part


@pytest.mark.parametrize("header", ["bytes=1000-", "bytes=2000-2100", "bytes=200-100", "bytes=-0"])
def test_a_part_at_or_past_the_end_is_empty(header: str) -> None:
    """A part that starts at or past the end of the file holds nothing."""
    part = parse_range(header, FILE_SIZE)

    assert part is not None
    assert len(part) == 0


@pytest.mark.parametrize("header", [None, "", "bytes=-", "items=0-5", "bytes=0-1,5-6", "bytes=a-"])
def test_a_header_that_names_no_part_asks_for_the_whole_file(header: str | None) -> None:
    """Without a usable Range header the request is for the whole file."""
    assert parse_range(header, FILE_SIZE) is None


@pytest.mark.parametrize(
    ("output_format", "frame_size"),
    [
        (WAV_FORMAT, 4),
        (AudioFormat(content_type=ContentType.WAV, sample_rate=96000, bit_depth=24), 6),
        (AudioFormat(content_type=ContentType.WAV, sample_rate=48000, bit_depth=16, channels=1), 2),
    ],
    ids=["44.1k-16", "96k-24", "mono"],
)
def test_a_file_is_a_header_with_the_real_sizes_and_the_samples(
    output_format: AudioFormat, frame_size: int
) -> None:
    """The size of a file is the 44 byte header plus its frames, and the header says so."""
    frames = 12345

    file = _file(frames, output_format)
    header = file.head(range(file.size))

    assert file.header_size == len(header) == 44
    assert file.size == 44 + frames * frame_size
    assert header[:4] == b"RIFF"
    assert struct.unpack("<L", header[4:8])[0] == file.size - 8
    # format tag 1 (PCM), which every player accepts, also above 16 bit
    assert struct.unpack("<H", header[20:22])[0] == 1
    assert struct.unpack("<L", header[24:28])[0] == output_format.sample_rate
    assert struct.unpack("<H", header[32:34])[0] == frame_size
    assert struct.unpack("<L", header[40:44])[0] == frames * frame_size


def test_a_file_past_four_gigabytes_fills_the_size_fields_of_its_header() -> None:
    """The header of a file that is larger than its size fields can hold carries their maximum."""
    file = _file(frames=2**31)

    header = file.head(range(44))

    assert file.size == 44 + 2**33
    assert struct.unpack("<L", header[4:8])[0] == 0xFFFFFFFF
    assert struct.unpack("<L", header[40:44])[0] == 0xFFFFFFFF


@pytest.mark.parametrize(
    ("data_offset", "second", "run_in"),
    [
        # the start of the file has no audio before it
        (0, 0, 0),
        (BYTES_PER_SECOND // 2, 0, BYTES_PER_SECOND // 2),
        (2 * BYTES_PER_SECOND - 4, 0, 2 * BYTES_PER_SECOND - 4),
        # from there on an encode starts on the whole second that leaves it a second or more
        (2 * BYTES_PER_SECOND, 1, BYTES_PER_SECOND),
        (10 * BYTES_PER_SECOND + 4, 9, BYTES_PER_SECOND + 4),
        (11 * BYTES_PER_SECOND - 4, 9, 2 * BYTES_PER_SECOND - 4),
        # a player may ask for a byte in the middle of a frame
        (16384 * 100 - 44, 8, 16384 * 100 - 44 - 8 * BYTES_PER_SECOND),
        (30 * BYTES_PER_SECOND + 3, 29, BYTES_PER_SECOND + 3),
    ],
)
def test_an_encode_starts_on_a_whole_second_before_the_part(
    data_offset: int, second: int, run_in: int
) -> None:
    """An offset maps to the second an encode starts at and the output to drop before it."""
    file = _file()

    assert file.encode_start(file.header_size + data_offset) == (second * 1000, run_in)


@pytest.mark.parametrize(
    ("samples", "frames"),
    [(1, 2), (1151, 2), (1152, 2), (1153, 3), (2304, 3), (60 * 44100, 2298), (60 * 48000, 2501)],
)
def test_an_mp3_has_one_frame_more_than_its_audio_fills(samples: int, frames: int) -> None:
    """The last frame of the audio is a whole one, and the encoder ends with a frame more."""
    assert mp3_frame_count(samples) == frames


def test_the_frames_of_an_mp3_at_48_khz_are_all_the_same_size() -> None:
    """At 48 kHz a frame is a whole number of bytes."""
    offsets = [mp3_frame_offset(frame, 48000) for frame in (0, 1, 2, 2501)]

    assert offsets == [0, MP3_48K_FRAME, 2 * MP3_48K_FRAME, 2501 * MP3_48K_FRAME]


def test_the_frames_of_an_mp3_at_44_khz_follow_the_padding_of_the_encoder() -> None:
    """At 44.1 kHz a frame is 1044 bytes or one more, in a pattern that comes round."""
    offsets = [mp3_frame_offset(frame, 44100) for frame in range(99)]
    sizes = [end - start for start, end in pairwise(offsets)]

    assert offsets[:4] == [0, 1044, 2089, 3134]
    assert offsets[49] == MP3_44K_BLOCK
    assert Counter(sizes[:49]) == {1045: 44, 1044: 5}
    assert sizes[49:] == sizes[:49]
    assert mp3_frame_offset(2298, 44100) == 46 * MP3_44K_BLOCK + offsets[44]


@pytest.mark.parametrize("sample_rate", [44100, 48000])
def test_a_byte_of_an_mp3_belongs_to_the_frame_it_is_in(sample_rate: int) -> None:
    """Every byte of a frame, from its first to its last, maps to that frame."""
    for frame in range(150):
        first = mp3_frame_offset(frame, sample_rate)
        last = mp3_frame_offset(frame + 1, sample_rate) - 1

        assert mp3_frame_at(first, sample_rate) == mp3_frame_at(last, sample_rate) == frame


@pytest.mark.parametrize(
    ("output_format", "frames", "size"),
    [
        (MP3_48K, 60 * 48000, 2501 * MP3_48K_FRAME),
        # 46 times the 49 frames the padding comes round in, and 44 frames
        (MP3_44K, 60 * 44100, 46 * MP3_44K_BLOCK + 45975),
        (
            AudioFormat(content_type=ContentType.MP3, sample_rate=48000, bit_depth=16, channels=1),
            60 * 48000,
            2501 * MP3_48K_FRAME,
        ),
    ],
    ids=["48k", "44.1k", "mono"],
)
def test_an_mp3_file_is_its_frames_and_nothing_else(
    output_format: AudioFormat, frames: int, size: int
) -> None:
    """The size of an MP3 file is that of its frames at 320 kbit/s: it has no header or tag."""
    file = _file(frames, output_format)

    assert file.header_size == 0
    assert file.head(range(HEAD_SIZE)) == b""
    assert file.size == size
    assert file.byte_rate == 40000


@pytest.mark.parametrize(
    ("output_format", "offset", "position_ms", "run_in"),
    [
        (MP3_44K, 0, 0, 0),
        # frame 87: the block that leaves it a second starts the file
        (MP3_44K, 91950, 0, 91950),
        # frame 88, a second and a frame past the block of 49 frames
        (MP3_44K, 91951, 1280, 91951 - MP3_44K_BLOCK),
        # a player may ask for a byte in the middle of a frame, or one a block starts on
        (MP3_44K, 1500001, 28 * 1280, 1500001 - 28 * MP3_44K_BLOCK),
        (MP3_44K, 32 * MP3_44K_BLOCK, 31 * 1280, MP3_44K_BLOCK),
        (MP3_48K, 0, 0, 0),
        (MP3_48K, 47 * MP3_48K_FRAME - 1, 0, 47 * MP3_48K_FRAME - 1),
        # at 48 kHz a block is 5 frames, which are 120 milliseconds
        (MP3_48K, 47 * MP3_48K_FRAME, 120, 42 * MP3_48K_FRAME),
        (MP3_48K, 16384 * 100, 332 * 120, 16384 * 100 - 1660 * MP3_48K_FRAME),
    ],
)
def test_an_mp3_encode_starts_on_a_block_a_second_or_more_before_the_part(
    output_format: AudioFormat, offset: int, position_ms: int, run_in: int
) -> None:
    """An offset maps to the block of frames an encode starts on and the output to drop."""
    file = _file(output_format=output_format)

    assert file.encode_start(offset) == (position_ms, run_in)


@pytest.mark.parametrize("output_format", [MP3_44K, MP3_48K])
def test_an_mp3_encode_starts_on_a_whole_frame_of_every_sample_rate(
    output_format: AudioFormat,
) -> None:
    """Audio that is resampled on its way is read from a whole frame before and after it."""
    file = _file(60 * output_format.sample_rate, output_format)

    for offset in range(0, file.size, 16384):
        position_ms, _ = file.encode_start(offset)
        for sample_rate in (22050, 44100, 48000, 88200, 96000, 192000):
            assert position_ms * sample_rate % 1000 == 0


def test_the_last_bytes_of_an_mp3_are_known_without_an_encode() -> None:
    """Where a renderer looks for a tag there is none, which takes no audio to tell."""
    mp3, wav = _file(output_format=MP3_44K), _file()

    assert mp3.head(range(mp3.size - 128, mp3.size)) == bytes(128)
    assert mp3.head(range(mp3.size - 100, mp3.size - 50)) == bytes(50)
    assert mp3.head(range(mp3.size - 129, mp3.size)) == b""
    assert wav.head(range(wav.size - 128, wav.size)) == b""

    # an MP3 whose audio ended early has its last bytes there
    mp3.end_at(mp3.size - 4000)
    assert mp3.head(range(mp3.size - 128, mp3.size)) == bytes(128)


def test_an_mp3_answers_a_look_for_a_tag_at_the_end_of_a_size_it_had() -> None:
    """A renderer looks for a tag at the end of the size it was told with the start."""
    mp3, wav = _file(output_format=MP3_44K), _file()
    declared = mp3.size
    mp3.end_at(declared - 4000)

    assert mp3.size_for(f"bytes={declared - 128}-") == declared
    assert mp3.head(range(declared - 128, declared)) == bytes(128)
    # every other request is answered against the real size
    for header in (None, "bytes=0-", "bytes=-128", f"bytes={mp3.size}-", f"bytes={declared - 1}-"):
        assert mp3.size_for(header) == mp3.size

    declared = wav.size
    wav.end_at(declared - 4000)
    assert wav.size_for(f"bytes={declared - 128}-") == wav.size


@pytest.mark.parametrize(
    "pcm_format",
    [
        PCM_FORMAT,
        AudioFormat(content_type=ContentType.PCM_S24LE, sample_rate=96000, bit_depth=24),
        AudioFormat(content_type=ContentType.PCM_F32LE, sample_rate=48000, bit_depth=32),
    ],
    ids=["16-bit", "24-bit-resampled", "float"],
)
def test_an_mp3_is_encoded_without_reservoir_tag_or_dither(pcm_format: AudioFormat) -> None:
    """Nothing in an encode may differ from the one before: no frame leans on another."""
    file = _file(output_format=MP3_48K, pcm_format=pcm_format)

    args = get_ffmpeg_args(
        file.pcm_format, file.encode_format, [], extra_output_args=file.encoder_args
    )

    encoder = args[args.index("mp3") - 1 :]
    assert encoder == [
        "-f",
        "mp3",
        "-b:a",
        "320k",
        "-write_xing",
        "0",
        "-id3v2_version",
        "0",
        "-reservoir",
        "0",
        "-",
    ]
    assert not any("dither" in arg for arg in args)
    # the file itself is what the player asked for
    assert file.output_format is MP3_48K
    assert file.output_format.bit_depth == 16


def test_a_wav_is_encoded_as_samples_for_the_header_of_the_file() -> None:
    """The encode of a WAV file delivers the samples that follow the header the file has."""
    file = _file()

    assert file.encode_format.content_type == ContentType.PCM_S16LE
    assert file.encoder_args == []
    assert file.output_format is WAV_FORMAT


@pytest.mark.parametrize(
    ("output_format", "pcm_format"),
    [
        (MP3_44K, PCM_FORMAT),
        (MP3_48K, AudioFormat(content_type=ContentType.PCM_F32LE, sample_rate=96000, bit_depth=32)),
        (
            WAV_FORMAT,
            AudioFormat(content_type=ContentType.PCM_S24LE, sample_rate=96000, bit_depth=24),
        ),
    ],
    ids=["mp3", "mp3-resampled", "wav-resampled"],
)
def test_an_amount_of_audio_maps_to_the_file_of_that_length(
    output_format: AudioFormat, pcm_format: AudioFormat
) -> None:
    """Audio that ends early leaves the file as large as one that declared that length."""
    file = _file(60 * output_format.sample_rate, output_format, pcm_format)
    shorter = _file(round(31.7 * output_format.sample_rate), output_format, pcm_format)
    frame_size = pcm_format.pcm_sample_size // pcm_format.sample_rate

    assert file.offset_of(round(31.7 * pcm_format.sample_rate) * frame_size) == shorter.size
    assert file.offset_of(60 * pcm_format.pcm_sample_size) == file.size


def test_the_start_of_a_file_is_kept_as_it_is_sent() -> None:
    """Bytes that continue the start in memory are kept, anything else is not."""
    assert not _file().has_audio_in_memory
    file = _file()

    file.keep(44, b"a" * 100)
    # a part further on leaves a hole, and a part that was kept before adds nothing
    file.keep(500, b"b" * 100)
    file.keep(44, b"c" * 100)
    file.keep(144, b"d" * 50)

    assert file.has_audio_in_memory
    assert file.head(range(44, file.size)) == b"a" * 100 + b"d" * 50
    assert file.head(range(100, 150)) == b"a" * 44 + b"d" * 6
    assert file.head(range(194, 300)) == b""


def test_the_start_that_is_kept_ends_at_one_megabyte() -> None:
    """Memory holds the first megabyte of a file, header included, and nothing after it."""
    file = _file()

    file.keep(44, b"a" * (HEAD_SIZE - 100))
    file.keep(HEAD_SIZE - 56, b"b" * 500)
    file.keep(HEAD_SIZE, b"c" * 500)

    head = file.head(range(file.size))
    assert len(head) == HEAD_SIZE
    assert head.endswith(b"a" + b"b" * 56)


def test_a_file_hands_out_thirty_seconds_of_head_start_six_per_encode() -> None:
    """The first five encodes of a file get six seconds, the ones after that none."""
    file = _file()

    assert [file.take_head_start() for _ in range(7)] == [6, 6, 6, 6, 6, 0, 0]


def test_audio_that_ends_early_sets_the_real_size() -> None:
    """A file shrinks to where its audio ended, and never grows past what it declared."""
    file = _file(frames=1000)
    declared = file.size

    file.end_at(declared + 400)
    assert file.size == declared

    file.end_at(declared - 400)
    assert file.size == declared - 400


def test_a_decoded_track_declares_its_exact_length() -> None:
    """A track whose audio was decoded to its end declares that length."""
    queue_item = _queue_item(duration=60, buffer=_buffer(60.4))

    assert declared_frames(queue_item, 44100) == round(60.4 * 44100)


@pytest.mark.parametrize(
    "buffer",
    [None, _buffer(12.0, eof=False), _buffer(31.5, has_error=True)],
    ids=["no-buffer", "still-decoding", "source-failed"],
)
def test_a_track_that_is_not_decoded_yet_declares_a_second_more_than_its_duration(
    buffer: MagicMock | None,
) -> None:
    """Durations are whole seconds, so a second is added rather than music cut off."""
    queue_item = _queue_item(duration=60, buffer=buffer)

    assert declared_frames(queue_item, 48000) == 61 * 48000


def test_a_track_that_starts_at_a_position_declares_what_is_left_of_it() -> None:
    """A file starts where its item starts."""
    assert declared_frames(_queue_item(duration=60, seek_position=45), 44100) == 16 * 44100
    decoded = _queue_item(duration=60, seek_position=45, buffer=_buffer(60.4))
    assert declared_frames(decoded, 44100) == round(15.4 * 44100)
    assert declared_frames(_queue_item(duration=60, seek_position=90), 44100) == 0


def test_a_player_keeps_three_files_at_most() -> None:
    """A new file takes the place of the oldest one of its player."""
    files = FileStreams()
    keys = [FileKey("s1", "q1", f"item{index}", 0) for index in range(4)]
    for key in keys[:3]:
        files.add("player1", key, _file())
    files.add("player2", keys[0], _file())

    files.add("player1", keys[3], _file())

    assert files.get("player1", keys[0]) is None
    assert all(files.get("player1", key) is not None for key in keys[1:])
    assert files.get("player2", keys[0]) is not None


def test_a_file_that_a_player_reads_keeps_its_place() -> None:
    """The track that plays is not the one that makes room, however old its file is."""
    files = FileStreams()
    keys = [FileKey("s1", "q1", f"item{index}", 0) for index in range(4)]
    playing = _file()
    playing.request = MagicMock()
    files.add("player1", keys[0], playing)
    for key in keys[1:3]:
        files.add("player1", key, _file())

    files.add("player1", keys[3], _file())

    assert files.get("player1", keys[0]) is playing
    assert files.get("player1", keys[1]) is None
    assert all(files.get("player1", key) is not None for key in keys[2:])


def test_a_file_is_one_track_of_one_playback_from_one_position() -> None:
    """Another session, track or start position is another file."""
    files = FileStreams()
    key = FileKey("s1", "q1", "item1", 0)
    file = _file()
    files.add("player1", key, file)

    assert files.get("player1", key) is file
    assert files.get("player1", key._replace(session_id="s2")) is None
    assert files.get("player1", key._replace(queue_item_id="item2")) is None
    assert files.get("player1", key._replace(start_position=30)) is None
    assert files.get("player2", key) is None


def test_a_queue_that_stops_releases_its_files() -> None:
    """The files of a queue go with their session, those of another queue stay."""
    files = FileStreams()
    stopped = FileKey("s1", "q1", "item1", 0)
    current = FileKey("s2", "q1", "item1", 30)
    other_queue = FileKey("s3", "q2", "item9", 0)
    for player_id, key in (("p1", stopped), ("p1", current), ("p2", other_queue)):
        files.add(player_id, key, _file())

    files.release("q1", "s2")

    assert files.get("p1", stopped) is None
    assert files.get("p1", current) is not None
    assert files.get("p2", other_queue) is not None

    files.release("q1", None)

    assert files.get("p1", current) is None
    assert files.get("p2", other_queue) is not None
