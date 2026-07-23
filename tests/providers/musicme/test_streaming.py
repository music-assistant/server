"""Test MusicMe provider streaming methods."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import MediaNotFoundError

from music_assistant.providers.musicme.constants import PARTNER_ID, STREAM_BASE
from music_assistant.providers.musicme.provider import MusicMeProvider

# ---------------------------------------------------------------------------
# _get_stream_url
# ---------------------------------------------------------------------------


class TestGetStreamUrl:
    """Tests for _get_stream_url."""

    @pytest.mark.asyncio
    async def test_returns_stream_url(self, provider: MusicMeProvider) -> None:
        """Test that a valid ticket is decrypted and a stream URL is built."""
        # Build a fake encrypted ticket that decrypt() can handle.
        # We mock _api_get to return a response with a "ticket" field,
        # then mock decrypt to return a known token.
        fake_ticket = "FAKETOKEN123"
        provider._api_get = AsyncMock(return_value={"ticket": "dummy_encrypted"})  # type: ignore[method-assign]

        with patch(
            "music_assistant.providers.musicme.provider.decrypt",
            return_value=fake_ticket,
        ):
            url = await provider._get_stream_url("5034644330297-01_01")

        assert url == f"{STREAM_BASE}/{PARTNER_ID}/{fake_ticket}.mp4"
        provider._api_get.assert_called_once()
        call_args = provider._api_get.call_args[0][0]
        assert "/getstream/" in call_args
        assert "ref=5034644330297-01_01" in call_args

    @pytest.mark.asyncio
    async def test_raises_on_no_ticket(self, provider: MusicMeProvider) -> None:
        """Test that missing ticket raises MediaNotFoundError."""
        provider._api_get = AsyncMock(return_value={"no_ticket_here": True})  # type: ignore[method-assign]

        with pytest.raises(MediaNotFoundError, match="No streaming ticket"):
            await provider._get_stream_url("5034644330297-01_01")

    @pytest.mark.asyncio
    async def test_raises_on_none_response(self, provider: MusicMeProvider) -> None:
        """Test that None API response raises MediaNotFoundError."""
        provider._api_get = AsyncMock(return_value=None)  # type: ignore[method-assign]

        with pytest.raises(MediaNotFoundError, match="No streaming ticket"):
            await provider._get_stream_url("5034644330297-01_01")


# ---------------------------------------------------------------------------
# _get_radio_track
# ---------------------------------------------------------------------------


class TestGetRadioTrack:
    """Tests for _get_radio_track."""

    @pytest.mark.asyncio
    async def test_returns_first_streamable_track(self, provider: MusicMeProvider) -> None:
        """Test that the first streamable track barcode is returned."""
        provider._api_get = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "results": {
                    "tracks": [
                        {"barcode": "AAA-01_01", "streamable": 0},
                        {"barcode": "BBB-01_01", "streamable": 2},
                        {"barcode": "CCC-01_01", "streamable": 2},
                    ]
                }
            }
        )

        result = await provider._get_radio_track("rd-123")
        assert result == "BBB-01_01"

    @pytest.mark.asyncio
    async def test_raises_on_no_data(self, provider: MusicMeProvider) -> None:
        """Test that None response raises MediaNotFoundError."""
        provider._api_get = AsyncMock(return_value=None)  # type: ignore[method-assign]

        with pytest.raises(MediaNotFoundError, match="returned no data"):
            await provider._get_radio_track("rd-123")

    @pytest.mark.asyncio
    async def test_raises_on_no_streamable_tracks(self, provider: MusicMeProvider) -> None:
        """Test that no streamable tracks raises MediaNotFoundError."""
        provider._api_get = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "results": {
                    "tracks": [
                        {"barcode": "AAA-01_01", "streamable": 1},
                        {"barcode": "BBB-01_01", "streamable": 0},
                    ]
                }
            }
        )

        with pytest.raises(MediaNotFoundError, match="No streamable tracks"):
            await provider._get_radio_track("rd-123")


# ---------------------------------------------------------------------------
# get_stream_details
# ---------------------------------------------------------------------------


class TestGetStreamDetails:
    """Tests for get_stream_details."""

    @pytest.mark.asyncio
    async def test_track_stream_details(self, provider: MusicMeProvider) -> None:
        """Test stream details for a regular track."""
        fake_url = f"{STREAM_BASE}/{PARTNER_ID}/TOKEN123.mp4"
        provider._get_stream_url = AsyncMock(return_value=fake_url)  # type: ignore[method-assign]

        details = await provider.get_stream_details("5034644330297-01_01", MediaType.TRACK)

        assert details.item_id == "5034644330297-01_01"
        assert details.stream_type == StreamType.HTTP
        assert details.audio_format.content_type == ContentType.MP4
        assert details.audio_format.sample_rate == 44100
        assert details.audio_format.channels == 2
        assert details.path == fake_url
        assert details.can_seek is True
        provider._get_stream_url.assert_called_once_with("5034644330297-01_01")

    @pytest.mark.asyncio
    async def test_radio_resolves_track_first(self, provider: MusicMeProvider) -> None:
        """Test that radio media type resolves to a track barcode before streaming."""
        fake_url = f"{STREAM_BASE}/{PARTNER_ID}/TOKEN456.mp4"
        provider._get_radio_track = AsyncMock(return_value="RESOLVED-01_01")  # type: ignore[method-assign]
        provider._get_stream_url = AsyncMock(return_value=fake_url)  # type: ignore[method-assign]

        details = await provider.get_stream_details("rd-1163935", MediaType.RADIO)

        provider._get_radio_track.assert_called_once_with("rd-1163935")
        provider._get_stream_url.assert_called_once_with("RESOLVED-01_01")
        assert details.item_id == "rd-1163935"
        assert details.path == fake_url
