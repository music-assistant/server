"""Test Tidal Streaming Manager."""

import base64
from collections.abc import AsyncGenerator, Coroutine
from sqlite3 import OperationalError
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from music_assistant_models.enums import ContentType, ExternalID, StreamType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import AudioFormat, Track

from music_assistant.providers.tidal.streaming import TidalStreamingManager

# A minimal valid DASH manifest for testing.
_TEST_MANIFEST_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static">'
    "<Period><AdaptationSet>"
    '<Representation id="1" codecs="flac" audioSamplingRate="44100">'
    '<SegmentTemplate initialization="https://cdn.tidal.com/init.mp4" '
    'media="https://cdn.tidal.com/$Number$.mp4" startNumber="1" timescale="44100">'
    "<SegmentTimeline>"
    '<S d="176128" r="2"/>'
    "</SegmentTimeline>"
    "</SegmentTemplate>"
    "</Representation>"
    "</AdaptationSet></Period></MPD>"
)
_TEST_MANIFEST_B64 = base64.b64encode(_TEST_MANIFEST_XML.encode()).decode()


@pytest.fixture
def provider_mock() -> Mock:
    """Return a mock provider."""
    provider = Mock()
    provider.domain = "tidal"
    provider.instance_id = "tidal_instance"
    provider.config.get_value.return_value = "HIGH"
    provider.api = AsyncMock()
    provider.api.OPEN_API_URL = "https://openapi.tidal.com/v2"

    # Mock throttler bypass as async context manager using MagicMock
    bypass_ctx = MagicMock()
    bypass_ctx.__aenter__ = AsyncMock(return_value=None)
    bypass_ctx.__aexit__ = AsyncMock(return_value=None)
    provider.api.throttler = Mock()
    provider.api.throttler.bypass = Mock(return_value=bypass_ctx)

    provider.get_track = AsyncMock()

    # Mock mass
    provider.mass = Mock()
    provider.mass.cache.get = AsyncMock(return_value=None)
    provider.mass.cache.set = AsyncMock()
    provider.mass.cache.delete = AsyncMock()
    provider.mass.music.tracks.get_library_item_by_prov_id = AsyncMock(return_value=None)
    return provider


@pytest.fixture
def streaming_manager(provider_mock: Mock) -> TidalStreamingManager:
    """Return a TidalStreamingManager instance."""
    return TidalStreamingManager(provider_mock)


@pytest.fixture
def mock_track() -> Mock:
    """Return a mock track."""
    track = Mock(spec=Track)
    track.item_id = "123"
    track.duration = 180
    return track


async def test_get_stream_details_lossless(
    streaming_manager: TidalStreamingManager, provider_mock: Mock, mock_track: Mock
) -> None:
    """Test get_stream_details with LOSSLESS quality."""
    provider_mock.get_track.return_value = mock_track
    provider_mock.api.get.return_value = (
        {
            "manifestMimeType": "application/vnd.tidal.bts",
            "urls": ["https://example.com/stream.flac"],
            "audioQuality": "LOSSLESS",
            "sampleRate": 44100,
            "bitDepth": 16,
        },
        None,
    )

    stream_details = await streaming_manager.get_stream_details("123")

    assert stream_details.item_id == "123"
    assert stream_details.provider == "tidal_instance"
    assert stream_details.audio_format.content_type == ContentType.FLAC
    assert stream_details.audio_format.sample_rate == 44100
    assert stream_details.audio_format.bit_depth == 16
    assert stream_details.stream_type == StreamType.HTTP
    assert stream_details.path == "https://example.com/stream.flac"
    assert stream_details.can_seek is True

    provider_mock.get_track.assert_called_with("123")
    provider_mock.api.get.assert_called_with(
        "tracks/123/playbackinfopostpaywall",
        params={
            "playbackmode": "STREAM",
            "assetpresentation": "FULL",
            "audioquality": "HIGH",
        },
    )


async def test_get_stream_details_hires(
    streaming_manager: TidalStreamingManager, provider_mock: Mock, mock_track: Mock
) -> None:
    """Test get_stream_details with HIRES_LOSSLESS quality."""
    provider_mock.get_track.return_value = mock_track
    provider_mock.api.get.return_value = {
        "urls": ["https://example.com/stream.flac"],
        "audioQuality": "HIRES_LOSSLESS",
        "sampleRate": 96000,
        "bitDepth": 24,
    }

    stream_details = await streaming_manager.get_stream_details("123")

    assert stream_details.audio_format.content_type == ContentType.FLAC
    assert stream_details.audio_format.sample_rate == 96000
    assert stream_details.audio_format.bit_depth == 24


