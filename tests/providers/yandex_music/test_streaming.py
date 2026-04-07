"""Unit tests for Yandex Music streaming quality selection."""

from __future__ import annotations

import unittest.mock
from typing import TYPE_CHECKING, Any

import pytest
from aiohttp import ClientPayloadError, ServerDisconnectedError
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from music_assistant_models.enums import ContentType, StreamType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.providers.yandex_music import streaming as _streaming_mod
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

    def __init__(
        self,
        chunks: list[bytes],
        *,
        drop_payload_error: bool = False,
        drop_error: Exception | None = None,
    ) -> None:
        self._chunks = chunks
        self._drop_error: Exception | None = (
            ClientPayloadError("connection reset by peer") if drop_payload_error else drop_error
        )

    async def iter_chunked(self, size: int) -> Any:
        for chunk in self._chunks:
            yield chunk
        if self._drop_error is not None:
            raise self._drop_error


class _MockResponse:
    """Fake aiohttp ClientResponse for streaming tests."""

    def __init__(
        self,
        chunks: list[bytes],
        *,
        status: int = 200,
        error: Exception | None = None,
        drop_payload_error: bool = False,
        drop_error: Exception | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.content = _MockContent(
            chunks,
            drop_payload_error=drop_payload_error,
            drop_error=drop_error,
        )
        self.status = status
        self._error = error
        self.headers: dict[str, str] = headers or {}

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

    # First request drops after 48 bytes; second serves the remainder with 206 Partial Content
    first_resp = _MockResponse([ciphertext[:drop_at]], drop_payload_error=True)
    second_resp = _MockResponse([ciphertext[drop_at:]], status=206)
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
    assert session.calls[0]["headers"] == {"Range": "bytes=0-4194303"}
    assert session.calls[1]["headers"] == {"Range": f"bytes={drop_at}-{drop_at + 4194304 - 1}"}


async def test_get_audio_stream_refreshes_url_on_410(
    streaming_manager: YandexMusicStreamingManager,
    streaming_provider_stub: StreamingProviderStub,
) -> None:
    """On HTTP 410 (URL expired), a fresh URL is fetched and streaming resumes."""
    key = b"\x33" * 32
    plaintext = b"LOSSLESS" * 32

    nonce_16 = bytes(16)
    encryptor = Cipher(algorithms.AES(key), modes.CTR(nonce_16)).encryptor()
    ciphertext = encryptor.update(plaintext) + encryptor.finalize()

    fresh_url = "https://cdn.yandex.net/fresh.flac"
    expired_resp = _MockResponse([], status=410)
    fresh_resp = _MockResponse([ciphertext])

    call_count = 0

    def _get(_url: str, **_kwargs: object) -> _MockResponse:
        nonlocal call_count
        call_count += 1
        return expired_resp if call_count == 1 else fresh_resp

    streaming_provider_stub.mass.http_session = unittest.mock.MagicMock()
    streaming_provider_stub.mass.http_session.get = _get

    # Mock get_track_file_info_lossless to return a fresh URL
    streaming_provider_stub.client = unittest.mock.AsyncMock()
    streaming_provider_stub.client.get_track_file_info_lossless = unittest.mock.AsyncMock(
        return_value={"url": fresh_url, "codec": "flac-mp4", "key": key.hex()}
    )
    streaming_manager.client = streaming_provider_stub.client

    result = b""
    with unittest.mock.patch("asyncio.sleep"):
        async for chunk in streaming_manager.get_audio_stream(
            _make_encrypted_stream_details(key.hex())
        ):
            result += chunk

    assert result == plaintext
    streaming_provider_stub.client.get_track_file_info_lossless.assert_called_once_with(
        "test_track_123"
    )


async def test_get_audio_stream_raises_after_all_retries_on_410(
    streaming_manager: YandexMusicStreamingManager,
    streaming_provider_stub: StreamingProviderStub,
) -> None:
    """MediaNotFoundError is raised after all retries exhausted on persistent 410."""
    key = b"\x44" * 32
    sd = _make_encrypted_stream_details(key.hex())
    streaming_provider_stub.mass.http_session = _MockHttpSession(_MockResponse([], status=410))
    streaming_provider_stub.client = unittest.mock.AsyncMock()
    streaming_provider_stub.client.get_track_file_info_lossless = unittest.mock.AsyncMock(
        return_value={"url": "https://cdn.example.com/still-expired.flac", "key": key.hex()}
    )
    streaming_manager.client = streaming_provider_stub.client

    with (
        pytest.raises(MediaNotFoundError, match="retries exhausted"),
        unittest.mock.patch("asyncio.sleep"),
    ):
        async for _ in streaming_manager.get_audio_stream(sd):
            pass


async def test_get_audio_stream_retries_on_server_disconnected(
    streaming_manager: YandexMusicStreamingManager,
    streaming_provider_stub: StreamingProviderStub,
) -> None:
    """On ServerDisconnectedError, reconnects with Range header and full plaintext is restored."""
    key = b"\x55" * 32
    plaintext = b"CCCCCCCCCCCCCCCC" * 3 + b"DDDDDDDDDDDDDDDD" * 3  # 96 bytes

    nonce_16 = bytes(16)
    encryptor = Cipher(algorithms.AES(key), modes.CTR(nonce_16)).encryptor()
    ciphertext = encryptor.update(plaintext) + encryptor.finalize()

    drop_at = 48
    first_resp = _MockResponse(
        [ciphertext[:drop_at]], drop_error=ServerDisconnectedError("server closed")
    )
    second_resp = _MockResponse([ciphertext[drop_at:]], status=206)
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
    assert session.calls[1]["headers"] == {"Range": f"bytes={drop_at}-{drop_at + 4194304 - 1}"}


async def test_get_audio_stream_retries_on_read_timeout(
    streaming_manager: YandexMusicStreamingManager,
    streaming_provider_stub: StreamingProviderStub,
) -> None:
    """On asyncio.TimeoutError (read stall), reconnects with Range header and stream completes."""
    key = b"\x66" * 32
    plaintext = b"EEEEEEEEEEEEEEEE" * 3 + b"FFFFFFFFFFFFFFFF" * 3  # 96 bytes

    nonce_16 = bytes(16)
    encryptor = Cipher(algorithms.AES(key), modes.CTR(nonce_16)).encryptor()
    ciphertext = encryptor.update(plaintext) + encryptor.finalize()

    drop_at = 48
    first_resp = _MockResponse([ciphertext[:drop_at]], drop_error=TimeoutError("read timeout"))
    second_resp = _MockResponse([ciphertext[drop_at:]], status=206)
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
    assert session.calls[1]["headers"] == {"Range": f"bytes={drop_at}-{drop_at + 4194304 - 1}"}


async def test_get_audio_stream_resets_decrypt_when_range_ignored(
    streaming_manager: YandexMusicStreamingManager,
    streaming_provider_stub: StreamingProviderStub,
) -> None:
    """If server returns 200 instead of 206 after Range request, decryptor resets to position 0."""
    key = b"\x77" * 32
    plaintext = b"GGGGGGGGGGGGGGGG" * 6  # 96 bytes

    nonce_16 = bytes(16)
    encryptor = Cipher(algorithms.AES(key), modes.CTR(nonce_16)).encryptor()
    ciphertext = encryptor.update(plaintext) + encryptor.finalize()

    drop_at = 48
    # First response drops after 48 bytes; second ignores Range and sends full ciphertext with 200
    first_resp = _MockResponse([ciphertext[:drop_at]], drop_payload_error=True)
    second_resp = _MockResponse([ciphertext], status=200)  # server ignored Range
    session = _MultiCallHttpSession([first_resp, second_resp])
    streaming_provider_stub.mass.http_session = session

    result = b""
    with unittest.mock.patch("asyncio.sleep"):
        async for chunk in streaming_manager.get_audio_stream(
            _make_encrypted_stream_details(key.hex())
        ):
            result += chunk

    # Decryptor was reset to 0 on the second request, so full plaintext is recovered
    assert result == plaintext


async def test_get_audio_stream_fails_immediately_when_url_refresh_returns_nothing(
    streaming_manager: YandexMusicStreamingManager,
    streaming_provider_stub: StreamingProviderStub,
) -> None:
    """If get_track_file_info_lossless returns no URL, stream fails without wasting retries."""
    key = b"\x88" * 32
    sd = _make_encrypted_stream_details(key.hex())
    streaming_provider_stub.mass.http_session = _MockHttpSession(_MockResponse([], status=410))
    streaming_provider_stub.client = unittest.mock.AsyncMock()
    # Simulate API returning no usable URL (None result)
    streaming_provider_stub.client.get_track_file_info_lossless = unittest.mock.AsyncMock(
        return_value=None
    )
    streaming_manager.client = streaming_provider_stub.client

    with (
        pytest.raises(MediaNotFoundError, match="retries exhausted"),
        unittest.mock.patch("asyncio.sleep"),
    ):
        async for _ in streaming_manager.get_audio_stream(sd):
            pass

    # Should have given up after attempt 0 (refresh returned None → no stale URL reuse)
    assert streaming_provider_stub.client.get_track_file_info_lossless.call_count == 1


async def test_get_audio_stream_exact_window_boundary(
    streaming_manager: YandexMusicStreamingManager,
    streaming_provider_stub: StreamingProviderStub,
) -> None:
    """File whose size is an exact multiple of _RANGE_WINDOW does not trigger a 416 error.

    Without the Content-Range EOF guard the loop would request the next window after
    receiving exactly _RANGE_WINDOW bytes in a 206 response, which would result in a
    416 Range Not Satisfiable error raised as MediaNotFoundError.  With the fix the
    loop detects EOF via the Content-Range header and returns cleanly.
    """
    # Use a tiny window (16 bytes = one AES block) so the test stays fast.
    small_window = 16
    key = b"\xab" * 32
    plaintext = b"x" * small_window  # file size == window size (exact boundary)

    nonce_16 = bytes(16)
    encryptor = Cipher(algorithms.AES(key), modes.CTR(nonce_16)).encryptor()
    ciphertext = encryptor.update(plaintext) + encryptor.finalize()

    # Content-Range: bytes 0-15/16  — signals that byte 15 is the last one
    first_resp = _MockResponse(
        [ciphertext],
        status=206,
        headers={"Content-Range": f"bytes 0-{small_window - 1}/{small_window}"},
    )
    # Second response must never be reached; if it is, the test will fail.
    second_resp = _MockResponse([], status=416, error=RuntimeError("should not be requested"))
    session = _MultiCallHttpSession([first_resp, second_resp])
    streaming_provider_stub.mass.http_session = session

    result = b""
    with unittest.mock.patch.object(_streaming_mod, "_RANGE_WINDOW", small_window):
        async for chunk in streaming_manager.get_audio_stream(
            _make_encrypted_stream_details(key.hex())
        ):
            result += chunk

    assert result == plaintext
    assert len(session.calls) == 1, "second window must not be requested when EOF is detected"


async def test_get_audio_stream_continues_after_non_block_boundary_drop(
    streaming_manager: YandexMusicStreamingManager,
    streaming_provider_stub: StreamingProviderStub,
) -> None:
    """TCP drop at a non-AES-block boundary must not cause premature EOF on reconnect.

    Scenario (patched window = 32 bytes = 2 AES blocks, 50-byte file):
    - Window 1 (bytes=0-31) drops at byte 17 (not on a 16-byte AES boundary).
    - Reconnect re-requests from block_start=16; server returns full 32 bytes.
    - Old bug: window_got = 31 < _RANGE_WINDOW = 32 → stream terminates at byte 48,
      losing the final 2 bytes of the file.
    - Fixed:   received = window_got + block_skip = 31 + 1 = 32 = _RANGE_WINDOW
      → stream continues to window 2, which delivers the remaining 2 bytes.
    """
    small_window = 32  # 2 AES blocks
    key = b"\xcc" * 32
    plaintext = b"X" * 50  # 50 bytes → two windows (32 + 2 remaining)

    nonce_16 = bytes(16)
    encryptor = Cipher(algorithms.AES(key), modes.CTR(nonce_16)).encryptor()
    ciphertext = encryptor.update(plaintext) + encryptor.finalize()

    drop_at = 17  # non-block boundary (17 % 16 != 0)

    # Window 1: bytes=0-31, drops after delivering 17 bytes
    resp1 = _MockResponse([ciphertext[:drop_at]], drop_payload_error=True)
    # Reconnect: block_start=16, requests bytes=16-47, server returns full 32 bytes
    resp2 = _MockResponse([ciphertext[16:48]], status=206)
    # Window 2: bytes=48-79, only 2 bytes remain in the file
    resp3 = _MockResponse([ciphertext[48:50]], status=206)

    session = _MultiCallHttpSession([resp1, resp2, resp3])
    streaming_provider_stub.mass.http_session = session

    result = b""
    with (
        unittest.mock.patch.object(_streaming_mod, "_RANGE_WINDOW", small_window),
        unittest.mock.patch("asyncio.sleep"),
    ):
        async for chunk in streaming_manager.get_audio_stream(
            _make_encrypted_stream_details(key.hex())
        ):
            result += chunk

    assert result == plaintext, f"Expected {len(plaintext)} bytes, got {len(result)}"
    assert len(session.calls) == 3
    assert session.calls[0]["headers"] == {"Range": "bytes=0-31"}
    assert session.calls[1]["headers"] == {"Range": "bytes=16-47"}  # AES-aligned reconnect
    assert session.calls[2]["headers"] == {"Range": "bytes=48-79"}  # second window
