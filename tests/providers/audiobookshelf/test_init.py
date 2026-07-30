"""Tests for Audiobookshelf provider initialization."""

from music_assistant_models.enums import ProviderFeature

from music_assistant.providers.audiobookshelf import Audiobookshelf


def test_supported_features_before_async_init(provider: Audiobookshelf) -> None:
    """Provider features are available before asynchronous initialization."""
    assert ProviderFeature.PLAYLIST_CREATE_AUDIOBOOKS in provider.supported_features
