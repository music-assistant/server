"""Unit tests for KION Music streaming quality selection."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from music_assistant_models.enums import ContentType

from music_assistant.providers.kion_music.constants import QUALITY_HIGH, QUALITY_LOSSLESS
from music_assistant.providers.kion_music.streaming import KionMusicStreamingManager

if TYPE_CHECKING:
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
    return KionMusicStreamingManager(streaming_provider_stub)


@pytest.fixture
def streaming_manager_with_tracking(
    streaming_provider_stub_with_tracking: StreamingProviderStubWithTracking,
) -> KionMusicStreamingManager:
    """Create streaming manager with tracking logger for assertions."""
    return KionMusicStreamingManager(streaming_provider_stub_with_tracking)


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


def test_get_content_type_flac_mp4_returns_flac(
    streaming_manager: KionMusicStreamingManager,
) -> None:
    """flac-mp4 codec from get-file-info is mapped to MP4 container and FLAC codec."""
    content_type = streaming_manager._get_content_type("flac-mp4")
    assert content_type[0] == ContentType.MP4
    assert content_type[1] == ContentType.FLAC
    content_type_upper = streaming_manager._get_content_type("FLAC-MP4")
    assert content_type_upper[0] == ContentType.MP4
    assert content_type_upper[1] == ContentType.FLAC
