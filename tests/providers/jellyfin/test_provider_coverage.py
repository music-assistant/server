"""Comprehensive coverage tests for the Jellyfin provider NotImplemented methods."""

from unittest import mock

import pytest
from music_assistant_models.enums import MediaType

from music_assistant.providers.jellyfin import JellyfinProvider


class TestJellyfinProviderNotImplemented:
    """Test that NotImplementedError stubs are properly implemented."""

    def _get_provider(self) -> JellyfinProvider:
        """Create a minimal provider instance for testing NotImplementedError methods."""
        provider = mock.MagicMock(spec=JellyfinProvider)
        # Set real unbound methods so they can be called with proper arguments
        provider.add_playlist_tracks = JellyfinProvider.add_playlist_tracks
        provider.remove_playlist_tracks = JellyfinProvider.remove_playlist_tracks
        provider.create_playlist = JellyfinProvider.create_playlist
        provider.get_artist_toptracks = JellyfinProvider.get_artist_toptracks
        provider.get_audiobook = JellyfinProvider.get_audiobook
        provider.get_podcast = JellyfinProvider.get_podcast
        provider.get_podcast_episode = JellyfinProvider.get_podcast_episode
        provider.get_radio = JellyfinProvider.get_radio
        provider.get_resume_position = JellyfinProvider.get_resume_position
        return provider

    @pytest.mark.asyncio
    async def test_add_playlist_tracks_not_supported(self) -> None:
        """Test that add_playlist_tracks raises NotImplementedError."""
        provider = self._get_provider()
        with pytest.raises(NotImplementedError, match="does not support adding tracks"):
            await JellyfinProvider.add_playlist_tracks(provider, "playlist-id", ["track-1"])

    @pytest.mark.asyncio
    async def test_remove_playlist_tracks_not_supported(self) -> None:
        """Test that remove_playlist_tracks raises NotImplementedError."""
        provider = self._get_provider()
        with pytest.raises(
            NotImplementedError, match="does not support removing tracks from playlists"
        ):
            await JellyfinProvider.remove_playlist_tracks(provider, "playlist-id", (0,))

    @pytest.mark.asyncio
    async def test_create_playlist_not_supported(self) -> None:
        """Test that create_playlist raises NotImplementedError."""
        provider = self._get_provider()
        with pytest.raises(NotImplementedError, match="does not support creating playlists"):
            await JellyfinProvider.create_playlist(provider, "New Playlist")

    @pytest.mark.asyncio
    async def test_get_artist_toptracks_not_supported(self) -> None:
        """Test that get_artist_toptracks raises NotImplementedError."""
        provider = self._get_provider()
        with pytest.raises(NotImplementedError, match="does not provide artist top tracks"):
            await JellyfinProvider.get_artist_toptracks(provider, "artist-id")

    @pytest.mark.asyncio
    async def test_get_audiobook_not_supported(self) -> None:
        """Test that get_audiobook raises NotImplementedError."""
        provider = self._get_provider()
        with pytest.raises(NotImplementedError, match="does not support audiobooks"):
            await JellyfinProvider.get_audiobook(provider, "audiobook-id")

    @pytest.mark.asyncio
    async def test_get_podcast_not_supported(self) -> None:
        """Test that get_podcast raises NotImplementedError."""
        provider = self._get_provider()
        with pytest.raises(NotImplementedError, match="does not support podcasts"):
            await JellyfinProvider.get_podcast(provider, "podcast-id")

    @pytest.mark.asyncio
    async def test_get_podcast_episode_not_supported(self) -> None:
        """Test that get_podcast_episode raises NotImplementedError."""
        provider = self._get_provider()
        with pytest.raises(NotImplementedError, match="does not support podcast episodes"):
            await JellyfinProvider.get_podcast_episode(provider, "episode-id")

    @pytest.mark.asyncio
    async def test_get_radio_not_supported(self) -> None:
        """Test that get_radio raises NotImplementedError."""
        provider = self._get_provider()
        with pytest.raises(NotImplementedError, match="does not support radios"):
            await JellyfinProvider.get_radio(provider, "radio-id")

    @pytest.mark.asyncio
    async def test_get_resume_position_not_supported(self) -> None:
        """Test that get_resume_position raises NotImplementedError."""
        provider = self._get_provider()
        with pytest.raises(NotImplementedError, match="does not provide resume positions"):
            await JellyfinProvider.get_resume_position(provider, "item-id", MediaType.TRACK)
