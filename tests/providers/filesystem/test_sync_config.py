"""Tests for filesystem provider sync configuration behavior."""

from unittest.mock import MagicMock, patch

from music_assistant_models.enums import ProviderFeature

from music_assistant.providers.filesystem_local import LocalFileSystemProvider
from music_assistant.providers.filesystem_local.constants import (
    CONF_ENTRY_CONTENT_TYPE,
    CONF_ENTRY_LIBRARY_SYNC_AUDIOBOOKS,
    CONF_ENTRY_LIBRARY_SYNC_PLAYLISTS,
    CONF_ENTRY_LIBRARY_SYNC_PODCASTS,
    CONF_ENTRY_LIBRARY_SYNC_TRACKS,
)

BASE_FEATURES = {ProviderFeature.BROWSE, ProviderFeature.SEARCH}


def _create_provider(
    content_type: str = "music",
    sync_tracks: bool = True,
    sync_playlists: bool = True,
    sync_audiobooks: bool = True,
    sync_podcasts: bool = True,
    write_access: bool = False,
) -> LocalFileSystemProvider:
    """Create a LocalFileSystemProvider with mocked dependencies.

    :param content_type: The media content type ("music", "audiobooks", "podcasts").
    :param sync_tracks: Whether the tracks sync checkbox is enabled.
    :param sync_playlists: Whether the playlists sync checkbox is enabled.
    :param sync_audiobooks: Whether the audiobooks sync checkbox is enabled.
    :param sync_podcasts: Whether the podcasts sync checkbox is enabled.
    :param write_access: Whether the provider has write access.
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
    provider.write_access = write_access

    return provider


class TestSupportedFeaturesMusic:
    """Test supported_features for music content type."""

    def test_all_sync_enabled(self) -> None:
        """All music sync features are present when both checkboxes are enabled."""
        provider = _create_provider(sync_tracks=True, sync_playlists=True)
        features = provider.supported_features
        assert features == {
            *BASE_FEATURES,
            ProviderFeature.LIBRARY_TRACKS,
            ProviderFeature.LIBRARY_PLAYLISTS,
        }
        assert ProviderFeature.PLAYLIST_TRACKS_EDIT not in features
        assert ProviderFeature.PLAYLIST_CREATE not in features

    def test_tracks_disabled_removes_track_feature(self) -> None:
        """Disabling tracks removes LIBRARY_TRACKS from features."""
        provider = _create_provider(sync_tracks=False, sync_playlists=True)
        features = provider.supported_features
        assert ProviderFeature.LIBRARY_TRACKS not in features
        assert ProviderFeature.LIBRARY_PLAYLISTS in features

    def test_playlists_disabled_removes_playlist_feature(self) -> None:
        """Disabling playlists removes LIBRARY_PLAYLISTS from features."""
        provider = _create_provider(sync_tracks=True, sync_playlists=False)
        features = provider.supported_features
        assert ProviderFeature.LIBRARY_TRACKS in features
        assert ProviderFeature.LIBRARY_PLAYLISTS not in features

    def test_all_sync_disabled_leaves_only_base_features(self) -> None:
        """Disabling all sync options leaves only browse and search."""
        provider = _create_provider(sync_tracks=False, sync_playlists=False)
        assert provider.supported_features == BASE_FEATURES

    def test_albums_and_artists_never_in_features(self) -> None:
        """LIBRARY_ALBUMS and LIBRARY_ARTISTS are never advertised.

        Albums and artists are always derived from track imports, not synced
        independently. Advertising these features would cause the config
        controller to inject meaningless sync checkboxes.
        """
        provider = _create_provider(sync_tracks=True, sync_playlists=True)
        features = provider.supported_features
        assert ProviderFeature.LIBRARY_ALBUMS not in features
        assert ProviderFeature.LIBRARY_ARTISTS not in features

    def test_write_access_adds_playlist_edit_features(self) -> None:
        """Write access adds playlist editing features regardless of sync config."""
        provider = _create_provider(sync_tracks=False, sync_playlists=False, write_access=True)
        features = provider.supported_features
        assert ProviderFeature.PLAYLIST_TRACKS_EDIT in features
        assert ProviderFeature.PLAYLIST_CREATE in features


class TestSupportedFeaturesAudiobooks:
    """Test supported_features for audiobooks content type."""

    def test_sync_enabled(self) -> None:
        """Audiobook feature present when sync is enabled."""
        provider = _create_provider(content_type="audiobooks", sync_audiobooks=True)
        assert ProviderFeature.LIBRARY_AUDIOBOOKS in provider.supported_features

    def test_sync_disabled(self) -> None:
        """Audiobook feature absent when sync is disabled."""
        provider = _create_provider(content_type="audiobooks", sync_audiobooks=False)
        assert provider.supported_features == BASE_FEATURES


class TestSupportedFeaturesPodcasts:
    """Test supported_features for podcasts content type."""

    def test_sync_enabled(self) -> None:
        """Podcast feature present when sync is enabled."""
        provider = _create_provider(content_type="podcasts", sync_podcasts=True)
        assert ProviderFeature.LIBRARY_PODCASTS in provider.supported_features

    def test_sync_disabled(self) -> None:
        """Podcast feature absent when sync is disabled."""
        provider = _create_provider(content_type="podcasts", sync_podcasts=False)
        assert provider.supported_features == BASE_FEATURES
