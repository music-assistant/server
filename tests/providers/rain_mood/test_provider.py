"""Tests for the Rainy Mood provider."""

from unittest.mock import MagicMock, patch

import pytest
from music_assistant_models.enums import MediaType, ProviderFeature, StreamType
from music_assistant_models.errors import MediaNotFoundError

from music_assistant.providers import rain_mood
from music_assistant.providers.rain_mood import RAIN_ITEM_ID, RAIN_URL, RainyMoodProvider


def _create_provider() -> RainyMoodProvider:
    """Create a RainyMoodProvider with mocked dependencies."""
    with patch.object(RainyMoodProvider, "__init__", lambda *_a, **_kw: None):
        provider = RainyMoodProvider.__new__(RainyMoodProvider)

    provider.config = MagicMock()
    provider.config.instance_id = "rain_mood"
    provider.manifest = MagicMock()
    provider.manifest.domain = "rain_mood"
    provider.mass = MagicMock()
    provider.logger = MagicMock()

    return provider


async def test_sound_effects_enumeration() -> None:
    """The provider offers a single rain sound effect item."""
    provider = _create_provider()
    items = [item async for item in provider.get_sound_effects()]
    assert len(items) == 1
    item = items[0]
    assert item.item_id == RAIN_ITEM_ID
    assert item.media_type == MediaType.SOUND_EFFECT
    assert item.name == "Rain"
    assert item.translation_key == RAIN_ITEM_ID
    assert item.metadata.description


async def test_get_sound_effect() -> None:
    """The rain effect resolves by id; unknown ids raise MediaNotFoundError."""
    provider = _create_provider()
    item = await provider.get_sound_effect(RAIN_ITEM_ID)
    assert item.name == "Rain"
    assert item.provider_mappings
    with pytest.raises(MediaNotFoundError):
        await provider.get_sound_effect("unknown")


async def test_supported_features() -> None:
    """The provider advertises sound effects and browse, no library features."""
    assert ProviderFeature.SOUND_EFFECTS in rain_mood.SUPPORTED_FEATURES
    assert ProviderFeature.BROWSE in rain_mood.SUPPORTED_FEATURES
    assert not any(x for x in rain_mood.SUPPORTED_FEATURES if x.value.startswith("library_"))


async def test_stream_details() -> None:
    """Streamdetails point at the remote rain stream over HTTP."""
    provider = _create_provider()
    stream_details = await provider.get_stream_details(RAIN_ITEM_ID)
    assert stream_details.stream_type == StreamType.HTTP
    assert stream_details.media_type == MediaType.SOUND_EFFECT
    assert stream_details.path == RAIN_URL


async def test_stream_details_unknown() -> None:
    """Streamdetails for an unknown id raise MediaNotFoundError."""
    provider = _create_provider()
    with pytest.raises(MediaNotFoundError):
        await provider.get_stream_details("unknown")
