"""Unit tests for Yandex Music streaming quality selection."""

from __future__ import annotations

import unittest.mock
from typing import TYPE_CHECKING, Any

import pytest
from aiohttp import ClientPayloadError
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from music_assistant_models.enums import ContentType, StreamType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

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


def test_select_best_quality_balanced_falls_back_to_highest(
    streaming_manager: YandexMusicStreamingManager,
) -> None:
    """When preferred is 'balanced' and no option in 128-256kbps range, highest bitrate is used."""
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


def test_get_content_type_flac_mp4_returns_mp4_container_with_flac_codec(
    streaming_manager: YandexMusicStreamingManager,
) -> None:
    """flac-mp4 codec from get-file-info is mapped to MP4 container with FLAC codec."""
    assert streaming_manager._get_content_type("flac-mp4") == (ContentType.MP4, ContentType.FLAC)
    assert streaming_manager._get_content_type("FLAC-MP4") == (ContentType.MP4, ContentType.FLAC)


def test_get_content_type_flac_returns_flac_container_with_unknown_codec(
    streaming_manager: YandexMusicStreamingManager,
) -> None:
    """Plain FLAC codec is mapped to FLAC container with UNKNOWN codec."""
    assert streaming_manager._get_content_type("flac") == (ContentType.FLAC, ContentType.UNKNOWN)
    assert streaming_manager._get_content_type("FLAC") == (ContentType.FLAC, ContentType.UNKNOWN)


def test_get_content_type_aac_variants_return_aac(
    streaming_manager: YandexMusicStreamingManager,
) -> None:
    """All AAC codec variants are mapped correctly (MP4 container or plain AAC)."""
    # Plain AAC variants
    assert streaming_manager._get_content_type("aac") == (ContentType.AAC, ContentType.UNKNOWN)
    assert streaming_manager._get_content_type("AAC") == (ContentType.AAC, ContentType.UNKNOWN)
    assert streaming_manager._get_content_type("he-aac") == (ContentType.AAC, ContentType.UNKNOWN)
    assert streaming_manager._get_content_type("HE-AAC") == (ContentType.AAC, ContentType.UNKNOWN)
    # MP4 container variants
    assert streaming_manager._get_content_type("aac-mp4") == (ContentType.MP4, ContentType.AAC)
    assert streaming_manager._get_content_type("AAC-MP4") == (ContentType.MP4, ContentType.AAC)
    assert streaming_manager._get_content_type("he-aac-mp4") == (ContentType.MP4, ContentType.AAC)
    assert streaming_manager._get_content_type("HE-AAC-MP4") == (ContentType.MP4, ContentType.AAC)


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


# --- Audio params tests ---


def test_get_audio_params_flac_mp4(
    streaming_manager: YandexMusicStreamingManager,
) -> None:
    """flac-mp4 returns 48kHz/24bit."""
    assert streaming_manager._get_audio_params("flac-mp4") == (48000, 24)


def test_get_audio_params_flac_mp4_case_insensitive(
    streaming_manager: YandexMusicStreamingManager,
) -> None:
    """flac-mp4 matching is case-insensitive."""
    assert streaming_manager._get_audio_params("FLAC-MP4") == (48000, 24)


def test_get_audio_params_flac(
    streaming_manager: YandexMusicStreamingManager,
) -> None:
    """Plain FLAC returns CD-quality defaults."""
    assert streaming_manager._get_audio_params("flac") == (44100, 16)


def test_get_audio_params_mp3(
    streaming_manager: YandexMusicStreamingManager,
) -> None:
    """MP3 returns CD-quality defaults."""
    assert streaming_manager._get_audio_params("mp3") == (44100, 16)


def test_get_audio_params_none(
    streaming_manager: YandexMusicStreamingManager,
) -> None:
    """None codec returns CD-quality defaults."""
    assert streaming_manager._get_audio_params(None) == (44100, 16)


# --- get_audio_stream tests ---


def _make_encrypted_stream_details(
    key_hex: str,
    url: str = "https://example.com/encrypted.flac",
) -> StreamDetails:
    """Build StreamDetails for encrypted FLAC stream tests."""
    return StreamDetails(
        item_id="test_track_123",
        provider="yandex_music_instance",
        audio_format=AudioFormat(content_type=ContentType.MP4),
        stream_type=StreamType.CUSTOM,
        data={
            "encrypted_url": url,
            "decryption_key": key_hex,
            "codec": "flac-mp4",
        },
    )


class _MockContent:
    """Async iterable content for mock HTTP responses."""

    def __init__(self, chunks: list[bytes], *, drop_payload_error: bool = False) -> None:
        self._chunks = chunks
        self._drop = drop_payload_error

    async def iter_chunked(self, size: int) -> Any:
        for chunk in self._chunks:
            yield chunk
        if self._drop:
            raise ClientPayloadError("connection reset by peer")