async def test_get_stream_details_with_dash_manifest(
    streaming_manager: TidalStreamingManager, provider_mock: Mock, mock_track: Mock
) -> None:
    """Test get_stream_details with DASH manifest uses CUSTOM stream with parsed segments."""
    provider_mock.get_track.return_value = mock_track
    provider_mock.api.get.return_value = {
        "manifestMimeType": "application/dash+xml",
        "manifest": _TEST_MANIFEST_B64,
        "audioQuality": "HIGH",
        "sampleRate": 44100,
        "bitDepth": 16,
    }

    stream_details = await streaming_manager.get_stream_details("123")

    assert stream_details.stream_type == StreamType.CUSTOM
    assert stream_details.audio_format.content_type == ContentType.MP4
    assert stream_details.can_seek is True
    assert stream_details.allow_seek is True
    # Segment info stored in data field
    assert isinstance(stream_details.data, dict)
    assert stream_details.data["init_url"] == "https://cdn.tidal.com/init.mp4"
    segments = stream_details.data["segments"]
    assert len(segments) == 3
    assert segments[0]["url"] == "https://cdn.tidal.com/1.mp4"
    assert segments[0]["start"] == 0.0
    assert segments[2]["url"] == "https://cdn.tidal.com/3.mp4"


async def test_get_stream_details_with_codec(
    streaming_manager: TidalStreamingManager, provider_mock: Mock, mock_track: Mock
) -> None:
    """Test get_stream_details with codec specified."""
    provider_mock.get_track.return_value = mock_track
    provider_mock.api.get.return_value = {
        "urls": ["https://example.com/stream.aac"],
        "audioQuality": "HIGH",
        "codec": "AAC",
        "sampleRate": 44100,
        "bitDepth": 16,
    }

    stream_details = await streaming_manager.get_stream_details("123")

    assert stream_details.audio_format.content_type == ContentType.AAC


async def test_get_stream_details_defaults_to_mp4(
    streaming_manager: TidalStreamingManager, provider_mock: Mock, mock_track: Mock
) -> None:
    """Test get_stream_details defaults to MP4 when no quality/codec."""
    provider_mock.get_track.return_value = mock_track
    provider_mock.api.get.return_value = {
        "urls": ["https://example.com/stream.m4a"],
        "sampleRate": 44100,
        "bitDepth": 16,
    }

    stream_details = await streaming_manager.get_stream_details("123")

    assert stream_details.audio_format.content_type == ContentType.MP4


async def test_get_stream_details_no_urls_raises_error(
    streaming_manager: TidalStreamingManager, provider_mock: Mock, mock_track: Mock
) -> None:
    """Test get_stream_details raises error when no URLs."""
    provider_mock.get_track.return_value = mock_track
    provider_mock.api.get.return_value = {
        "audioQuality": "HIGH",
        "sampleRate": 44100,
        "bitDepth": 16,
    }

    with pytest.raises(MediaNotFoundError, match="No stream URL found"):
        await streaming_manager.get_stream_details("123")


async def test_get_stream_details_track_not_found_no_isrc(
    streaming_manager: TidalStreamingManager, provider_mock: Mock
) -> None:
    """Test get_stream_details when track not found and no ISRC fallback."""
    provider_mock.get_track.side_effect = MediaNotFoundError("Track not found")
    provider_mock.mass.music.tracks.get_library_item_by_prov_id.return_value = None

    with pytest.raises(MediaNotFoundError, match="Track 123 not found"):
        await streaming_manager.get_stream_details("123")


async def test_get_track_by_isrc_from_cache(
    streaming_manager: TidalStreamingManager, provider_mock: Mock, mock_track: Mock
) -> None:
    """Test _get_track_by_isrc returns cached result."""
    provider_mock.mass.cache.get.return_value = "cached_track_456"
    provider_mock.get_track.return_value = mock_track

    result = await streaming_manager._get_track_by_isrc("123")

    assert result == mock_track
    provider_mock.mass.cache.get.assert_called_with(
        "123",
        provider="tidal_instance",
        category=2,  # CACHE_CATEGORY_ISRC_MAP
    )
    provider_mock.get_track.assert_called_with("cached_track_456")


