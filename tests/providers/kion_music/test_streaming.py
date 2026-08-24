"""Unit tests for KION Music streaming quality selection."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

import pytest
from aiohttp import ClientPayloadError
from music_assistant_models.enums import ContentType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.providers.kion_music.constants import QUALITY_HIGH, QUALITY_LOSSLESS
from music_assistant.providers.kion_music.streaming import KionMusicStreamingManager

if TYPE_CHECKING:
    from music_assistant.providers.kion_music.provider import KionMusicProvider
    from tests.providers.kion_music.conftest import (
        StreamingProviderStub,
        StreamingProviderStubWithTracking,
    )


def _make_download_info(
    codec: str,
    bitrate_in_kbps: int,
    direct_link: str = "https://example.com/track",
) -> Any:
    """Build DownloadInfo-like object."""
    return type(
        "DownloadInfo",
        (),
        {
            "codec": codec,
            "bitrate_in_kbps": bitrate_in_kbps,
            "direct_link": direct_link,
        },
    )()


@pytest.fixture
def streaming_manager(
    streaming_provider_stub: StreamingProviderStub,
) -> KionMusicStreamingManager:
    """Create streaming manager with real stub (no Mock)."""
    return KionMusicStreamingManager(cast("KionMusicProvider", streaming_provider_stub))


@pytest.fixture
def streaming_manager_with_tracking(
    streaming_provider_stub_with_tracking: StreamingProviderStubWithTracking,
) -> KionMusicStreamingManager:
    """Create streaming manager with tracking logger for assertions."""
    return KionMusicStreamingManager(
        cast("KionMusicProvider", streaming_provider_stub_with_tracking)
    )


def test_select_best_quality_lossless_returns_flac(
    streaming_manager: KionMusicStreamingManager,
) -> None:
    """When preferred_quality is 'lossless' and list has MP3 and FLAC, FLAC is selected."""
    mp3 = _make_download_info("mp3", 320, "https://example.com/track.mp3")
    flac = _make_download_info("flac", 0, "https://example.com/track.flac")
    download_infos = [mp3, flac]

    result = streaming_manager._select_best_quality(download_infos, QUALITY_LOSSLESS)

    assert result is not None
    assert result.codec == "flac"
    assert result.direct_link == "https://example.com/track.flac"


def test_select_best_quality_high_returns_highest_bitrate(
    streaming_manager: KionMusicStreamingManager,
) -> None:
    """When preferred is 'high' and list has MP3 and FLAC, highest bitrate is selected."""
    mp3 = _make_download_info("mp3", 320, "https://example.com/track.mp3")
    flac = _make_download_info("flac", 0, "https://example.com/track.flac")
    download_infos = [mp3, flac]

    result = streaming_manager._select_best_quality(download_infos, QUALITY_HIGH)

    assert result is not None
    assert result.codec == "mp3"
    assert result.bitrate_in_kbps == 320


def test_select_best_quality_empty_list_returns_none(
    streaming_manager: KionMusicStreamingManager,
) -> None:
    """Empty download_infos returns None."""
    result = streaming_manager._select_best_quality([], QUALITY_LOSSLESS)
    assert result is None


def test_select_best_quality_none_preferred_returns_highest_bitrate(
    streaming_manager: KionMusicStreamingManager,
) -> None:
    """When preferred_quality is None, returns highest bitrate."""
    mp3 = _make_download_info("mp3", 320, "https://example.com/track.mp3")
    flac = _make_download_info("flac", 0, "https://example.com/track.flac")
    download_infos = [mp3, flac]

    result = streaming_manager._select_best_quality(download_infos, None)

    assert result is not None
    assert result.codec == "mp3"
    assert result.bitrate_in_kbps == 320


def _flac_streaminfo_payload(sample_rate: int, bit_depth: int) -> bytes:
    """
    Build a 34-byte FLAC STREAMINFO payload for the given sample_rate/bit_depth.

    Layout of bytes 10..14 (4 bytes, big-endian, 32 bits total):
        sample_rate (20) | channels (3) | bps_minus_1 (5) | total_samples_hi (4)
    """
    val = (sample_rate & 0xFFFFF) << 12 | (1 & 0x7) << 9 | ((bit_depth - 1) & 0x1F) << 4
    return b"\x00" * 10 + val.to_bytes(4, "big") + b"\x00" * 20


def _flac_header(sample_rate: int = 44100, bit_depth: int = 16) -> bytes:
    """Build a valid FLAC file header: magic + block header + STREAMINFO payload."""
    magic = b"fLaC"
    block_header = b"\x00\x00\x00\x22"  # type=0 (STREAMINFO), length=34
    return magic + block_header + _flac_streaminfo_payload(sample_rate, bit_depth)


def _mp4_dfla_header(sample_rate: int = 44100, bit_depth: int = 16) -> bytes:
    """Build an MP4 buffer containing a dfLa box with embedded STREAMINFO."""
    prefix = b"\x00" * 8  # any 4+ byte prefix so dfl_pos >= 4
    version_flags = b"\x00" * 4
    block_header = b"\x00\x00\x00\x22"
    payload = _flac_streaminfo_payload(sample_rate, bit_depth)
    return prefix + b"dfLa" + version_flags + block_header + payload


def _mp4_mp4a_header(sample_rate: int = 48000, sample_size: int = 16) -> bytes:
    """Build an MP4 buffer containing an mp4a AudioSampleEntry."""
    prefix = b"\x00" * 8
    sr_fixed = (sample_rate << 16) & 0xFFFFFFFF
    # 0..18: reserved(6) + data_ref(2) + version(2) + revision(2) + vendor(4) + channels(2)
    # 18..20: sample_size; 20..24: compression_id + packet_size; 24..28: sample_rate (16.16)
    entry = (
        b"\x00" * 18 + sample_size.to_bytes(2, "big") + b"\x00" * 4 + sr_fixed.to_bytes(4, "big")
    )
    return prefix + b"mp4a" + entry


def test_parse_flac_streaminfo_valid() -> None:
    """Valid FLAC STREAMINFO returns parsed sample_rate and bit_depth."""
    result = KionMusicStreamingManager._parse_flac_streaminfo(_flac_header(44100, 16))
    assert result == (44100, 16)
    result_hires = KionMusicStreamingManager._parse_flac_streaminfo(_flac_header(96000, 24))
    assert result_hires == (96000, 24)


def test_parse_flac_streaminfo_wrong_magic() -> None:
    """Header without 'fLaC' magic returns (0, 0)."""
    bad = b"OggS" + _flac_header()[4:]
    assert KionMusicStreamingManager._parse_flac_streaminfo(bad) == (0, 0)


def test_parse_flac_streaminfo_too_short() -> None:
    """Header shorter than 42 bytes returns (0, 0)."""
    assert KionMusicStreamingManager._parse_flac_streaminfo(b"fLaC\x00\x00") == (0, 0)


def test_parse_mp4_audio_params_dfla() -> None:
    """DfLa (FLAC-in-MP4) box is parsed via embedded STREAMINFO."""
    result = KionMusicStreamingManager._parse_mp4_audio_params(_mp4_dfla_header(44100, 16))
    assert result == (44100, 16)


def test_parse_mp4_audio_params_mp4a_fallback() -> None:
    """When dfLa is absent, mp4a AudioSampleEntry provides sample_rate/bit_depth."""
    result = KionMusicStreamingManager._parse_mp4_audio_params(_mp4_mp4a_header(48000, 16))
    assert result == (48000, 16)


def test_parse_mp4_audio_params_no_box_returns_zero() -> None:
    """Buffer containing neither dfLa nor mp4a returns (0, 0)."""
    assert KionMusicStreamingManager._parse_mp4_audio_params(b"\x00" * 128) == (0, 0)


def test_get_content_type_flac_mp4_returns_mp4_flac(
    streaming_manager: KionMusicStreamingManager,
) -> None:
    """flac-mp4 → (MP4 container, FLAC codec), matching yandex_music convention."""
    assert streaming_manager._get_content_type("flac-mp4") == (
        ContentType.MP4,
        ContentType.FLAC,
    )
    assert streaming_manager._get_content_type("FLAC-MP4") == (
        ContentType.MP4,
        ContentType.FLAC,
    )


def test_get_content_type_aac_mp4_returns_mp4_aac(
    streaming_manager: KionMusicStreamingManager,
) -> None:
    """aac-mp4 / he-aac-mp4 → (MP4 container, AAC codec)."""
    assert streaming_manager._get_content_type("aac-mp4") == (
        ContentType.MP4,
        ContentType.AAC,
    )
    assert streaming_manager._get_content_type("he-aac-mp4") == (
        ContentType.MP4,
        ContentType.AAC,
    )


def test_get_content_type_plain_codecs(
    streaming_manager: KionMusicStreamingManager,
) -> None:
    """Plain codecs report themselves as content_type with UNKNOWN codec_type."""
    assert streaming_manager._get_content_type("flac") == (ContentType.FLAC, ContentType.UNKNOWN)
    assert streaming_manager._get_content_type("mp3") == (ContentType.MP3, ContentType.UNKNOWN)
    assert streaming_manager._get_content_type("mpeg") == (ContentType.MP3, ContentType.UNKNOWN)
    assert streaming_manager._get_content_type("aac") == (ContentType.AAC, ContentType.UNKNOWN)
    assert streaming_manager._get_content_type("he-aac") == (ContentType.AAC, ContentType.UNKNOWN)


def _make_stream_details(
    decryption_key: str, url: str = "https://example.com/enc.flac"
) -> StreamDetails:
    """Build a minimal StreamDetails for get_audio_stream tests."""
    return StreamDetails(
        provider="kion_music_instance",
        item_id="test_track",
        audio_format=AudioFormat(content_type=ContentType.FLAC),
        data={
            "url": url,
            "codec": "flac",
            "transport": "encraw",
            "bit_rate": 0,
            "fi_quality": "lossless",
            "fi_codecs": "flac-mp4,flac",
            "decryption_key": decryption_key,
        },
    )


async def test_get_audio_stream_retries_on_payload_error_then_raises(
    streaming_manager: KionMusicStreamingManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ClientPayloadError causes retries; raises MediaNotFoundError after max retries."""
    get_audio_stream = getattr(streaming_manager, "get_audio_stream", None)
    if get_audio_stream is None:
        pytest.skip("get_audio_stream not available in this provider version")

    async def _no_sleep(_: float) -> None:
        pass

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    class _DroppingContent:
        async def iter_chunked(self, n: int) -> Any:
            raise ClientPayloadError("Connection dropped")
            yield b""  # type: ignore[unreachable]  # makes this an async generator

    class _DroppingResponse:
        status = 200
        content = _DroppingContent()

        def raise_for_status(self) -> None:
            pass

    class _DroppingContext:
        async def __aenter__(self) -> _DroppingResponse:
            return _DroppingResponse()

        async def __aexit__(self, *args: object) -> None:
            pass

    class _FakeHttpSession:
        def get(self, url: str, headers: Any = None, **kwargs: Any) -> _DroppingContext:
            return _DroppingContext()

    streaming_manager_mass: Any = streaming_manager.mass
    streaming_manager_mass.http_session = _FakeHttpSession()
    streamdetails = _make_stream_details(decryption_key="00" * 16)  # valid 16-byte AES key

    with pytest.raises(MediaNotFoundError, match="retries were exhausted"):
        async for _ in get_audio_stream(streamdetails):
            pass
