"""Tests for filesystem provider sync configuration behavior."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.media_items import Podcast

from music_assistant.providers.filesystem_local import LocalFileSystemProvider
from music_assistant.providers.filesystem_local.constants import (
    AUDIOBOOK_EXTENSIONS,
    CONF_ENTRY_CONTENT_TYPE,
    CONF_ENTRY_LIBRARY_SYNC_AUDIOBOOKS,
    CONF_ENTRY_LIBRARY_SYNC_PLAYLISTS,
    CONF_ENTRY_LIBRARY_SYNC_PODCASTS,
    CONF_ENTRY_LIBRARY_SYNC_TRACKS,
    PODCAST_EPISODE_EXTENSIONS,
    TRACK_EXTENSIONS,
)


def _create_provider(
    content_type: str = "music",
    sync_tracks: bool = True,
    sync_playlists: bool = True,
    sync_audiobooks: bool = True,
    sync_podcasts: bool = True,
) -> LocalFileSystemProvider:
    """
    Create a LocalFileSystemProvider with mocked dependencies.

    :param content_type: The media content type ("music", "audiobooks", "podcasts").
    :param sync_tracks: Whether the tracks sync checkbox is enabled.
    :param sync_playlists: Whether the playlists sync checkbox is enabled.
    :param sync_audiobooks: Whether the audiobooks sync checkbox is enabled.
    :param sync_podcasts: Whether the podcasts sync checkbox is enabled.
    """
    config_values = {
        CONF_ENTRY_CONTENT_TYPE.key: content_type,
        CONF_ENTRY_LIBRARY_SYNC_TRACKS.key: sync_tracks,
        CONF_ENTRY_LIBRARY_SYNC_PLAYLISTS.key: sync_playlists,
        CONF_ENTRY_LIBRARY_SYNC_AUDIOBOOKS.key: sync_audiobooks,
        CONF_ENTRY_LIBRARY_SYNC_PODCASTS.key: sync_podcasts,
    }

    mock_config = MagicMock()
    mock_config.get_value = MagicMock(side_effect=lambda key: config_values.get(key))

    with patch.object(LocalFileSystemProvider, "__init__", lambda *_a, **_kw: None):
        provider = LocalFileSystemProvider.__new__(LocalFileSystemProvider)

    provider.config = mock_config
    provider.media_content_type = content_type
    provider.write_access = False
    provider._sync_tracks = sync_tracks
    provider._sync_playlists = sync_playlists
    provider.sync_running = False
    provider.mass = MagicMock()
    provider.logger = MagicMock()

    return provider


class TestSupportedFeatures:
    """Test that supported_features reflects content type, not sync preferences."""

    def test_music_content_type(self) -> None:
        """Music content type advertises all music library features."""
        provider = _create_provider(content_type="music")
        features = provider.supported_features
        assert ProviderFeature.LIBRARY_TRACKS in features
        assert ProviderFeature.LIBRARY_PLAYLISTS in features
        assert ProviderFeature.LIBRARY_ALBUMS in features
        assert ProviderFeature.LIBRARY_ARTISTS in features

    def test_audiobooks_content_type(self) -> None:
        """Audiobooks content type advertises audiobook library feature."""
        provider = _create_provider(content_type="audiobooks")
        assert ProviderFeature.LIBRARY_AUDIOBOOKS in provider.supported_features

    def test_podcasts_content_type(self) -> None:
        """Podcasts content type advertises podcast library feature."""
        provider = _create_provider(content_type="podcasts")
        assert ProviderFeature.LIBRARY_PODCASTS in provider.supported_features


class TestSyncLibraryEarlyReturn:
    """Test that sync_library returns early when all sync options are disabled."""

    @pytest.mark.asyncio
    async def test_music_returns_early_when_all_disabled(self) -> None:
        """Music provider returns early when both tracks and playlists sync are disabled."""
        provider = _create_provider(sync_tracks=False, sync_playlists=False)
        await provider.sync_library(MediaType.TRACK)
        provider.mass.music.database.get_rows_from_query.assert_not_called()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_audiobooks_returns_early_when_disabled(self) -> None:
        """Audiobooks provider returns early when audiobook sync is disabled."""
        provider = _create_provider(content_type="audiobooks", sync_audiobooks=False)
        await provider.sync_library(MediaType.AUDIOBOOK)
        provider.mass.music.database.get_rows_from_query.assert_not_called()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_podcasts_returns_early_when_disabled(self) -> None:
        """Podcasts provider returns early when podcast sync is disabled."""
        provider = _create_provider(content_type="podcasts", sync_podcasts=False)
        await provider.sync_library(MediaType.PODCAST)
        provider.mass.music.database.get_rows_from_query.assert_not_called()  # type: ignore[attr-defined]


class TestProcessItemRespectsConfig:
    """Test that _process_item_async skips items when sync is disabled."""

    @pytest.mark.asyncio
    async def test_tracks_skipped_when_sync_disabled(self) -> None:
        """Track files are not imported when track sync is disabled."""
        provider = _create_provider(sync_tracks=False)
        item = MagicMock()
        item.ext = next(iter(TRACK_EXTENSIONS))
        item.relative_path = "Artist/Album/track.mp3"

        result = await provider._process_item_async(item, None)

        assert result is False
        provider.mass.music.tracks.add_item_to_library.assert_not_called()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_tracks_imported_when_sync_enabled(self) -> None:
        """Track files are imported when track sync is enabled."""
        provider = _create_provider(sync_tracks=True)
        item = MagicMock()
        item.ext = next(iter(TRACK_EXTENSIONS))
        item.absolute_path = "/media/Artist/Album/track.mp3"
        item.relative_path = "Artist/Album/track.mp3"
        item.file_size = 1000

        mock_track = MagicMock()
        provider._parse_track = AsyncMock(return_value=mock_track)  # type: ignore[method-assign]
        provider.mass.music.tracks.add_item_to_library = AsyncMock()  # type: ignore[method-assign,misc]

        with patch(
            "music_assistant.providers.filesystem_local.base.async_parse_tags",
            new_callable=AsyncMock,
        ):
            result = await provider._process_item_async(item, None)

        assert result is True
        provider.mass.music.tracks.add_item_to_library.assert_called_once()

    @pytest.mark.asyncio
    async def test_playlists_skipped_when_sync_disabled(self) -> None:
        """Playlist files are not imported when playlist sync is disabled."""
        provider = _create_provider(sync_playlists=False)
        item = MagicMock()
        item.ext = "m3u"
        item.relative_path = "playlists/favorites.m3u"

        result = await provider._process_item_async(item, None)

        assert result is False
        provider.mass.music.playlists.add_item_to_library.assert_not_called()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_playlists_imported_when_sync_enabled(self) -> None:
        """Playlist files are imported when playlist sync is enabled."""
        provider = _create_provider(sync_playlists=True)
        item = MagicMock()
        item.ext = "m3u"
        item.absolute_path = "/media/playlists/favorites.m3u"
        item.relative_path = "playlists/favorites.m3u"

        mock_playlist = MagicMock()
        provider.get_playlist = AsyncMock(return_value=mock_playlist)  # type: ignore[method-assign]
        provider.mass.music.playlists.add_item_to_library = AsyncMock()  # type: ignore[method-assign,misc]

        result = await provider._process_item_async(item, None)

        assert result is True
        provider.mass.music.playlists.add_item_to_library.assert_called_once()

    @pytest.mark.asyncio
    async def test_audiobooks_imported_when_sync_enabled(self) -> None:
        """Audiobook files are imported when audiobook sync is enabled."""
        provider = _create_provider(content_type="audiobooks", sync_audiobooks=True)
        item = MagicMock()
        item.ext = next(iter(AUDIOBOOK_EXTENSIONS))
        item.absolute_path = "/media/Author/Book/chapter01.m4b"
        item.relative_path = "Author/Book/chapter01.m4b"
        item.file_size = 5000

        mock_audiobook = MagicMock()
        provider._parse_audiobook = AsyncMock(return_value=mock_audiobook)  # type: ignore[method-assign]
        provider.mass.music.audiobooks.add_item_to_library = AsyncMock()  # type: ignore[method-assign,misc]

        with patch(
            "music_assistant.providers.filesystem_local.base.async_parse_tags",
            new_callable=AsyncMock,
        ):
            result = await provider._process_item_async(item, None)

        assert result is True
        provider.mass.music.audiobooks.add_item_to_library.assert_called_once()

    @pytest.mark.asyncio
    async def test_podcasts_imported_when_sync_enabled(self) -> None:
        """Podcast files are imported when podcast sync is enabled."""
        provider = _create_provider(content_type="podcasts", sync_podcasts=True)
        item = MagicMock()
        item.ext = next(iter(PODCAST_EPISODE_EXTENSIONS))
        item.absolute_path = "/media/Podcast/episode01.mp3"
        item.relative_path = "Podcast/episode01.mp3"
        item.file_size = 3000

        mock_episode = MagicMock()
        mock_episode.podcast = MagicMock(spec=Podcast)
        provider._parse_podcast_episode = AsyncMock(return_value=mock_episode)  # type: ignore[method-assign]
        provider.mass.music.podcasts.add_item_to_library = AsyncMock()  # type: ignore[method-assign,misc]

        with patch(
            "music_assistant.providers.filesystem_local.base.async_parse_tags",
            new_callable=AsyncMock,
        ):
            result = await provider._process_item_async(item, None)

        assert result is True
        provider.mass.music.podcasts.add_item_to_library.assert_called_once()