async def test_get_track_by_isrc_cache_miss_lookup_success(
    streaming_manager: TidalStreamingManager, provider_mock: Mock, mock_track: Mock
) -> None:
    """Test _get_track_by_isrc performs ISRC lookup on cache miss."""
    # Cache miss
    provider_mock.mass.cache.get.return_value = None

    # Library item with ISRC
    lib_track = Mock()
    lib_track.external_ids = [(ExternalID.ISRC, "US1234567890")]
    provider_mock.mass.music.tracks.get_library_item_by_prov_id.return_value = lib_track

    # API lookup
    provider_mock.api.get.return_value = {"data": [{"id": 456}]}

    # Final track fetch
    provider_mock.get_track.return_value = mock_track

    result = await streaming_manager._get_track_by_isrc("123")

    assert result == mock_track

    # Verify API call
    provider_mock.api.get.assert_called_with(
        "/tracks",
        params={"filter[isrc]": "US1234567890"},
        base_url=provider_mock.api.OPEN_API_URL,
    )

    # Verify cache set
    provider_mock.mass.cache.set.assert_called_with(
        key="123",
        data="456",
        provider="tidal_instance",
        category=2,  # CACHE_CATEGORY_ISRC_MAP
        persistent=True,
        expiration=86400 * 90,
    )

    # Verify final track fetch
    provider_mock.get_track.assert_called_with("456")


async def test_get_track_by_isrc_no_library_item(
    streaming_manager: TidalStreamingManager, provider_mock: Mock
) -> None:
    """Test _get_track_by_isrc returns None when no library item."""
    provider_mock.mass.cache.get.return_value = None
    provider_mock.mass.music.tracks.get_library_item_by_prov_id.return_value = None

    result = await streaming_manager._get_track_by_isrc("123")

    assert result is None


async def test_get_track_by_isrc_no_isrc_external_id(
    streaming_manager: TidalStreamingManager, provider_mock: Mock
) -> None:
    """Test _get_track_by_isrc returns None when library item has no ISRC."""
    provider_mock.mass.cache.get.return_value = None

    lib_track = Mock()
    lib_track.external_ids = [(ExternalID.BARCODE, "some-id")]
    provider_mock.mass.music.tracks.get_library_item_by_prov_id.return_value = lib_track

    result = await streaming_manager._get_track_by_isrc("123")

    assert result is None


async def test_get_track_by_isrc_api_returns_empty(
    streaming_manager: TidalStreamingManager, provider_mock: Mock
) -> None:
    """Test _get_track_by_isrc returns None when API returns no data."""
    provider_mock.mass.cache.get.return_value = None

    lib_track = Mock()
    lib_track.external_ids = [(ExternalID.ISRC, "US1234567890")]
    provider_mock.mass.music.tracks.get_library_item_by_prov_id.return_value = lib_track

    provider_mock.api.get.return_value = {"data": []}

    result = await streaming_manager._get_track_by_isrc("123")

    assert result is None


async def test_get_track_by_isrc_cached_track_not_found(
    streaming_manager: TidalStreamingManager, provider_mock: Mock
) -> None:
    """Test _get_track_by_isrc deletes cache when cached track not found."""
    provider_mock.mass.cache.get.return_value = "cached_track_999"
    provider_mock.get_track.side_effect = MediaNotFoundError("Track not found")

    # Should continue with ISRC lookup
    lib_track = Mock()
    lib_track.external_ids = [(ExternalID.ISRC, "US1234567890")]
    provider_mock.mass.music.tracks.get_library_item_by_prov_id.return_value = lib_track

    provider_mock.api.get.return_value = {"data": []}

    result = await streaming_manager._get_track_by_isrc("123")

    # Should delete invalid cache entry
    provider_mock.mass.cache.delete.assert_called_with(
        "123",
        provider="tidal_instance",
        category=2,  # CACHE_CATEGORY_ISRC_MAP
    )

    assert result is None


