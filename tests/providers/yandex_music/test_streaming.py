"""Unit tests for Yandex Music streaming quality selection."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from music_assistant_models.enums import ContentType

from music_assistant.providers.yandex_music.constants import (
    QUALITY_BALANCED,
    QUALITY_EFFICIENT,
    QUALITY_HIGH,
    QUALITY_SUPERB,
)
from music_assistant.providers.yandex_music.streaming import YandexMusicStreamingManager

if TYPE_CHECKING:
    from tests.providers.yandex_music.conftest import (
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
) -> YandexMusicStreamingManager:
    """Create streaming manager with real stub (no Mock)."""
    return YandexMusicStreamingManager(streaming_provider_stub)  # type: ignore[arg-type]


@pytest.fixture
def streaming_manager_with_tracking(
    streaming_provider_stub_with_tracking: StreamingProviderStubWithTracking,
) -> YandexMusicStreamingManager:
    """Create streaming manager with tracking logger for assertions."""
    return YandexMusicStreamingManager(streaming_provider_stub_with_tracking)  # type: ignore[arg-type]


def test_select_best_quality_lossless_returns_flac(
    streaming_manager: YandexMusicStreamingManager,
) -> None:
    """When preferred_quality is 'lossless' and list has MP3 and FLAC, FLAC is selected."""
    mp3 = _make_download_info("mp3", 320, "https://example.com/track.mp3")
    flac = _make_download_info("flac", 0, "https://example.com/track.flac")
    download_infos = [mp3, flac]

    result = streaming_manager._select_best_quality(download_infos, QUALITY_SUPERB)

    assert result is not None
    assert result.codec == "flac"
    assert result.direct_link == "https://example.com/track.flac"


def test_select_best_quality_balanced_returns_medium_bitrate(
    streaming_manager: YandexMusicStreamingManager,
) -> None:
    """When preferred is 'balanced' and no option in range, fallback to highest bitrate."""
    mp3 = _make_download_info("mp3", 320, "https://example.com/track.mp3")
    flac = _make_download_info("flac", 0, "https://example.com/track.flac")
    download_infos = [mp3, flac]

    result = streaming_manager._select_best_quality(download_infos, QUALITY_BALANCED)

    assert result is not None
    assert result.codec == "mp3"
    assert result.bitrate_in_kbps == 320


def test_select_best_quality_label_lossless_flac_returns_flac(
    streaming_manager: YandexMusicStreamingManager,
) -> None:
    """When preferred_quality is UI label 'Lossless (FLAC)', FLAC is selected."""
    mp3 = _make_download_info("mp3", 320, "https://example.com/track.mp3")
    flac = _make_download_info("flac", 0, "https://example.com/track.flac")
    download_infos = [mp3, flac]

    result = streaming_manager._select_best_quality(download_infos, "Lossless (FLAC)")

    assert result is not None
    assert result.codec == "flac"


def test_select_best_quality_lossless_no_flac_returns_fallback(
    streaming_manager_with_tracking: YandexMusicStreamingManager,
) -> None:
    """When lossless requested but no FLAC in list, returns best available (fallback)."""
    mp3 = _make_download_info("mp3", 320, "https://example.com/track.mp3")
    download_infos = [mp3]

    result = streaming_manager_with_tracking._select_best_quality(download_infos, QUALITY_SUPERB)

    assert result is not None
    assert result.codec == "mp3"
    assert streaming_manager_with_tracking.provider.logger._warning_count == 1  # type: ignore[attr-defined]


def test_select_best_quality_empty_list_returns_none(
    streaming_manager: YandexMusicStreamingManager,
) -> None:
    """Empty download_infos returns None."""
    result = streaming_manager._select_best_quality([], QUALITY_SUPERB)
    assert result is None


def test_select_best_quality_none_preferred_returns_highest_bitrate(
    streaming_manager: YandexMusicStreamingManager,
) -> None:
    """When preferred_quality is None, returns highest bitrate."""
    mp3 = _make_download_info("mp3", 320, "https://example.com/track.mp3")
    flac = _make_download_info("flac", 0, "https://example.com/track.flac")
    download_infos = [mp3, flac]

    result = streaming_manager._select_best_quality(download_infos, None)

    assert result is not None
    assert result.codec == "mp3"
    assert result.bitrate_in_kbps == 320


def test_get_content_type_flac_mp4_returns_flac(
    streaming_manager: YandexMusicStreamingManager,
) -> None:
    """flac-mp4 codec from get-file-info is mapped to ContentType.FLAC."""
    assert streaming_manager._get_content_type("flac-mp4") == ContentType.FLAC
    assert streaming_manager._get_content_type("FLAC-MP4") == ContentType.FLAC


def test_get_content_type_aac_variants_return_aac(
    streaming_manager: YandexMusicStreamingManager,
) -> None:
    """All AAC codec variants are mapped to ContentType.AAC."""
    # Test all AAC variants defined in GET_FILE_INFO_CODECS
    assert streaming_manager._get_content_type("aac") == ContentType.AAC
    assert streaming_manager._get_content_type("AAC") == ContentType.AAC
    assert streaming_manager._get_content_type("aac-mp4") == ContentType.AAC
    assert streaming_manager._get_content_type("AAC-MP4") == ContentType.AAC
    assert streaming_manager._get_content_type("he-aac") == ContentType.AAC
    assert streaming_manager._get_content_type("HE-AAC") == ContentType.AAC
    assert streaming_manager._get_content_type("he-aac-mp4") == ContentType.AAC
    assert streaming_manager._get_content_type("HE-AAC-MP4") == ContentType.AAC


# --- Efficient quality tests ---


def test_select_best_quality_efficient_prefers_lowest_aac(
    streaming_manager: YandexMusicStreamingManager,
) -> None:
    """Efficient quality prefers lowest bitrate AAC over higher bitrate options."""
    mp3_320 = _make_download_info("mp3", 320)
    aac_64 = _make_download_info("aac", 64)
    aac_192 = _make_download_info("aac", 192)

    result = streaming_manager._select_best_quality([mp3_320, aac_64, aac_192], QUALITY_EFFICIENT)

    assert result is not None
    assert result.codec == "aac"
    assert result.bitrate_in_kbps == 64


def test_select_best_quality_efficient_aac_mp4_variant(
    streaming_manager: YandexMusicStreamingManager,
) -> None:
    """Efficient quality recognizes aac-mp4 container variant."""
    mp3_320 = _make_download_info("mp3", 320)
    aac_mp4_64 = _make_download_info("aac-mp4", 64)

    result = streaming_manager._select_best_quality([mp3_320, aac_mp4_64], QUALITY_EFFICIENT)

    assert result is not None
    assert result.codec == "aac-mp4"
    assert result.bitrate_in_kbps == 64


def test_select_best_quality_efficient_fallback_to_mp3(
    streaming_manager: YandexMusicStreamingManager,
) -> None:
    """Efficient quality falls back to MP3 when no AAC available."""
    mp3_128 = _make_download_info("mp3", 128)
    flac = _make_download_info("flac", 0)

    result = streaming_manager._select_best_quality([mp3_128, flac], QUALITY_EFFICIENT)

    assert result is not None
    assert result.codec == "mp3"


def test_select_best_quality_efficient_fallback_to_lowest(
    streaming_manager: YandexMusicStreamingManager,
) -> None:
    """Efficient quality falls back to lowest bitrate when no AAC/MP3."""
    flac = _make_download_info("flac", 1411)

    result = streaming_manager._select_best_quality([flac], QUALITY_EFFICIENT)

    assert result is not None
    assert result.codec == "flac"


# --- High quality tests ---


def test_select_best_quality_high_prefers_mp3_320(
    streaming_manager: YandexMusicStreamingManager,
) -> None:
    """High quality prefers MP3 with bitrate >= 256kbps."""
    mp3_320 = _make_download_info("mp3", 320)
    mp3_128 = _make_download_info("mp3", 128)
    aac_192 = _make_download_info("aac", 192)
    flac = _make_download_info("flac", 1411)

    result = streaming_manager._select_best_quality([mp3_320, mp3_128, aac_192, flac], QUALITY_HIGH)

    assert result is not None
    assert result.codec == "mp3"
    assert result.bitrate_in_kbps == 320


def test_select_best_quality_high_fallback_to_any_mp3(
    streaming_manager: YandexMusicStreamingManager,
) -> None:
    """High quality falls back to any MP3 when no high-bitrate MP3 available."""
    mp3_128 = _make_download_info("mp3", 128)
    aac_192 = _make_download_info("aac", 192)

    result = streaming_manager._select_best_quality([mp3_128, aac_192], QUALITY_HIGH)

    assert result is not None
    assert result.codec == "mp3"
    assert result.bitrate_in_kbps == 128


def test_select_best_quality_high_no_mp3_uses_non_flac(
    streaming_manager: YandexMusicStreamingManager,
) -> None:
    """High quality uses highest non-FLAC when no MP3 available."""
    aac_192 = _make_download_info("aac", 192)
    flac = _make_download_info("flac", 1411)

    result = streaming_manager._select_best_quality([aac_192, flac], QUALITY_HIGH)

    assert result is not None
    assert result.codec == "aac"
    assert result.bitrate_in_kbps == 192


def test_select_best_quality_high_only_flac_returns_flac(
    streaming_manager: YandexMusicStreamingManager,
) -> None:
    """High quality returns FLAC as last resort when nothing else available."""
    flac = _make_download_info("flac", 1411)

    result = streaming_manager._select_best_quality([flac], QUALITY_HIGH)

    assert result is not None
    assert result.codec == "flac"