class _MockResponse:
    """Fake aiohttp ClientResponse for streaming tests."""

    def __init__(
        self,
        chunks: list[bytes],
        *,
        error: Exception | None = None,
        drop_payload_error: bool = False,
    ) -> None:
        self.content = _MockContent(chunks, drop_payload_error=drop_payload_error)
        self._error = error

    def raise_for_status(self) -> None:
        """Raise stored error if set, simulating a non-2xx HTTP response."""
        if self._error is not None:
            raise self._error

    async def __aenter__(self) -> _MockResponse:
        return self

    async def __aexit__(self, *args: object) -> None:
        pass


class _MockHttpSession:
    """Fake aiohttp ClientSession for streaming tests."""

    def __init__(self, response: _MockResponse) -> None:
        self._response = response

    def get(self, url: str, **kwargs: object) -> _MockResponse:
        return self._response


class _MultiCallHttpSession:
    """Fake aiohttp ClientSession returning successive responses and recording calls."""

    def __init__(self, responses: list[_MockResponse]) -> None:
        self._responses = responses
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: object) -> _MockResponse:
        self.calls.append({"url": url, "headers": kwargs.get("headers", {})})
        return self._responses[len(self.calls) - 1]


async def test_get_audio_stream_invalid_key_length(
    streaming_manager: YandexMusicStreamingManager,
) -> None:
    """Invalid AES key length raises MediaNotFoundError before any HTTP request."""
    sd = _make_encrypted_stream_details("deadbeef")  # 4 bytes — invalid

    with pytest.raises(MediaNotFoundError, match="Unsupported AES key length"):
        async for _ in streaming_manager.get_audio_stream(sd):
            pass


async def test_get_audio_stream_http_error_raises_media_not_found(
    streaming_manager: YandexMusicStreamingManager,
    streaming_provider_stub: StreamingProviderStub,
) -> None:
    """HTTP error from encrypted URL is converted to MediaNotFoundError."""
    key = b"\x00" * 32
    sd = _make_encrypted_stream_details(key.hex())
    streaming_provider_stub.mass.http_session = _MockHttpSession(
        _MockResponse([], error=RuntimeError("403 Forbidden"))
    )

    with pytest.raises(MediaNotFoundError, match="Failed to fetch encrypted stream"):
        async for _ in streaming_manager.get_audio_stream(sd):
            pass


async def test_get_audio_stream_decrypts_aes_ctr_correctly(
    streaming_manager: YandexMusicStreamingManager,
    streaming_provider_stub: StreamingProviderStub,
) -> None:
    """Encrypted stream is decrypted correctly with AES-256-CTR and zero IV."""
    key = b"\x42" * 32
    plaintext = b"Hello, Yandex Music FLAC data!\n" * 50

    # Encrypt with the same algorithm used in get_audio_stream
    nonce_16 = bytes(16)
    encryptor = Cipher(algorithms.AES(key), modes.CTR(nonce_16)).encryptor()
    ciphertext = encryptor.update(plaintext) + encryptor.finalize()

    sd = _make_encrypted_stream_details(key.hex())
    streaming_provider_stub.mass.http_session = _MockHttpSession(_MockResponse([ciphertext]))

    result = b""
    async for chunk in streaming_manager.get_audio_stream(sd):
        result += chunk

    assert result == plaintext


async def test_get_audio_stream_reconnects_with_range_header(
    streaming_manager: YandexMusicStreamingManager,
    streaming_provider_stub: StreamingProviderStub,
) -> None:
    """On ClientPayloadError, reconnects with correct Range header and full plaintext restored."""
    key = b"\x11" * 32
    # 96 bytes = 6 AES-CTR blocks; split at byte 48 (block boundary)
    plaintext = b"AAAAAAAAAAAAAAAA" * 3 + b"BBBBBBBBBBBBBBBB" * 3

    nonce_16 = bytes(16)
    encryptor = Cipher(algorithms.AES(key), modes.CTR(nonce_16)).encryptor()
    ciphertext = encryptor.update(plaintext) + encryptor.finalize()

    drop_at = 48  # exactly 3 blocks — clean block boundary

    # First request drops after 48 bytes; second serves the remainder
    first_resp = _MockResponse([ciphertext[:drop_at]], drop_payload_error=True)
    second_resp = _MockResponse([ciphertext[drop_at:]])
    session = _MultiCallHttpSession([first_resp, second_resp])
    streaming_provider_stub.mass.http_session = session

    result = b""
    with unittest.mock.patch("asyncio.sleep"):
        async for chunk in streaming_manager.get_audio_stream(
            _make_encrypted_stream_details(key.hex())
        ):
            result += chunk

    assert result == plaintext
    assert len(session.calls) == 2
    assert session.calls[0].get("headers") == {}
    assert session.calls[1]["headers"] == {"Range": f"bytes={drop_at}-"}