async def test_get_stream_details_with_isrc_fallback(
    streaming_manager: TidalStreamingManager, provider_mock: Mock, mock_track: Mock
) -> None:
    """Test get_stream_details uses ISRC fallback when direct lookup fails."""
    # Direct lookup fails
    provider_mock.get_track.side_effect = [
        MediaNotFoundError("Track not found"),  # First call
        mock_track,  # Second call from ISRC lookup
        mock_track,  # Third call for stream details
    ]

    # ISRC lookup succeeds
    lib_track = Mock()
    lib_track.external_ids = [(ExternalID.ISRC, "US1234567890")]
    provider_mock.mass.music.tracks.get_library_item_by_prov_id.return_value = lib_track

    provider_mock.api.get.return_value = (
        {"data": [{"id": 456}]},  # ISRC lookup response
        None,
    )

    # Stream details
    provider_mock.api.get.side_effect = [
        ({"data": [{"id": 456}]}, None),  # ISRC lookup
        (
            {  # Stream details
                "urls": ["https://example.com/stream.flac"],
                "audioQuality": "LOSSLESS",
                "sampleRate": 44100,
                "bitDepth": 16,
            },
            None,
        ),
    ]

    stream_details = await streaming_manager.get_stream_details("123")

    assert stream_details.item_id == "123"
    assert stream_details.path == "https://example.com/stream.flac"


