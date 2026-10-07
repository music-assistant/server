"""Tests for get_stream_details with direct FLAC streaming support."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ContentType
from music_assistant_models.errors import MediaNotFoundError

from music_assistant.providers.zvuk_music.api_client import ZvukMusicClient
from music_assistant.providers.zvuk_music.provider import ZvukMusicProvider


def _make_mock_track(has_flac: bool = True, duration: int = 240) -> MagicMock:
    """
    Create a mock ZvukTrack with configurable has_flac and duration.

    :param has_flac: Whether FLAC is available for this track.
    :param duration: Track duration in seconds.
    :return: Mock track object.
    """
    track = MagicMock()
    track.has_flac = has_flac
    track.duration = duration
    return track


def _make_provider(quality_pref: str) -> Any:
    """
    Create a ZvukMusicProvider with mocked MA and config.

    :param quality_pref: Quality preference string ("lossless" or "high").
    :return: Configured provider instance.
    """
    provider = MagicMock(spec=ZvukMusicProvider)

    config = MagicMock()
    config.get_value = MagicMock(return_value=quality_pref)
    provider.config = config

    provider.instance_id = "zvuk_music--test"
    provider.client = MagicMock(spec=ZvukMusicClient)
    provider.logger = MagicMock()

    return provider


def _make_client(
    track: MagicMock, flac: str | None, high: str | None, mid: str | None
) -> MagicMock:
    """
    Create a mocked ZvukMusicClient returning one GraphQL stream entry.

    :param track: Track returned by get_track.
    :param flac: URL in the ``flac`` field.
    :param high: URL in the ``high`` field.
    :param mid: URL in the ``mid`` field.
    """
    client = MagicMock(spec=ZvukMusicClient)
    client.get_track = AsyncMock(return_value=track)
    client.get_stream_urls = AsyncMock(return_value=[MagicMock(flac=flac, high=high, mid=mid)])
    return client


class TestGetStreamDetailsFlac:
    """Tests for get_stream_details quality selection."""

    @pytest.mark.asyncio
    async def test_lossless_with_flac_returns_flac_in_mp4(self) -> None:
        """Lossless with a FLAC URL returns FLAC inside an MP4 container."""
        provider = _make_provider("lossless")
        provider.client = _make_client(
            _make_mock_track(), "https://cdn.zvuk.com/t.mp4", "https://h", "https://m"
        )

        result = await ZvukMusicProvider.get_stream_details(provider, "12345")

        assert result.audio_format.content_type == ContentType.MP4
        assert result.audio_format.codec_type == ContentType.FLAC
        assert result.path == "https://cdn.zvuk.com/t.mp4"

    @pytest.mark.asyncio
    async def test_flac_missing_falls_back_to_high(self) -> None:
        """When no FLAC URL is offered, falls back to HIGH MP3."""
        provider = _make_provider("lossless")
        provider.client = _make_client(
            _make_mock_track(has_flac=False), None, "https://cdn.zvuk.com/track.mp3", "https://m"
        )

        result = await ZvukMusicProvider.get_stream_details(provider, "12345")

        assert result.audio_format.content_type == ContentType.MP3
        assert result.audio_format.bit_rate == 320

    @pytest.mark.asyncio
    async def test_high_quality_pref_skips_flac(self) -> None:
        """When high (not lossless) is preferred, FLAC is never selected."""
        provider = _make_provider("high")
        provider.client = _make_client(
            _make_mock_track(), "https://cdn.zvuk.com/t.mp4", "https://cdn.zvuk.com/track.mp3", None
        )

        result = await ZvukMusicProvider.get_stream_details(provider, "12345")

        assert result.audio_format.content_type == ContentType.MP3
        assert result.path == "https://cdn.zvuk.com/track.mp3"

    @pytest.mark.asyncio
    async def test_mid_when_only_mid_available(self) -> None:
        """MP3 128 is used when it is the only quality offered."""
        provider = _make_provider("high")
        provider.client = _make_client(_make_mock_track(), None, None, "https://cdn.zvuk.com/m.mp3")

        result = await ZvukMusicProvider.get_stream_details(provider, "12345")

        assert result.audio_format.bit_rate == 128
        assert result.path == "https://cdn.zvuk.com/m.mp3"

    @pytest.mark.asyncio
    async def test_duration_from_track_metadata(self) -> None:
        """StreamDetails.duration is populated from track.duration."""
        provider = _make_provider("high")
        provider.client = _make_client(
            _make_mock_track(has_flac=False, duration=333), None, "https://h", None
        )

        result = await ZvukMusicProvider.get_stream_details(provider, "12345")

        assert result.duration == 333

    @pytest.mark.asyncio
    async def test_raises_when_all_urls_none(self) -> None:
        """MediaNotFoundError is raised when no quality is available."""
        provider = _make_provider("lossless")
        provider.client = _make_client(_make_mock_track(has_flac=False), None, None, None)

        with pytest.raises(MediaNotFoundError):
            await ZvukMusicProvider.get_stream_details(provider, "12345")

    @pytest.mark.asyncio
    async def test_raises_when_no_stream_entries(self) -> None:
        """MediaNotFoundError is raised when the API returns no stream entry."""
        provider = _make_provider("high")
        client = _make_client(_make_mock_track(), None, None, None)
        client.get_stream_urls = AsyncMock(return_value=[])
        provider.client = client

        with pytest.raises(MediaNotFoundError):
            await ZvukMusicProvider.get_stream_details(provider, "12345")
