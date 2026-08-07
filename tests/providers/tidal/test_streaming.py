"""Test Tidal Streaming Manager."""

from collections.abc import Coroutine
from sqlite3 import OperationalError
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from music_assistant_models.enums import ContentType, ExternalID, StreamType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import AudioFormat, Track

from music_assistant.providers.tidal.streaming import TidalStreamingManager


@pytest.fixture
def provider_mock(provider_mock: Mock) -> Mock:
    """Return the shared provider mock with the streaming quality and throttler bypass wired."""
    provider_mock.config.get_value.return_value = "HIGH"
    provider_mock.api.OPEN_API_URL = "https://openapi.tidal.com/v2"

    # the streaming manager enters api.throttler.bypass() as an async context manager,
    # which a MagicMock supports out of the box
    provider_mock.api.throttler.bypass = Mock(return_value=MagicMock())

    return provider_mock


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
    provider_mock.api.get.return_value = {
        "manifestMimeType": "application/vnd.tidal.bts",
        "urls": ["https://example.com/stream.flac"],
        "audioQuality": "LOSSLESS",
        "sampleRate": 44100,
        "bitDepth": 16,
    }

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
    """Test get_stream_details with DASH manifest served via HTTP route."""
    provider_mock.get_track.return_value = mock_track
    provider_mock.api.get.return_value = {
        "manifestMimeType": "application/dash+xml",
        "manifest": "bWFuaWZlc3REYXRh",
        "audioQuality": "HIGH",
        "sampleRate": 44100,
        "bitDepth": 16,
    }
    # Mock the stream server's dynamic route registration
    provider_mock.mass.streams.register_dynamic_route = Mock(return_value=lambda: None)
    provider_mock.mass.streams.base_url = "http://localhost:8097"

    stream_details = await streaming_manager.get_stream_details("123")

    assert isinstance(stream_details.path, str)
    assert stream_details.path.startswith("http://localhost:8097/tidal-dash/")
    assert "base64" not in stream_details.path
    # Verify the route was registered
    provider_mock.mass.streams.register_dynamic_route.assert_called_once()
    # Verify cleanup was scheduled with duration-based TTL (180s track + 300s buffer = 480s)
    provider_mock.mass.call_later.assert_called_with(
        480,
        streaming_manager._remove_dash_route,
        "/tidal-dash/a3aca34e43c1737c7738507842972fa9",
        task_id="tidal-dash-cleanup-a3aca34e43c1737c7738507842972fa9",
    )


async def test_get_stream_details_with_dash_manifest_handler(
    streaming_manager: TidalStreamingManager, provider_mock: Mock, mock_track: Mock
) -> None:
    """Test the DASH manifest handler returns correct body and content type."""
    provider_mock.get_track.return_value = mock_track
    provider_mock.api.get.return_value = {
        "manifestMimeType": "application/dash+xml",
        "manifest": "bWFuaWZlc3REYXRh",
        "audioQuality": "HIGH",
        "sampleRate": 44100,
        "bitDepth": 16,
    }
    # Capture the registered handler
    register_mock = Mock()
    provider_mock.mass.streams.register_dynamic_route = register_mock
    provider_mock.mass.streams.base_url = "http://localhost:8097"

    await streaming_manager.get_stream_details("123")

    # Extract the handler that was registered as second argument
    handler = register_mock.call_args[0][1]
    response = await handler(Mock())

    assert response.body == b"manifestData"  # base64 decoded
    assert response.content_type == "application/dash+xml"
    # Verify Cache-Control header is set to prevent proxy caching
    assert response.headers.get("Cache-Control") == "no-cache"


async def test_get_stream_details_with_dash_manifest_duplicate_registration(
    streaming_manager: TidalStreamingManager, provider_mock: Mock, mock_track: Mock
) -> None:
    """Test cleanup is still scheduled when reusing an already-registered route."""
    provider_mock.get_track.return_value = mock_track
    provider_mock.api.get.return_value = {
        "manifestMimeType": "application/dash+xml",
        "manifest": "bWFuaWZlc3REYXRh",
        "audioQuality": "HIGH",
        "sampleRate": 44100,
        "bitDepth": 16,
    }
    # First call succeeds, second raises RuntimeError (duplicate route)
    provider_mock.mass.streams.register_dynamic_route = Mock(
        side_effect=[None, RuntimeError("duplicate")]
    )
    provider_mock.mass.streams.base_url = "http://localhost:8097"
    # call_later must count calls
    call_later_mock = Mock()
    provider_mock.mass.call_later = call_later_mock

    # Call get_stream_details twice (same track → same manifest hash)
    await streaming_manager.get_stream_details("123")
    await streaming_manager.get_stream_details("123")

    # route registration was attempted twice
    assert provider_mock.mass.streams.register_dynamic_route.call_count == 2
    # cleanup was scheduled both times (not skipped on duplicate)
    assert call_later_mock.call_count == 2


async def test_remove_dash_route(
    streaming_manager: TidalStreamingManager, provider_mock: Mock
) -> None:
    """Test _remove_dash_route calls unregister on the stream server."""
    unregister_mock = Mock()
    provider_mock.mass.streams.unregister_dynamic_route = unregister_mock

    streaming_manager._remove_dash_route("/tidal-dash/abc123")

    unregister_mock.assert_called_once_with("/tidal-dash/abc123", method="GET")


async def test_remove_dash_route_handles_runtime_error(
    streaming_manager: TidalStreamingManager, provider_mock: Mock
) -> None:
    """Test _remove_dash_route silently swallows RuntimeError."""
    provider_mock.mass.streams.unregister_dynamic_route = Mock(
        side_effect=RuntimeError("not found")
    )

    # Should not raise
    streaming_manager._remove_dash_route("/tidal-dash/abc123")


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
        "tracks",
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

    provider_mock.api.get.side_effect = [
        {"data": [{"id": 456}]},  # ISRC lookup
        {  # Stream details
            "urls": ["https://example.com/stream.flac"],
            "audioQuality": "LOSSLESS",
            "sampleRate": 44100,
            "bitDepth": 16,
        },
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
