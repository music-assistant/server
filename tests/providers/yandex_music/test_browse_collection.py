"""Tests that Collection folder renders audiobooks/podcasts sub-folders."""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest
from music_assistant_models.enums import ProviderFeature
from music_assistant_models.media_items import BrowseFolder

from music_assistant.providers.yandex_music.provider import YandexMusicProvider

from .conftest import provider_dir

_STRINGS = json.loads((provider_dir() / "strings.json").read_text(encoding="utf-8"))


def _make_provider_mock(features: set[ProviderFeature]) -> Mock:
    provider = Mock(spec=YandexMusicProvider)
    provider.instance_id = "yandex_music_instance"
    provider.domain = "yandex_music"
    provider.supported_features = features
    provider.mass = Mock()
    # Resolve authored names from the real strings.json, like the MA
    # translations controller would for the English source.
    provider.mass.translations.get_translation.side_effect = lambda key: _lookup_strings_key(key)
    # real method so the strings.json lookup path runs
    provider._media_label = YandexMusicProvider._media_label.__get__(provider, YandexMusicProvider)
    provider.logger = Mock()
    return provider


def _lookup_strings_key(key: str) -> str | None:
    """Resolve provider.yandex_music.media.<group>.<slug>.name against strings.json."""
    parts = key.split(".")
    node: object = _STRINGS
    for part in parts[2:]:  # skip "music_assistant.providers.yandex_music.yandex_music"
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node if isinstance(node, str) else None


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
    assert audiobook_folder.name == _STRINGS["media"]["folder"]["my_audiobooks"]["name"]


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
async def test_collection_folders_carry_authored_translation_keys() -> None:
    """
    Collection folders localize via translation keys authored in strings.json.

    Localization happens at API serialization (per connection locale), so
    the provider must emit an English fallback name plus a key that exists
    under ``media.folder`` in strings.json.
    """
    features = {
        ProviderFeature.LIBRARY_AUDIOBOOKS,
        ProviderFeature.LIBRARY_PODCASTS,
    }
    provider = _make_provider_mock(features)

    folders = await YandexMusicProvider._browse_collection(
        provider, "yandex_music_instance://collection"
    )

    authored = _STRINGS["media"]["folder"]
    for folder in folders:
        assert isinstance(folder, BrowseFolder)
        assert folder.translation_key is not None
        assert folder.translation_key in authored
