"""Tests for running while Zvuk's anti-bot protection blocks the tiny API."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from music_assistant_models.enums import ContentType
from music_assistant_models.errors import MediaNotFoundError, ProviderUnavailableError

from music_assistant.providers.zvuk_music.api_client import ZvukMusicClient
from music_assistant.providers.zvuk_music.parsers import parse_playlist
from music_assistant.providers.zvuk_music.provider import ZvukMusicProvider

CDN_HIGH = "https://cdn68.zvuk.com/track/1/streamhq?sig=a"
CDN_MID = "https://cdn67.zvuk.com/track/1/stream?sig=b"


def _make_stream_provider(quality_pref: str, high: str | None, mid: str | None) -> MagicMock:
    """
    Create a provider whose GraphQL stream lookup returns the given URLs.

    :param quality_pref: Quality preference ("lossless" or "high").
    :param high: URL returned in the GraphQL ``high`` field.
    :param mid: URL returned in the GraphQL ``mid`` field.
    """
    provider = MagicMock(spec=ZvukMusicProvider)
    provider.config = MagicMock()
    provider.config.get_value = MagicMock(return_value=quality_pref)
    provider.instance_id = "zvuk_music--test"
    provider.logger = MagicMock()
    client = MagicMock(spec=ZvukMusicClient)
    track = MagicMock()
    track.duration = 200
    track.has_flac = True
    client.get_track = AsyncMock(return_value=track)
    client.get_stream_urls = AsyncMock(return_value=[MagicMock(flac=None, high=high, mid=mid)])
    provider.client = client
    return provider


class TestConnectWithBlockedProfile:
    """connect() when the tiny profile endpoint is blocked."""

    @pytest.mark.asyncio
    async def test_connect_succeeds_without_user_id(self) -> None:
        """A verified token connects even though the profile cannot be fetched."""
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

    def test_user_id_is_none_before_connect(self) -> None:
        """user_id reports an unknown user instead of raising."""
        assert ZvukMusicClient(token="valid").user_id is None


class TestStreamsViaGraphql:
    """get_stream_details uses GraphQL stream URLs for MP3."""

    @pytest.mark.asyncio
    async def test_high_preference_uses_graphql_high(self) -> None:
        """MP3 320 comes from GraphQL without touching the tiny stream endpoint."""
        provider = _make_stream_provider("high", CDN_HIGH, CDN_MID)

        result = await ZvukMusicProvider.get_stream_details(provider, "1")

        assert result.path == CDN_HIGH
        assert result.audio_format.content_type == ContentType.MP3
        assert result.audio_format.bit_rate == 320

    @pytest.mark.asyncio
    async def test_graphql_mid_when_high_missing(self) -> None:
        """MP3 128 from GraphQL is used when no high-quality URL is offered."""
        provider = _make_stream_provider("high", None, CDN_MID)

        result = await ZvukMusicProvider.get_stream_details(provider, "1")

        assert result.path == CDN_MID
        assert result.audio_format.bit_rate == 128

    @pytest.mark.asyncio
    async def test_lossless_without_flac_falls_back_to_graphql_high(self) -> None:
        """Lossless falls back to GraphQL MP3 320 when no FLAC URL is offered."""
        provider = _make_stream_provider("lossless", CDN_HIGH, CDN_MID)

        result = await ZvukMusicProvider.get_stream_details(provider, "1")

        assert result.path == CDN_HIGH
        assert result.audio_format.content_type == ContentType.MP3

    @pytest.mark.asyncio
    async def test_raises_when_no_stream_anywhere(self) -> None:
        """MediaNotFoundError when neither FLAC nor GraphQL MP3 is available."""
        provider = _make_stream_provider("lossless", None, None)

        with pytest.raises(MediaNotFoundError):
            await ZvukMusicProvider.get_stream_details(provider, "1")


class TestPlaylistEditableWithoutUserId:
    """Playlist ownership when the user id is unknown."""

    def _provider(self, collection_ids: set[str]) -> Mock:
        provider = Mock()
        provider.instance_id = "zvuk_music_test"
        provider.domain = "zvuk_music"
        provider.client = Mock()
        provider.client.user_id = None
        provider.client.collection_playlist_ids = collection_ids
        return provider

    def _playlist(self, playlist_id: int) -> Mock:
        playlist = Mock()
        playlist.id = playlist_id
        playlist.title = "List"
        playlist.user_id = 555
        playlist.description = None
        playlist.image = None
        playlist.duration = None
        return playlist

    def test_collection_playlist_is_editable(self) -> None:
        """A playlist from the user's collection is editable."""
        result = parse_playlist(self._provider({"10"}), self._playlist(10))

        assert result.is_editable is True
        assert result.owner == "Me"

    def test_foreign_playlist_is_not_editable(self) -> None:
        """A playlist outside the user's collection stays read-only."""
        result = parse_playlist(self._provider({"10"}), self._playlist(20))

        assert result.is_editable is False


class TestUserPlaylistsRecordCollection:
    """get_user_playlists remembers which playlists are in the collection."""

    @pytest.mark.asyncio
    async def test_records_collection_playlist_ids(self) -> None:
        """Collection playlist ids are stored for ownership checks."""
        client = ZvukMusicClient(token="valid")
        inner = MagicMock()
        inner.get_user_playlists = AsyncMock(return_value=[Mock(id="10"), Mock(id=11)])
        client._client = inner

        await client.get_user_playlists()

        assert client.collection_playlist_ids == {"10", "11"}


class TestEditorialBlocked:
    """Editorial recommendations when the grid endpoint is blocked."""

    @pytest.mark.asyncio
    async def test_editorial_returns_empty_when_blocked(self) -> None:
        """A blocked grid endpoint yields no editorial playlists instead of failing."""
        provider = MagicMock(spec=ZvukMusicProvider)
        provider.logger = MagicMock()
        provider.client = MagicMock(spec=ZvukMusicClient)
        provider.client.get_editorial_playlist_ids = AsyncMock(
            side_effect=ProviderUnavailableError("Bot detected by Zvuk")
        )

        result = await ZvukMusicProvider._get_editorial_playlists(provider)

        assert result == []
