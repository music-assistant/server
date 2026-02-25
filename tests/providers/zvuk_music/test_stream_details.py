"""Tests for get_stream_details with direct FLAC streaming support."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ContentType
from music_assistant_models.errors import MediaNotFoundError

from music_assistant.providers.zvuk_music.api_client import ZvukMusicClient
from music_assistant.providers.zvuk_music.provider import ZvukMusicProvider


def _make_mock_track(has_flac: bool = True, duration: int = 240) -> MagicMock:
    """Create a mock ZvukTrack with configurable has_flac and duration.

    :param has_flac: Whether FLAC is available for this track.
    :param duration: Track duration in seconds.
    :return: Mock track object.
    """
    track = MagicMock()
    track.has_flac = has_flac
    track.duration = duration
    return track


def _make_provider(quality_pref: str) -> ZvukMusicProvider:
    """Create a ZvukMusicProvider with mocked MA and config.

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


class TestGetDirectStreamUrl:
    """Tests for ZvukMusicClient.get_direct_stream_url."""

    @pytest.mark.asyncio
    async def test_returns_stream_url_for_flac(self) -> None:
        """get_direct_stream_url returns the stream URL string from API response."""
        client = ZvukMusicClient(token="test", http_session=MagicMock())
        client._tiny_get = AsyncMock(  # type: ignore[method-assign]
            return_value={"stream": "https://cdn.zvuk.com/track.flac?token=abc"}
        )

        url = await client.get_direct_stream_url("12345", "flac")

        assert url == "https://cdn.zvuk.com/track.flac?token=abc"
        client._tiny_get.assert_called_once_with("track/stream", {"quality": "flac", "id": "12345"})

    @pytest.mark.asyncio
    async def test_returns_none_when_stream_missing(self) -> None:
        """get_direct_stream_url returns None when API result has no stream key."""
        client = ZvukMusicClient(token="test", http_session=MagicMock())
        client._tiny_get = AsyncMock(return_value={})  # type: ignore[method-assign]

        url = await client.get_direct_stream_url("12345", "flac")

        assert url is None

    @pytest.mark.asyncio
    async def test_returns_none_when_api_returns_none(self) -> None:
        """get_direct_stream_url returns None when API returns None."""
        client = ZvukMusicClient(token="test", http_session=MagicMock())
        client._tiny_get = AsyncMock(return_value=None)  # type: ignore[method-assign]

        url = await client.get_direct_stream_url("12345", "high")

        assert url is None

    @pytest.mark.asyncio
    async def test_passes_quality_param(self) -> None:
        """get_direct_stream_url passes the quality parameter to the API."""
        client = ZvukMusicClient(token="test", http_session=MagicMock())
        client._tiny_get = AsyncMock(  # type: ignore[method-assign]
            return_value={"stream": "https://cdn.zvuk.com/track.mp3"}
        )

        await client.get_direct_stream_url("99999", "high")

        call_args = client._tiny_get.call_args
        assert call_args.args[1]["quality"] == "high"
        assert call_args.args[1]["id"] == "99999"


