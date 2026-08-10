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
from music_assistant.providers.filesystem_local.helpers import FileSystemItem


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
            "music_assistant.providers.filesystem_local.async_parse_tags",
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
            "music_assistant.providers.filesystem_local.async_parse_tags",
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
            "music_assistant.providers.filesystem_local.async_parse_tags",
            new_callable=AsyncMock,
        ):
            result = await provider._process_item_async(item, None)

        assert result is True
        provider.mass.music.podcasts.add_item_to_library.assert_called_once()


def _classify(
    provider: LocalFileSystemProvider,
    relative_path: str,
    file_checksums: dict[str, str] | None = None,
) -> tuple[list[tuple[FileSystemItem, str | None]], set[str]]:
    """Run a single file through _classify_scan_item and return its scan buckets."""
    item = FileSystemItem(
        filename=relative_path.rsplit("/", 1)[-1],
        relative_path=relative_path,
        absolute_path=f"/media/{relative_path}",
        is_dir=False,
        checksum="1",
    )
    items_to_process: list[tuple[FileSystemItem, str | None]] = []
    cur_filenames: set[str] = set()
    provider._classify_scan_item(
        item,
        file_checksums=file_checksums or {},
        cue_file_checksums={},
        cur_filenames=cur_filenames,
        items_to_process=items_to_process,
        unchanged_cue_items=[],
        cue_stems=set(),
        ignore_album_playlists=False,
    )
    return items_to_process, cur_filenames


class TestClassifyScanItemSkipsUnimportedFiles:
    """
    The scan must only queue files this provider actually imports.

    The walker looks for the union of every content type's extensions, so a file
    that this content type never imports gets no provider mapping and would be
    queued again on every single sync.
    """

    def test_music_skips_audiobook_only_extension(self) -> None:
        """An .m4b in a music library is never imported, so it must not be queued."""
        provider = _create_provider(content_type="music")
        items, _ = _classify(provider, "Boeken/luisterboek.m4b")
        assert items == []

    def test_skipped_file_still_counts_as_present(self) -> None:
        """A skipped file is still on disk, so the deletion pass must not claim it."""
        provider = _create_provider(content_type="music")
        _, cur_filenames = _classify(provider, "Boeken/luisterboek.m4b")
        assert cur_filenames == {"Boeken/luisterboek.m4b"}

    def test_indexed_track_survives_disabling_track_sync(self) -> None:
        """
        Turning off track sync must not delete the tracks already in the library.

        The deletion pass removes everything the scan did not report as present.
        """
        provider = _create_provider(content_type="music", sync_tracks=False)
        path = "Artist/Album/track.mp3"
        items, cur_filenames = _classify(provider, path, file_checksums={path: "1"})
        assert items == []
        assert cur_filenames == {path}

    def test_indexed_playlist_survives_disabling_playlist_sync(self) -> None:
        """Turning off playlist sync must not delete the playlists already in the library."""
        provider = _create_provider(content_type="music", sync_playlists=False)
        path = "playlists/favorites.m3u"
        items, cur_filenames = _classify(provider, path, file_checksums={path: "1"})
        assert items == []
        assert cur_filenames == {path}

    def test_music_queues_track(self) -> None:
        """A regular track file is still queued."""
        provider = _create_provider(content_type="music")
        items, _ = _classify(provider, "Artist/Album/track.mp3")
        assert [item.relative_path for item, _ in items] == ["Artist/Album/track.mp3"]

    def test_music_queues_cue(self) -> None:
        """A CUE sheet is still queued."""
        provider = _create_provider(content_type="music")
        items, _ = _classify(provider, "Artist/Album/album.cue")
        assert [item.relative_path for item, _ in items] == ["Artist/Album/album.cue"]

    def test_music_skips_tracks_when_sync_disabled(self) -> None:
        """Track files are not queued when track sync is off."""
        provider = _create_provider(content_type="music", sync_tracks=False)
        items, _ = _classify(provider, "Artist/Album/track.mp3")
        assert items == []

    def test_music_skips_playlists_when_sync_disabled(self) -> None:
        """Playlist files are not queued when playlist sync is off."""
        provider = _create_provider(content_type="music", sync_playlists=False)
        items, _ = _classify(provider, "playlists/favorites.m3u")
        assert items == []

    def test_audiobooks_skips_track_only_extension(self) -> None:
        """A .wav in an audiobooks library is never imported, so it must not be queued."""
        provider = _create_provider(content_type="audiobooks")
        items, _ = _classify(provider, "Author/Book/chapter.wav")
        assert items == []

    def test_audiobooks_skips_playlist(self) -> None:
        """A playlist in an audiobooks library is never imported, so it must not be queued."""
        provider = _create_provider(content_type="audiobooks")
        items, _ = _classify(provider, "Author/Book/book.m3u")
        assert items == []

    def test_podcasts_skips_cue(self) -> None:
        """A CUE sheet in a podcasts library is never imported, so it must not be queued."""
        provider = _create_provider(content_type="podcasts")
        items, _ = _classify(provider, "Podcast/episode.cue")
        assert items == []

    def test_podcasts_queues_episode(self) -> None:
        """A regular episode file is still queued."""
        provider = _create_provider(content_type="podcasts")
        items, _ = _classify(provider, "Podcast/episode01.mp3")
        assert [item.relative_path for item, _ in items] == ["Podcast/episode01.mp3"]
