"""Test we can parse Jellyfin models into Music Assistant models."""

import logging
import pathlib
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

import aiofiles
import aiohttp
import pytest
from aiojellyfin import Artist, Connection
from aiojellyfin.session import SessionConfiguration
from mashumaro.codecs.json import JSONDecoder
from music_assistant_models.enums import ContentType

from music_assistant.providers.jellyfin.const import (
    ITEM_KEY_CONTAINER,
    ITEM_KEY_MEDIA_CHANNELS,
    ITEM_KEY_MEDIA_CODEC,
    ITEM_KEY_MEDIA_SOURCES,
    ITEM_KEY_MEDIA_STREAM_TYPE,
    ITEM_KEY_MEDIA_STREAMS,
)
from music_assistant.providers.jellyfin.parsers import (
    audio_format,
    parse_album,
    parse_artist,
    parse_track,
)

if TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"
ARTIST_FIXTURES = list(FIXTURES_DIR.glob("artists/*.json"))
ALBUM_FIXTURES = list(FIXTURES_DIR.glob("albums/*.json"))
TRACK_FIXTURES = list(FIXTURES_DIR.glob("tracks/*.json"))

ARTIST_DECODER = JSONDecoder(Artist)

_LOGGER = logging.getLogger(__name__)


@pytest.fixture
async def connection() -> AsyncGenerator[Connection]:
    """Spin up a dummy connection."""
    async with aiohttp.ClientSession() as session:
        session_config = SessionConfiguration(
            session=session,
            url="http://localhost:1234",
            app_name="X",
            app_version="0.0.0",
            device_id="X",
            device_name="localhost",
        )
        yield Connection(session_config, "USER_ID", "ACCESS_TOKEN")


@pytest.mark.parametrize("example", ARTIST_FIXTURES, ids=lambda val: str(val.stem))
async def test_parse_artists(
    example: pathlib.Path, connection: Connection, snapshot: SnapshotAssertion
) -> None:
    """Test we can parse artists."""
    async with aiofiles.open(example, encoding="utf-8") as fp:
        raw_data = ARTIST_DECODER.decode(await fp.read())
    parsed = parse_artist(_LOGGER, "xx-instance-id-xx", connection, raw_data).to_dict()
    # sort external Ids to ensure they are always in the same order for snapshot testing
    parsed["external_ids"].sort()
    assert snapshot == parsed


@pytest.mark.parametrize("example", ALBUM_FIXTURES, ids=lambda val: str(val.stem))
async def test_parse_albums(
    example: pathlib.Path, connection: Connection, snapshot: SnapshotAssertion
) -> None:
    """Test we can parse albums."""
    async with aiofiles.open(example, encoding="utf-8") as fp:
        raw_data = ARTIST_DECODER.decode(await fp.read())
    parsed = parse_album(_LOGGER, "xx-instance-id-xx", connection, raw_data).to_dict()
    # sort external Ids to ensure they are always in the same order for snapshot testing
    parsed["external_ids"].sort()
    assert snapshot == parsed


@pytest.mark.parametrize("example", TRACK_FIXTURES, ids=lambda val: str(val.stem))
async def test_parse_tracks(
    example: pathlib.Path, connection: Connection, snapshot: SnapshotAssertion
) -> None:
    """Test we can parse tracks."""
    async with aiofiles.open(example, encoding="utf-8") as fp:
        raw_data = ARTIST_DECODER.decode(await fp.read())
    parsed = parse_track(_LOGGER, "xx-instance-id-xx", connection, raw_data).to_dict()
    # sort external Ids to ensure they are always in the same order for snapshot testing
    parsed["external_ids"]
    assert snapshot == parsed


def test_audio_format_empty_mediastreams() -> None:
    """Test audio_format handles empty MediaStreams array."""
    # Track with empty MediaStreams
    track: dict[str, Any] = {
        ITEM_KEY_MEDIA_STREAMS: [],
    }
    result = audio_format(track)  # type: ignore[arg-type]

    # Verify no exception is raised and result has expected attributes
    assert result is not None
    assert hasattr(result, "content_type")


def test_audio_format_missing_channels() -> None:
    """Test audio_format applies default when Channels field is missing."""
    # Track with MediaStreams but missing Channels
    track: dict[str, Any] = {
        ITEM_KEY_MEDIA_SOURCES: [{ITEM_KEY_CONTAINER: "mp3"}],
        ITEM_KEY_MEDIA_STREAMS: [
            {
                ITEM_KEY_MEDIA_STREAM_TYPE: "Audio",
                ITEM_KEY_MEDIA_CODEC: "mp3",
                "SampleRate": 48000,
                "BitDepth": 16,
                "BitRate": 320000,
            }
        ],
    }
    result = audio_format(track)  # type: ignore[arg-type]

    # Verify defaults are applied correctly
    assert result is not None
    assert result.channels == 2  # Default stereo
    assert result.sample_rate == 48000
    assert result.bit_depth == 16
    assert result.bit_rate == 320  # AudioFormat converts bps to kbps automatically


def test_audio_format_wav_container_not_treated_as_raw_pcm() -> None:
    """A WAV/PCM source must report the container so ffmpeg does not read it as raw PCM."""
    # content_type must be the container (wav), not the codec (pcm_s24le): a PCM content_type
    # makes the ffmpeg pipeline read the stream as headerless raw PCM, so the WAV header and
    # embedded cover art get decoded as samples -> white noise. The embedded cover art is also
    # why the audio stream is not first: the parser must pick the stream by Type, not index 0.
    track: dict[str, Any] = {
        ITEM_KEY_MEDIA_SOURCES: [{ITEM_KEY_CONTAINER: "wav"}],
        ITEM_KEY_MEDIA_STREAMS: [
            {ITEM_KEY_MEDIA_STREAM_TYPE: "Video", ITEM_KEY_MEDIA_CODEC: "mjpeg"},
            {
                ITEM_KEY_MEDIA_STREAM_TYPE: "Audio",
                ITEM_KEY_MEDIA_CODEC: "pcm_s24le",
                ITEM_KEY_MEDIA_CHANNELS: 2,
                "SampleRate": 48000,
                "BitDepth": 24,
            },
        ],
    }
    result = audio_format(track)  # type: ignore[arg-type]

    assert result.content_type == ContentType.WAV
    assert not result.content_type.is_pcm()
    assert result.codec_type == ContentType.PCM_S24LE
    assert result.sample_rate == 48000
    assert result.bit_depth == 24
    assert result.channels == 2
