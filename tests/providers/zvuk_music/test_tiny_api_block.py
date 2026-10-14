"""Tests for running while Zvuk's anti-bot protection blocks the tiny API."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from music_assistant_models.errors import ProviderUnavailableError

from music_assistant.providers.zvuk_music.api_client import ZvukMusicClient
from music_assistant.providers.zvuk_music.parsers import parse_playlist
from music_assistant.providers.zvuk_music.provider import ZvukMusicProvider


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


class TestPlaylistEditableWithoutUserId:
    """Playlist ownership when the user id is unknown."""

    def _provider(self) -> Mock:
        provider = Mock()
        provider.instance_id = "zvuk_music_test"
        provider.domain = "zvuk_music"
        provider.client = Mock()
        provider.client.user_id = None
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

    def test_playlist_is_read_only_without_user_id(self) -> None:
        """Without a known user ID no playlist is treated as owned, even a followed one."""
        result = parse_playlist(self._provider(), self._playlist(10))

        assert result.is_editable is False
        assert result.owner == "Zvuk Music"


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