class TestGetStreamDetailsFlac:
    """Tests for get_stream_details with FLAC streaming logic."""

    @pytest.mark.asyncio
    async def test_lossless_with_has_flac_requests_flac(self) -> None:
        """When lossless is requested and track has FLAC, ContentType.FLAC is returned."""
        provider = MagicMock(spec=ZvukMusicProvider)
        provider.config = MagicMock()
        provider.config.get_value = MagicMock(return_value="lossless")
        provider.instance_id = "zvuk_music--test"

        mock_client = MagicMock(spec=ZvukMusicClient)
        mock_client.get_track = AsyncMock(return_value=_make_mock_track(has_flac=True))
        mock_client.get_direct_stream_url = AsyncMock(
            return_value="https://cdn.zvuk.com/track.flac"
        )
        provider.client = mock_client
        provider.logger = MagicMock()

        result = await ZvukMusicProvider.get_stream_details(provider, "12345")

        assert result.audio_format.content_type == ContentType.FLAC
        assert result.audio_format.bit_rate == 0
        mock_client.get_direct_stream_url.assert_called_with("12345", "flac")

    @pytest.mark.asyncio
    async def test_lossless_always_tries_flac_first(self) -> None:
        """When lossless is requested, FLAC is always attempted first regardless of has_flac."""
        provider = MagicMock(spec=ZvukMusicProvider)
        provider.config = MagicMock()
        provider.config.get_value = MagicMock(return_value="lossless")
        provider.instance_id = "zvuk_music--test"

        mock_client = MagicMock(spec=ZvukMusicClient)
        # has_flac=False but API actually returns FLAC URL
        mock_client.get_track = AsyncMock(return_value=_make_mock_track(has_flac=False))
        mock_client.get_direct_stream_url = AsyncMock(
            return_value="https://cdn.zvuk.com/track.flac"
        )
        provider.client = mock_client
        provider.logger = MagicMock()

        result = await ZvukMusicProvider.get_stream_details(provider, "12345")

        assert result.audio_format.content_type == ContentType.FLAC
        # Must try flac first even when has_flac=False
        calls = [c.args[1] for c in mock_client.get_direct_stream_url.call_args_list]
        assert calls[0] == "flac"

    @pytest.mark.asyncio
    async def test_flac_url_failure_falls_back_to_high(self) -> None:
        """When FLAC URL request returns None, falls back to HIGH MP3."""
        provider = MagicMock(spec=ZvukMusicProvider)
        provider.config = MagicMock()
        provider.config.get_value = MagicMock(return_value="lossless")
        provider.instance_id = "zvuk_music--test"

        mock_client = MagicMock(spec=ZvukMusicClient)
        mock_client.get_track = AsyncMock(return_value=_make_mock_track(has_flac=True))

        def stream_url_side_effect(_track_id: str, quality: str) -> str | None:
            if quality == "flac":
                return None  # FLAC unavailable for this track
            return "https://cdn.zvuk.com/track.mp3"

        mock_client.get_direct_stream_url = AsyncMock(side_effect=stream_url_side_effect)
        provider.client = mock_client
        provider.logger = MagicMock()

        result = await ZvukMusicProvider.get_stream_details(provider, "12345")

        assert result.audio_format.content_type == ContentType.MP3
        assert result.audio_format.bit_rate == 320

    @pytest.mark.asyncio
    async def test_high_quality_pref_skips_flac(self) -> None:
        """When high (not lossless) is preferred, FLAC is never requested."""
        provider = MagicMock(spec=ZvukMusicProvider)
        provider.config = MagicMock()
        provider.config.get_value = MagicMock(return_value="high")
        provider.instance_id = "zvuk_music--test"

        mock_client = MagicMock(spec=ZvukMusicClient)
        mock_client.get_track = AsyncMock(return_value=_make_mock_track(has_flac=True))
        mock_client.get_direct_stream_url = AsyncMock(return_value="https://cdn.zvuk.com/track.mp3")
        provider.client = mock_client
        provider.logger = MagicMock()

        result = await ZvukMusicProvider.get_stream_details(provider, "12345")

        assert result.audio_format.content_type == ContentType.MP3
        calls = [c.args[1] for c in mock_client.get_direct_stream_url.call_args_list]
        assert "flac" not in calls

    @pytest.mark.asyncio
    async def test_stream_path_is_url(self) -> None:
        """StreamDetails.path contains the URL returned by get_direct_stream_url."""
        provider = MagicMock(spec=ZvukMusicProvider)
        provider.config = MagicMock()
        provider.config.get_value = MagicMock(return_value="lossless")
        provider.instance_id = "zvuk_music--test"

        expected_url = "https://cdn.zvuk.com/track.flac?token=xyz"
        mock_client = MagicMock(spec=ZvukMusicClient)
        mock_client.get_track = AsyncMock(return_value=_make_mock_track(has_flac=True))
        mock_client.get_direct_stream_url = AsyncMock(return_value=expected_url)
        provider.client = mock_client
        provider.logger = MagicMock()

        result = await ZvukMusicProvider.get_stream_details(provider, "12345")

        assert result.path == expected_url

    @pytest.mark.asyncio
    async def test_duration_from_track_metadata(self) -> None:
        """StreamDetails.duration is populated from track.duration."""
        provider = MagicMock(spec=ZvukMusicProvider)
        provider.config = MagicMock()
        provider.config.get_value = MagicMock(return_value="high")
        provider.instance_id = "zvuk_music--test"

        mock_client = MagicMock(spec=ZvukMusicClient)
        mock_client.get_track = AsyncMock(
            return_value=_make_mock_track(has_flac=False, duration=333)
        )
        mock_client.get_direct_stream_url = AsyncMock(return_value="https://cdn.zvuk.com/track.mp3")
        provider.client = mock_client
        provider.logger = MagicMock()

        result = await ZvukMusicProvider.get_stream_details(provider, "12345")

        assert result.duration == 333

    @pytest.mark.asyncio
    async def test_raises_when_all_urls_none(self) -> None:
        """MediaNotFoundError is raised when all quality attempts return None."""
        provider = MagicMock(spec=ZvukMusicProvider)
        provider.config = MagicMock()
        provider.config.get_value = MagicMock(return_value="high")
        provider.instance_id = "zvuk_music--test"

        mock_client = MagicMock(spec=ZvukMusicClient)
        mock_client.get_track = AsyncMock(return_value=_make_mock_track(has_flac=False))
        mock_client.get_direct_stream_url = AsyncMock(return_value=None)
        provider.client = mock_client
        provider.logger = MagicMock()

        with pytest.raises(MediaNotFoundError):
            await ZvukMusicProvider.get_stream_details(provider, "12345")
