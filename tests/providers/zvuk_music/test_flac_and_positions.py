"""Tests for GraphQL FLAC streaming, playlist track positions and profile reuse."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from music_assistant_models.enums import ContentType

from music_assistant.providers.zvuk_music.api_client import ZvukMusicClient
from music_assistant.providers.zvuk_music.provider import ZvukMusicProvider

FLAC_URL = "https://cdn-progressive.zvuk.com/track-abc-flac-v4.mp4?expires=1&md5=x"
HIGH_URL = "https://cdn68.zvuk.com/track/1/streamhq?sig=a"
MID_URL = "https://cdn67.zvuk.com/track/1/stream?sig=b"


def _stream_provider(quality_pref: str, flac: str | None, high: str | None = HIGH_URL) -> Any:
    """
    Create a provider whose GraphQL stream lookup returns the given URLs.

    :param quality_pref: Quality preference ("lossless" or "high").
    :param flac: URL in the GraphQL ``flac`` field.
    :param high: URL in the GraphQL ``high`` field.
    """
    provider = MagicMock(spec=ZvukMusicProvider)
    provider.config = MagicMock()
    provider.config.get_value = MagicMock(return_value=quality_pref)
    provider.instance_id = "zvuk_music--test"
    provider.logger = MagicMock()
    client = MagicMock(spec=ZvukMusicClient)
    track = MagicMock()
    track.duration = 306
    track.has_flac = flac is not None
    client.get_track = AsyncMock(return_value=track)
    client.get_stream_urls = AsyncMock(return_value=[MagicMock(flac=flac, high=high, mid=MID_URL)])
    provider.client = client
    return provider


class TestFlacViaGraphql:
    """Lossless playback uses the GraphQL FLAC URL."""

    @pytest.mark.asyncio
    async def test_lossless_uses_graphql_flac_in_mp4(self) -> None:
        """FLAC comes from GraphQL and is declared as FLAC inside an MP4 container."""
        provider = _stream_provider("lossless", FLAC_URL)

        result = await ZvukMusicProvider.get_stream_details(provider, "1")

        assert result.path == FLAC_URL
        assert result.audio_format.content_type == ContentType.MP4
        assert result.audio_format.codec_type == ContentType.FLAC
        provider.client.get_stream_urls.assert_awaited_once_with("1")

    @pytest.mark.asyncio
    async def test_lossless_without_flac_uses_mp3_320(self) -> None:
        """Tracks without FLAC fall back to MP3 320."""
        provider = _stream_provider("lossless", None)

        result = await ZvukMusicProvider.get_stream_details(provider, "1")

        assert result.path == HIGH_URL
        assert result.audio_format.content_type == ContentType.MP3
        assert result.audio_format.bit_rate == 320

    @pytest.mark.asyncio
    async def test_high_preference_ignores_flac(self) -> None:
        """The high preference streams MP3 320 even when FLAC is offered."""
        provider = _stream_provider("high", FLAC_URL)

        result = await ZvukMusicProvider.get_stream_details(provider, "1")

        assert result.path == HIGH_URL
        assert result.audio_format.content_type == ContentType.MP3


def _playlist_provider(track_ids: list[str]) -> Any:
    """
    Create a provider whose playlist holds the given track IDs in order.

    :param track_ids: Playlist track IDs in playlist order.
    """
    provider = Mock(spec=ZvukMusicProvider)
    provider.logger = Mock()
    provider.instance_id = "zvuk_music--test"
    provider.domain = "zvuk_music"
    provider.client = Mock()
    provider.client.user_id = "1"
    playlist = Mock()
    playlist.tracks = [Mock(id=tid) for tid in track_ids]
    provider.client.get_playlist = AsyncMock(return_value=playlist)

    async def get_tracks(ids: list[str]) -> list[Mock]:
        # The API does not guarantee order; return them reversed.
        return [Mock(id=tid) for tid in reversed(ids)]

    provider.client.get_tracks = AsyncMock(side_effect=get_tracks)
    provider.client.update_playlist = AsyncMock(return_value=True)
    provider._get_playlist_track_ids = ZvukMusicProvider._get_playlist_track_ids.__get__(
        provider, ZvukMusicProvider
    )
    return provider


# get_playlist_tracks is wrapped by MA's cache decorator, which needs a running mass.
_get_playlist_tracks = ZvukMusicProvider.get_playlist_tracks.__wrapped__  # type: ignore[attr-defined]


def _fake_parse_track(_provider: Any, track_obj: Any) -> Mock:
    parsed = Mock()
    parsed.item_id = str(track_obj.id)
    parsed.position = None
    return parsed


class TestPlaylistTrackPositions:
    """Playlist tracks carry 1-based positions in playlist order."""

    @pytest.mark.asyncio
    async def test_first_page_positions_follow_playlist_order(self) -> None:
        """Positions start at 1 and follow the playlist, not the API batch order."""
        provider = _playlist_provider(["10", "20", "30"])

        with patch("music_assistant.providers.zvuk_music.provider.parse_track", _fake_parse_track):
            tracks = await _get_playlist_tracks(provider, "pl", page=0)

        assert [(t.item_id, t.position) for t in tracks] == [("10", 1), ("20", 2), ("30", 3)]

    @pytest.mark.asyncio
    async def test_second_page_continues_positions(self) -> None:
        """The second page starts at position 51."""
        ids = [str(i) for i in range(1, 76)]
        provider = _playlist_provider(ids)

        with patch("music_assistant.providers.zvuk_music.provider.parse_track", _fake_parse_track):
            tracks = await _get_playlist_tracks(provider, "pl", page=1)

        assert tracks[0].item_id == "51"
        assert tracks[0].position == 51
        assert tracks[-1].position == 75

    @pytest.mark.asyncio
    async def test_page_past_end_is_empty(self) -> None:
        """A page beyond the playlist returns no tracks."""
        provider = _playlist_provider(["10", "20"])

        tracks = await _get_playlist_tracks(provider, "pl", page=1)

        assert tracks == []

    @pytest.mark.asyncio
    async def test_remove_uses_one_based_positions(self) -> None:
        """Removing positions 1 and 3 drops the first and third tracks."""
        provider = _playlist_provider(["10", "20", "30", "40"])

        await ZvukMusicProvider.remove_playlist_tracks(provider, "pl", (1, 3))

        provider.client.update_playlist.assert_awaited_once_with("pl", ["20", "40"])


class TestConnectReusesProfile:
    """connect() reads the profile loaded during init()."""

    @pytest.mark.asyncio
    async def test_connect_uses_loaded_profile(self) -> None:
        """No second profile request is made after init()."""
        client = ZvukMusicClient(token="valid")
        inner = MagicMock()
        inner.init = AsyncMock(return_value=inner)
        inner.is_authorized = AsyncMock(return_value=True)
        inner.profile = Mock(id=777)
        inner.get_profile = AsyncMock()

        with patch(
            "music_assistant.providers.zvuk_music.api_client.ClientAsync",
            return_value=inner,
        ):
            await client.connect()

        assert client.user_id == "777"
        inner.get_profile.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_connect_without_profile_leaves_user_id_unknown(self) -> None:
        """A blocked profile leaves the user ID unknown."""
        client = ZvukMusicClient(token="valid")
        inner = MagicMock()
        inner.init = AsyncMock(return_value=inner)
        inner.is_authorized = AsyncMock(return_value=True)
        inner.profile = None

        with patch(
            "music_assistant.providers.zvuk_music.api_client.ClientAsync",
            return_value=inner,
        ):
            await client.connect()

        assert client.user_id is None