async def test_get_stream_details_schedules_background_mapping_update(
    streaming_manager: TidalStreamingManager,
    provider_mock: Mock,
    mock_track: Mock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure get_stream_details schedules the background mapping update task."""
    provider_mock.get_track.return_value = mock_track
    provider_mock.api.get.return_value = {
        "urls": ["https://example.com/stream.flac"],
        "audioQuality": "LOSSLESS",
        "sampleRate": 44100,
        "bitDepth": 16,
    }

    created: list[tuple[str, AudioFormat]] = []

    async def _fake_worker(provider_track_id: str, resolved_audio_format: AudioFormat) -> None:
        created.append((provider_track_id, resolved_audio_format))

    # Patch the worker method so we can validate the coroutine is created with expected args
    monkeypatch.setattr(
        streaming_manager, "_async_update_provider_mapping_audio_format", _fake_worker
    )

    captured_coros: list[Coroutine[Any, Any, None]] = []

    def _fake_create_task(coro: Coroutine[Any, Any, None]) -> None:
        # Don't schedule; just capture the coroutine so the test can await it.
        captured_coros.append(coro)

    provider_mock.mass.create_task = _fake_create_task

    stream_details = await streaming_manager.get_stream_details("123")

    assert len(captured_coros) == 1

    # Execute the captured coroutine (safe because we patched the worker)
    await captured_coros[0]

    assert created == [("123", stream_details.audio_format)]


async def test_async_update_provider_mapping_audio_format_no_library_item(
    streaming_manager: TidalStreamingManager, provider_mock: Mock
) -> None:
    """Ensure no update occurs when no library item is found."""
    provider_mock.mass.music.tracks.get_library_item_by_prov_id.return_value = None
    provider_mock.mass.music.tracks.update_provider_mapping = AsyncMock()

    await streaming_manager._async_update_provider_mapping_audio_format(
        provider_track_id="123",
        resolved_audio_format=AudioFormat(
            content_type=ContentType.FLAC, sample_rate=44100, bit_depth=16
        ),
    )

    provider_mock.mass.music.tracks.update_provider_mapping.assert_not_called()


async def test_async_update_provider_mapping_audio_format_no_mapping(
    streaming_manager: TidalStreamingManager, provider_mock: Mock
) -> None:
    """Ensure no update occurs when no provider mapping is found."""
    lib_track = Mock()
    lib_track.item_id = 1
    lib_track.provider_mappings = set()
    provider_mock.mass.music.tracks.get_library_item_by_prov_id.return_value = lib_track
    provider_mock.mass.music.tracks.update_provider_mapping = AsyncMock()

    await streaming_manager._async_update_provider_mapping_audio_format(
        provider_track_id="123",
        resolved_audio_format=AudioFormat(
            content_type=ContentType.FLAC, sample_rate=44100, bit_depth=16
        ),
    )

    provider_mock.mass.music.tracks.update_provider_mapping.assert_not_called()


async def test_async_update_provider_mapping_audio_format_same_format_no_update(
    streaming_manager: TidalStreamingManager, provider_mock: Mock
) -> None:
    """Ensure no update occurs when the audio format is unchanged."""
    fmt = AudioFormat(content_type=ContentType.FLAC, sample_rate=44100, bit_depth=16)
    mapping = Mock()
    mapping.provider_instance = provider_mock.instance_id
    mapping.item_id = "123"
    mapping.audio_format = fmt

    lib_track = Mock()
    lib_track.item_id = 1
    lib_track.provider_mappings = {mapping}
    provider_mock.mass.music.tracks.get_library_item_by_prov_id.return_value = lib_track
    provider_mock.mass.music.tracks.update_provider_mapping = AsyncMock()

    await streaming_manager._async_update_provider_mapping_audio_format(
        provider_track_id="123",
        resolved_audio_format=fmt,
    )

    provider_mock.mass.music.tracks.update_provider_mapping.assert_not_called()


async def test_async_update_provider_mapping_audio_format_different_format_updates(
    streaming_manager: TidalStreamingManager, provider_mock: Mock
) -> None:
    """Ensure update occurs when the audio format is different."""
    old_fmt = AudioFormat(content_type=ContentType.MP4, sample_rate=44100, bit_depth=16)
    new_fmt = AudioFormat(content_type=ContentType.FLAC, sample_rate=44100, bit_depth=16)

    mapping = Mock()
    mapping.provider_instance = provider_mock.instance_id
    mapping.item_id = "123"
    mapping.audio_format = old_fmt

    lib_track = Mock()
    lib_track.item_id = 1
    lib_track.provider_mappings = {mapping}
    provider_mock.mass.music.tracks.get_library_item_by_prov_id.return_value = lib_track
    provider_mock.mass.music.tracks.update_provider_mapping = AsyncMock()

    await streaming_manager._async_update_provider_mapping_audio_format(
        provider_track_id="123",
        resolved_audio_format=new_fmt,
    )

    provider_mock.mass.music.tracks.update_provider_mapping.assert_awaited_once()
    provider_mock.mass.music.tracks.update_provider_mapping.assert_awaited_with(
        item_id=1,
        provider_instance_id=provider_mock.instance_id,
        provider_item_id="123",
        audio_format=new_fmt,
    )


async def test_async_update_provider_mapping_audio_format_sqlite_operational_error_logs_debug(
    streaming_manager: TidalStreamingManager, provider_mock: Mock
) -> None:
    """Ensure OperationalError is logged at debug level."""
    provider_mock.logger = Mock()
    provider_mock.mass.music.tracks.get_library_item_by_prov_id.side_effect = OperationalError(
        "database is locked"
    )

    await streaming_manager._async_update_provider_mapping_audio_format(
        provider_track_id="123",
        resolved_audio_format=AudioFormat(
            content_type=ContentType.FLAC, sample_rate=44100, bit_depth=16
        ),
    )

    provider_mock.logger.debug.assert_called()


async def test_async_update_provider_mapping_audio_format_unexpected_error_logs_exception(
    streaming_manager: TidalStreamingManager, provider_mock: Mock
) -> None:
    """Ensure unexpected errors are logged at exception level."""
    provider_mock.logger = Mock()

    lib_track = Mock()
    lib_track.item_id = 1
    lib_track.provider_mappings = set()
    provider_mock.mass.music.tracks.get_library_item_by_prov_id.return_value = lib_track

    # Force an unexpected error after resolving lib_track
    provider_mock.mass.music.tracks.update_provider_mapping = AsyncMock(
        side_effect=RuntimeError("boom")
    )

    # Create a mapping that triggers the update path
    mapping = Mock()
    mapping.provider_instance = provider_mock.instance_id
    mapping.item_id = "123"
    mapping.audio_format = AudioFormat(
        content_type=ContentType.MP4, sample_rate=44100, bit_depth=16
    )
    lib_track.provider_mappings = {mapping}

    await streaming_manager._async_update_provider_mapping_audio_format(
        provider_track_id="123",
        resolved_audio_format=AudioFormat(
            content_type=ContentType.FLAC, sample_rate=44100, bit_depth=16
        ),
    )

    provider_mock.logger.exception.assert_called()


async def test_get_stream_details_dash_only_creates_mapping_task(
    streaming_manager: TidalStreamingManager, provider_mock: Mock, mock_track: Mock
) -> None:
    """Verify only the mapping update task is created (no route cleanup for CUSTOM streams)."""
    provider_mock.get_track.return_value = mock_track
    provider_mock.api.get.return_value = {
        "manifestMimeType": "application/dash+xml",
        "manifest": _TEST_MANIFEST_B64,
        "audioQuality": "HIGH",
        "sampleRate": 44100,
        "bitDepth": 16,
    }

    captured: list[Any] = []
    provider_mock.mass.create_task = Mock(side_effect=captured.append)

    await streaming_manager.get_stream_details("123")

    # Only the mapping update task should be created.
    assert provider_mock.mass.create_task.call_count == 1


async def test_get_stream_details_dash_repeated_calls_independent(
    streaming_manager: TidalStreamingManager, provider_mock: Mock, mock_track: Mock
) -> None:
    """Repeated calls for the same DASH track produce independent stream details."""
    provider_mock.get_track.return_value = mock_track
    provider_mock.mass.cache.get.return_value = {
        "manifestMimeType": "application/dash+xml",
        "manifest": _TEST_MANIFEST_B64,
        "audioQuality": "HIGH",
        "sampleRate": 44100,
        "bitDepth": 16,
    }

    sd1 = await streaming_manager.get_stream_details("123")
    sd2 = await streaming_manager.get_stream_details("123")

    # Both should be CUSTOM streams with the same segment data.
    assert sd1.stream_type == StreamType.CUSTOM
    assert sd2.stream_type == StreamType.CUSTOM
    assert sd1.data["init_url"] == sd2.data["init_url"]
    assert len(sd1.data["segments"]) == len(sd2.data["segments"])


async def test_parse_dash_segments_extracts_urls_with_timing(
    streaming_manager: TidalStreamingManager,
) -> None:
    """Verify _parse_dash_segments correctly extracts segment URLs and start times."""
    result = streaming_manager._parse_dash_segments(_TEST_MANIFEST_XML.encode(), "123")

    assert result["init_url"] == "https://cdn.tidal.com/init.mp4"
    segments = result["segments"]
    assert len(segments) == 3  # r="2" means 3 repetitions
    assert segments[0]["url"] == "https://cdn.tidal.com/1.mp4"
    assert segments[0]["start"] == 0.0
    assert segments[1]["url"] == "https://cdn.tidal.com/2.mp4"
    # 176128 / 44100 ≈ 3.993 seconds
    assert abs(segments[1]["start"] - 176128 / 44100) < 0.001
    assert segments[2]["url"] == "https://cdn.tidal.com/3.mp4"


async def test_get_audio_stream_yields_init_then_segments(
    streaming_manager: TidalStreamingManager, provider_mock: Mock
) -> None:
    """Verify get_audio_stream yields init segment followed by media segments."""
    mock_streamdetails = Mock()
    mock_streamdetails.item_id = "123"
    mock_streamdetails.data = {
        "init_url": "https://cdn.tidal.com/init.mp4",
        "segments": [
            {"url": "https://cdn.tidal.com/1.mp4", "start": 0.0},
            {"url": "https://cdn.tidal.com/2.mp4", "start": 4.0},
        ],
    }

    # Mock the HTTP session to return predictable bytes per URL.
    responses: dict[str, bytes] = {
        "https://cdn.tidal.com/init.mp4": b"INIT",
        "https://cdn.tidal.com/1.mp4": b"SEG1",
        "https://cdn.tidal.com/2.mp4": b"SEG2",
    }

    async def _mock_content(data: bytes) -> AsyncGenerator[bytes, None]:
        yield data

    def _mock_get(url: str, **_kwargs: object) -> MagicMock:
        ctx = MagicMock()
        resp = MagicMock()
        resp.status = 200
        resp.content.iter_any = lambda: _mock_content(responses[url])
        ctx.__aenter__ = AsyncMock(return_value=resp)
        ctx.__aexit__ = AsyncMock(return_value=None)
        return ctx

    mock_session = MagicMock()
    mock_session.get = _mock_get
    provider_mock.mass.http_session = mock_session

    chunks = []
    async for chunk in streaming_manager.get_audio_stream(mock_streamdetails):
        chunks.append(chunk)

    assert chunks == [b"INIT", b"SEG1", b"SEG2"]


async def test_get_audio_stream_seeks_to_correct_segment(
    streaming_manager: TidalStreamingManager, provider_mock: Mock
) -> None:
    """Verify get_audio_stream skips segments before the seek position."""
    mock_streamdetails = Mock()
    mock_streamdetails.item_id = "123"
    mock_streamdetails.data = {
        "init_url": "https://cdn.tidal.com/init.mp4",
        "segments": [
            {"url": "https://cdn.tidal.com/1.mp4", "start": 0.0},
            {"url": "https://cdn.tidal.com/2.mp4", "start": 4.0},
            {"url": "https://cdn.tidal.com/3.mp4", "start": 8.0},
        ],
    }

    fetched_urls: list[str] = []

    async def _mock_content(data: bytes) -> AsyncGenerator[bytes, None]:
        yield data

    def _mock_get(url: str, **_kwargs: object) -> MagicMock:
        fetched_urls.append(url)
        ctx = MagicMock()
        resp = MagicMock()
        resp.status = 200
        resp.content.iter_any = lambda: _mock_content(b"X")
        ctx.__aenter__ = AsyncMock(return_value=resp)
        ctx.__aexit__ = AsyncMock(return_value=None)
        return ctx

    mock_session = MagicMock()
    mock_session.get = _mock_get
    provider_mock.mass.http_session = mock_session

    chunks = []
    async for chunk in streaming_manager.get_audio_stream(mock_streamdetails, seek_position=5):
        chunks.append(chunk)

    # Should fetch init + segments starting from segment 2 (start=4.0, which is <= 5)
    assert fetched_urls == [
        "https://cdn.tidal.com/init.mp4",
        "https://cdn.tidal.com/2.mp4",
        "https://cdn.tidal.com/3.mp4",
    ]


async def test_get_audio_stream_raises_on_segment_error(
    streaming_manager: TidalStreamingManager, provider_mock: Mock
) -> None:
    """Verify get_audio_stream raises MediaNotFoundError on HTTP errors."""
    mock_streamdetails = Mock()
    mock_streamdetails.item_id = "123"
    mock_streamdetails.data = {
        "init_url": "https://cdn.tidal.com/init.mp4",
        "segments": [{"url": "https://cdn.tidal.com/1.mp4", "start": 0.0}],
    }

    async def _mock_content(data: bytes) -> AsyncGenerator[bytes, None]:
        yield data

    call_count = 0

    def _mock_get(_url: str, **_kwargs: object) -> MagicMock:
        nonlocal call_count
        call_count += 1
        ctx = MagicMock()
        resp = MagicMock()
        if call_count == 1:
            # Init segment succeeds
            resp.status = 200
            resp.content.iter_any = lambda: _mock_content(b"INIT")
        else:
            # Media segment fails
            resp.status = 403
        ctx.__aenter__ = AsyncMock(return_value=resp)
        ctx.__aexit__ = AsyncMock(return_value=None)
        return ctx

    mock_session = MagicMock()
    mock_session.get = _mock_get
    provider_mock.mass.http_session = mock_session

    with pytest.raises(MediaNotFoundError, match="HTTP 403"):
        async for _ in streaming_manager.get_audio_stream(mock_streamdetails):
            pass


async def test_get_stream_details_playback_info_cache_hit(
    streaming_manager: TidalStreamingManager, provider_mock: Mock, mock_track: Mock
) -> None:
    """Verify no API call is made when playback info is already cached."""
    provider_mock.get_track.return_value = mock_track
    provider_mock.mass.cache.get.return_value = {
        "manifestMimeType": "application/vnd.tidal.bts",
        "urls": ["https://cdn.tidal.com/stream.flac"],
        "audioQuality": "LOSSLESS",
        "sampleRate": 44100,
        "bitDepth": 16,
    }

    stream_details = await streaming_manager.get_stream_details("123")

    assert stream_details.path == "https://cdn.tidal.com/stream.flac"
    provider_mock.api.get.assert_not_called()
    provider_mock.mass.cache.set.assert_not_called()


async def test_get_stream_details_playback_info_cache_miss_sets_cache(
    streaming_manager: TidalStreamingManager, provider_mock: Mock, mock_track: Mock
) -> None:
    """Verify the API response is stored in cache on a cache miss."""
    provider_mock.get_track.return_value = mock_track
    provider_mock.mass.cache.get.return_value = None
    api_response = {
        "manifestMimeType": "application/vnd.tidal.bts",
        "urls": ["https://cdn.tidal.com/stream.flac"],
        "audioQuality": "LOSSLESS",
        "sampleRate": 44100,
        "bitDepth": 16,
    }
    provider_mock.api.get.return_value = api_response

    await streaming_manager.get_stream_details("123")

    provider_mock.mass.cache.set.assert_called_once()
    set_call = provider_mock.mass.cache.set.call_args
    assert set_call[0][1] == api_response  # second positional arg is the data
