"""Tests that Collection folder renders audiobooks/podcasts sub-folders."""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from music_assistant_models.enums import ProviderFeature
from music_assistant_models.media_items import BrowseFolder

from music_assistant.providers.yandex_music.constants import BROWSE_NAMES_EN, BROWSE_NAMES_RU
from music_assistant.providers.yandex_music.provider import YandexMusicProvider


def _make_provider_mock(features: set[ProviderFeature], *, locale: str = "en_US") -> Mock:
    provider = Mock(spec=YandexMusicProvider)
    provider.instance_id = "yandex_music_instance"
    provider.domain = "yandex_music"
    provider.supported_features = features
    provider.mass = Mock()
    provider.mass.metadata = Mock()
    provider.mass.metadata.locale = locale
    # real method so locale mapping runs
    provider._get_browse_names = YandexMusicProvider._get_browse_names.__get__(
        provider, YandexMusicProvider
    )
    provider.logger = Mock()
    return provider


@pytest.mark.asyncio
async def test_collection_shows_audiobooks_folder_when_feature_enabled() -> None:
    """LIBRARY_AUDIOBOOKS enabled → BrowseFolder for audiobooks is returned."""
    features = {
        ProviderFeature.LIBRARY_TRACKS,
        ProviderFeature.LIBRARY_ALBUMS,
        ProviderFeature.LIBRARY_AUDIOBOOKS,
    }
    provider = _make_provider_mock(features)

    folders = await YandexMusicProvider._browse_collection(
        provider, "yandex_music_instance://collection"
    )

    item_ids = [f.item_id for f in folders if isinstance(f, BrowseFolder)]
    assert "audiobooks" in item_ids
    audiobook_folder = next(
        f for f in folders if isinstance(f, BrowseFolder) and f.item_id == "audiobooks"
    )
    assert audiobook_folder.is_playable is False
    assert audiobook_folder.path.endswith("audiobooks")
    assert audiobook_folder.name == BROWSE_NAMES_EN["audiobooks"]


@pytest.mark.asyncio
async def test_collection_shows_podcasts_folder_when_feature_enabled() -> None:
    """LIBRARY_PODCASTS enabled → BrowseFolder for podcasts is returned."""
    features = {
        ProviderFeature.LIBRARY_TRACKS,
        ProviderFeature.LIBRARY_PODCASTS,
    }
    provider = _make_provider_mock(features)

    folders = await YandexMusicProvider._browse_collection(
        provider, "yandex_music_instance://collection"
    )

    item_ids = [f.item_id for f in folders if isinstance(f, BrowseFolder)]
    assert "podcasts" in item_ids


@pytest.mark.asyncio
async def test_collection_hides_audiobooks_folder_when_feature_disabled() -> None:
    """Disabling LIBRARY_AUDIOBOOKS removes the folder from Collection."""
    features = {
        ProviderFeature.LIBRARY_TRACKS,
        ProviderFeature.LIBRARY_ALBUMS,
    }
    provider = _make_provider_mock(features)

    folders = await YandexMusicProvider._browse_collection(
        provider, "yandex_music_instance://collection"
    )

    item_ids = [f.item_id for f in folders if isinstance(f, BrowseFolder)]
    assert "audiobooks" not in item_ids
    assert "podcasts" not in item_ids


@pytest.mark.asyncio
async def test_collection_audiobooks_folder_russian_locale() -> None:
    """Russian locale uses Russian folder names."""
    features = {
        ProviderFeature.LIBRARY_AUDIOBOOKS,
        ProviderFeature.LIBRARY_PODCASTS,
    }
    provider = _make_provider_mock(features, locale="ru_RU")

    folders = await YandexMusicProvider._browse_collection(
        provider, "yandex_music_instance://collection"
    )

    audiobook = next(
        f for f in folders if isinstance(f, BrowseFolder) and f.item_id == "audiobooks"
    )
    podcast = next(f for f in folders if isinstance(f, BrowseFolder) and f.item_id == "podcasts")
    assert audiobook.name == BROWSE_NAMES_RU["audiobooks"]
    assert podcast.name == BROWSE_NAMES_RU["podcasts"]
