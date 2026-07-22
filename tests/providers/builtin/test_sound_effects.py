"""Tests for builtin sound effect URL handling."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import SoundEffect

from music_assistant.helpers.playlists import PlaylistItem, ProviderMappingInfo
from music_assistant.providers.builtin import BuiltinProvider


class DummyMediaInfo:
    """Minimal media info stub for builtin provider tests."""

    def __init__(
        self,
        *,
        title: str = "Intro",
        duration: int | None = 12,
        media_format: str = "mp3",
        sample_rate: int = 44100,
        bits_per_sample: int = 16,
        bit_rate: int = 192,
        channels: int = 2,
        artists: list[str] | None = None,
        has_cover_image: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Initialize the stub with builtin media-info fields."""
        self.title = title
        self.duration = duration
        self.format = media_format
        self.sample_rate = sample_rate
        self.bits_per_sample = bits_per_sample
        self.bit_rate = bit_rate
        self.channels = channels
        self.artists = artists or []
        self.has_cover_image = has_cover_image
        self._extra = extra or {}

    def get(self, key: str, default: Any = None) -> Any:
        """Return dict-like tag values used by the provider."""
        return self._extra.get(key, default)


def _make_provider() -> BuiltinProvider:
    """Return a BuiltinProvider instance with mocked collaborators."""
    provider = BuiltinProvider.__new__(BuiltinProvider)
    provider.mass = MagicMock()
    provider.logger = MagicMock()
    provider.manifest = MagicMock(domain="builtin")
    provider.config = MagicMock(instance_id="builtin_1")
    return provider


@pytest.mark.asyncio
async def test_parse_item_returns_sound_effect_when_requested() -> None:
    """Builtin direct URLs can be parsed into SoundEffect items."""
    provider = _make_provider()
    provider._get_media_info = AsyncMock(  # type: ignore[method-assign]
        return_value=DummyMediaInfo(has_cover_image=True)
    )

    result = await provider.parse_item(
        "http://example.com/intro.mp3",
        requested_media_type=MediaType.SOUND_EFFECT,
    )

    assert isinstance(result, SoundEffect)
    assert result.name == "Intro"
    assert result.duration == 12
    assert result.provider_mappings
    mapping = next(iter(result.provider_mappings))
    assert mapping.provider_domain == "builtin"
    assert mapping.provider_instance == "builtin_1"
    assert mapping.item_id == "http://example.com/intro.mp3"
    assert result.metadata.images
    assert result.metadata.images[0].path == "http://example.com/intro.mp3"


@pytest.mark.asyncio
async def test_get_stream_details_preserves_requested_sound_effect_type() -> None:
    """Stream details keep SOUND_EFFECT even when the source looks non-seekable."""
    provider = _make_provider()
    provider._get_media_info = AsyncMock(  # type: ignore[method-assign]
        return_value=DummyMediaInfo(duration=None)
    )

    result = await provider.get_stream_details(
        "http://example.com/intro.mp3",
        MediaType.SOUND_EFFECT,
    )

    assert result.media_type == MediaType.SOUND_EFFECT
    assert result.can_seek is False
    assert result.allow_seek is False


@pytest.mark.asyncio
async def test_resolve_playlist_item_skips_library_lookup_for_sound_effects() -> None:
    """Unavailable sound effects stay provider items instead of going through library lookup."""
    provider = _make_provider()
    cast("Any", provider.mass.get_provider).return_value = None
    provider.mass.music = MagicMock()
    item = PlaylistItem(
        path="builtin://sound_effect/http://example.com/intro.mp3",
        title="Intro",
        length="12",
        metadata={
            "media_type": MediaType.SOUND_EFFECT.value,
            "name": "Intro",
        },
        providers=[
            ProviderMappingInfo(
                domain="builtin",
                item_id="http://example.com/intro.mp3",
                instance_id="missing_builtin",
            )
        ],
    )

    result = await provider._resolve_playlist_item(item)

    assert isinstance(result, SoundEffect)
    provider.mass.music.get_controller.assert_not_called()
