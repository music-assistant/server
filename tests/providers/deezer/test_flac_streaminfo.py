"""Test that Deezer FLAC tracks report the format found in their STREAMINFO block."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import AudioError

from music_assistant.providers.deezer.helpers import parse_flac_streaminfo
from music_assistant.providers.deezer.streaming import DeezerStreamingManager


def _flac_header(sample_rate: int, bit_depth: int, channels: int, block_type: int = 0) -> bytes:
    """Return the first 2048 bytes of a FLAC file with the given STREAMINFO values."""
    packed = (sample_rate << 44) | ((channels - 1) << 41) | ((bit_depth - 1) << 36)
    streaminfo = bytes(10) + packed.to_bytes(8, "big") + bytes(16)
    return (b"fLaC" + bytes([block_type]) + (34).to_bytes(3, "big") + streaminfo).ljust(
        2048, b"\x00"
    )


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (_flac_header(44100, 16, 2), (44100, 16, 2)),
        (_flac_header(88200, 24, 2), (88200, 24, 2)),
        (_flac_header(192000, 24, 2), (192000, 24, 2)),
        (b"ID3\x04" + _flac_header(192000, 24, 2), None),
        (_flac_header(192000, 24, 2)[:41], None),
        (_flac_header(192000, 24, 2, block_type=4), None),
    ],
)
def test_parse_flac_streaminfo(data: bytes, expected: tuple[int, int, int] | None) -> None:
    """STREAMINFO is parsed, anything that does not start with it is rejected."""
    assert parse_flac_streaminfo(data) == expected


def _streaming(
    monkeypatch: pytest.MonkeyPatch,
    track_format: str,
    first_chunk: bytes | Exception,
    cached: list[int] | None = None,
) -> tuple[DeezerStreamingManager, Mock]:
    """Return a streaming manager for one track and its mocked provider."""
    provider = Mock()
    provider.gw_client.get_deezer_track_urls = AsyncMock(
        return_value=(
            {"format": track_format, "sources": [{"url": "http://x"}]},
            {"SNG_ID": "1", "DURATION": "60", f"FILESIZE_{track_format}": "1000"},
        )
    )
    provider.mass.cache.get = AsyncMock(return_value=cached)
    provider.mass.cache.set = AsyncMock()
    response = MagicMock(status=206)
    if isinstance(first_chunk, Exception):
        response.content.readexactly = AsyncMock(side_effect=first_chunk)
    else:
        response.content.readexactly = AsyncMock(return_value=first_chunk)
    provider.mass.http_session.get.return_value.__aenter__ = AsyncMock(return_value=response)
    provider.mass.http_session.get.return_value.__aexit__ = AsyncMock(return_value=False)
    streaming = DeezerStreamingManager(provider)
    monkeypatch.setattr(streaming, "_get_blowfish_key", lambda _track_id: "0" * 16)
    monkeypatch.setattr(streaming, "_decrypt_chunk", lambda chunk, _key: chunk)
    return streaming, provider


async def test_flac_track_reports_real_format(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hi-res FLAC track is declared with the format from its header, which is cached."""
    streaming, provider = _streaming(monkeypatch, "FLAC", _flac_header(88200, 24, 2))

    details = await streaming.get_stream_details("1", MediaType.TRACK)

    assert (details.audio_format.sample_rate, details.audio_format.bit_depth) == (88200, 24)
    provider.mass.cache.set.assert_awaited_once()


async def test_cached_format_skips_the_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cached result is used without fetching the track."""
    streaming, provider = _streaming(monkeypatch, "FLAC", b"", cached=[96000, 24, 2])

    details = await streaming.get_stream_details("1", MediaType.TRACK)

    assert (details.audio_format.sample_rate, details.audio_format.bit_depth) == (96000, 24)
    provider.mass.http_session.get.assert_not_called()


@pytest.mark.parametrize("first_chunk", [TimeoutError(), b"ID3" * 700])
async def test_failed_probe_keeps_default_format(
    monkeypatch: pytest.MonkeyPatch, first_chunk: bytes | Exception
) -> None:
    """A probe that fails keeps the default format and caches nothing."""
    streaming, provider = _streaming(monkeypatch, "FLAC", first_chunk)

    details = await streaming.get_stream_details("1", MediaType.TRACK)

    assert (details.audio_format.sample_rate, details.audio_format.bit_depth) == (44100, 16)
    provider.mass.cache.set.assert_not_called()


async def test_missing_decrypt_key_skips_the_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """A build without the decryption key keeps the default format and fetches nothing."""
    streaming, provider = _streaming(monkeypatch, "FLAC", _flac_header(88200, 24, 2))

    def _no_key(_track_id: str) -> str:
        raise AudioError("no key")

    monkeypatch.setattr(streaming, "_get_blowfish_key", _no_key)

    details = await streaming.get_stream_details("1", MediaType.TRACK)

    assert (details.audio_format.sample_rate, details.audio_format.bit_depth) == (44100, 16)
    provider.mass.http_session.get.assert_not_called()
    provider.mass.cache.set.assert_not_called()


async def test_mp3_track_is_not_probed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only FLAC tracks are probed."""
    streaming, provider = _streaming(monkeypatch, "MP3_320", b"")

    await streaming.get_stream_details("1", MediaType.TRACK)

    provider.mass.http_session.get.assert_not_called()
