"""Tests for ZvukMusicProvider browse, recommendations, and playlist management."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.enums import ProviderFeature
from music_assistant_models.media_items import BrowseFolder, Playlist, RecommendationFolder

from music_assistant.providers.zvuk_music.constants import PLAYLIST_TRACK_FETCH_LIMIT
from music_assistant.providers.zvuk_music.provider import ZvukMusicProvider

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_playlist(item_id: str = "1") -> Playlist:
    """Return a minimal Playlist mock."""
    pl = Mock(spec=Playlist)
    pl.item_id = item_id
    return pl


def _make_track_mock(track_id: int) -> Mock:
    """Return a minimal track mock with .id attribute."""
    t = Mock()
    t.id = track_id
    return t


# ---------------------------------------------------------------------------
# Shared provider factory
# ---------------------------------------------------------------------------


def _make_provider() -> ZvukMusicProvider:
    """Create a ZvukMusicProvider mock with client and helpers bound."""
    provider = Mock(spec=ZvukMusicProvider)
    provider.client = Mock()
    provider.logger = Mock()
    provider.instance_id = "zvuk_music"
    provider.supported_features = frozenset({ProviderFeature.BROWSE})
    # Async helpers as mocks — overridden per-test using setattr() to satisfy mypy
    provider._get_for_you_playlists = AsyncMock(return_value=[])
    provider._get_editorial_playlists = AsyncMock(return_value=[])
    # Bind real implementations
    provider.remove_playlist_tracks = ZvukMusicProvider.remove_playlist_tracks.__get__(
        provider, ZvukMusicProvider
    )
    provider.recommendations = ZvukMusicProvider.recommendations.__get__(
        provider, ZvukMusicProvider
    )
    provider.browse = ZvukMusicProvider.browse.__get__(provider, ZvukMusicProvider)
    return provider


# ---------------------------------------------------------------------------
# Tests for remove_playlist_tracks()
# ---------------------------------------------------------------------------


class TestRemovePlaylistTracks:
    """Tests for ZvukMusicProvider.remove_playlist_tracks()."""

    @pytest.mark.asyncio
    async def test_removes_correct_positions_and_calls_update(self) -> None:
        """Tracks at specified positions are excluded; remaining IDs are passed to update."""
        provider = _make_provider()
        tracks = [
            _make_track_mock(10),
            _make_track_mock(20),
            _make_track_mock(30),
            _make_track_mock(40),
        ]
        provider.client.get_playlist_tracks = AsyncMock(return_value=tracks)
        provider.client.update_playlist = AsyncMock()

        # Remove positions 0 and 2 → keep tracks at positions 1 (id=20) and 3 (id=40)
        await provider.remove_playlist_tracks("playlist-1", (0, 2))

        provider.client.update_playlist.assert_awaited_once_with("playlist-1", ["20", "40"])

    @pytest.mark.asyncio
    async def test_get_playlist_tracks_called_with_fetch_limit(self) -> None:
        """get_playlist_tracks is always called with limit=PLAYLIST_TRACK_FETCH_LIMIT."""
        provider = _make_provider()
        provider.client.get_playlist_tracks = AsyncMock(return_value=[])
        provider.client.update_playlist = AsyncMock()

        await provider.remove_playlist_tracks("playlist-2", ())

        provider.client.get_playlist_tracks.assert_awaited_once_with(
            "playlist-2", limit=PLAYLIST_TRACK_FETCH_LIMIT
        )

    @pytest.mark.asyncio
    async def test_no_positions_keeps_all_tracks(self) -> None:
        """Removing no positions keeps all track IDs intact."""
        provider = _make_provider()
        tracks = [_make_track_mock(11), _make_track_mock(22)]
        provider.client.get_playlist_tracks = AsyncMock(return_value=tracks)
        provider.client.update_playlist = AsyncMock()

        await provider.remove_playlist_tracks("playlist-3", ())

        provider.client.update_playlist.assert_awaited_once_with("playlist-3", ["11", "22"])


# ---------------------------------------------------------------------------
# Tests for recommendations()
# ---------------------------------------------------------------------------


class TestRecommendations:
    """Tests for ZvukMusicProvider.recommendations()."""

    @pytest.mark.asyncio
    async def test_returns_two_folders_when_both_helpers_have_items(self) -> None:
        """Two RecommendationFolders are returned when both helpers return playlists."""
        provider = _make_provider()
        provider._get_for_you_playlists = AsyncMock(return_value=[_make_playlist("3")])
        provider._get_editorial_playlists = AsyncMock(return_value=[_make_playlist("99")])

        folders = await provider.recommendations()

        assert len(folders) == 2
        assert all(isinstance(f, RecommendationFolder) for f in folders)
        assert folders[0].item_id == "for_you"
        assert folders[1].item_id == "editorial"

    @pytest.mark.asyncio
    async def test_omits_for_you_folder_when_helper_returns_empty(self) -> None:
        """The «Плейлисты для вас» folder is omitted when _get_for_you_playlists returns []."""
        provider = _make_provider()
        provider._get_for_you_playlists = AsyncMock(return_value=[])
        provider._get_editorial_playlists = AsyncMock(return_value=[_make_playlist("99")])

        folders = await provider.recommendations()

        assert len(folders) == 1
        assert folders[0].item_id == "editorial"

    @pytest.mark.asyncio
    async def test_omits_editorial_folder_when_helper_returns_empty(self) -> None:
        """The «Подборки» folder is omitted when _get_editorial_playlists returns []."""
        provider = _make_provider()
        provider._get_for_you_playlists = AsyncMock(return_value=[_make_playlist("3")])
        provider._get_editorial_playlists = AsyncMock(return_value=[])

        folders = await provider.recommendations()

        assert len(folders) == 1
        assert folders[0].item_id == "for_you"

    @pytest.mark.asyncio
    async def test_returns_empty_when_both_helpers_return_empty(self) -> None:
        """An empty list is returned when both helpers have no playlists."""
        provider = _make_provider()
        provider._get_for_you_playlists = AsyncMock(return_value=[])
        provider._get_editorial_playlists = AsyncMock(return_value=[])

        folders = await provider.recommendations()

        assert folders == []


# ---------------------------------------------------------------------------
# Tests for browse()
# ---------------------------------------------------------------------------


class TestBrowse:
    """Tests for ZvukMusicProvider.browse()."""

    @pytest.mark.asyncio
    async def test_root_path_returns_two_browse_folders(self) -> None:
        """Root path returns exactly two BrowseFolder items."""
        provider = _make_provider()

        result = await provider.browse("zvuk_music://")

        assert len(result) == 2
        assert all(isinstance(f, BrowseFolder) for f in result)
        assert result[0].item_id == "for_you"
        assert result[1].item_id == "editorial"

    @pytest.mark.asyncio
    async def test_for_you_subpath_returns_for_you_playlists(self) -> None:
        """Path ending with 'for_you' returns the for-you playlist list."""
        provider = _make_provider()
        pl_1, pl_2 = _make_playlist("3"), _make_playlist("4")
        for_you_mock = AsyncMock(return_value=[pl_1, pl_2])
        editorial_mock = AsyncMock(return_value=[])
        provider._get_for_you_playlists = for_you_mock
        provider._get_editorial_playlists = editorial_mock

        result = await provider.browse("zvuk_music://for_you")

        assert result == [pl_1, pl_2]
        editorial_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_editorial_subpath_returns_editorial_playlists(self) -> None:
        """Path ending with 'editorial' returns the editorial playlist list."""
        provider = _make_provider()
        pl = _make_playlist("101")
        for_you_mock = AsyncMock(return_value=[])
        editorial_mock = AsyncMock(return_value=[pl])
        provider._get_for_you_playlists = for_you_mock
        provider._get_editorial_playlists = editorial_mock

        result = await provider.browse("zvuk_music://editorial")

        assert result == [pl]
        for_you_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_browse_folder_paths_are_correct(self) -> None:
        """Root BrowseFolders have paths pointing into the provider namespace."""
        provider = _make_provider()

        result = await provider.browse("zvuk_music://")

        for_you_folder = cast("BrowseFolder", result[0])
        editorial_folder = cast("BrowseFolder", result[1])
        assert for_you_folder.path == "zvuk_music://for_you"
        assert editorial_folder.path == "zvuk_music://editorial"
