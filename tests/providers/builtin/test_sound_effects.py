"""Tests for builtin sound effect URL handling."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import Radio, SoundEffect, Track

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


@pytest.mark.asyncio
async def test_get_stream_details_detects_radio_from_icyname_tag() -> None:
    """Recognize an icy-name tag (stored as icyname) as a radio stream."""
    provider = _make_provider()
    provider._get_media_info = AsyncMock(  # type: ignore[method-assign]
        return_value=DummyMediaInfo(duration=180, extra={"icyname": "Radio Station"})
    )

    result = await provider.get_stream_details(
        "http://example.com/stream.mp3",
        MediaType.TRACK,
    )

    assert result.media_type == MediaType.RADIO


@pytest.mark.asyncio
async def test_parse_item_returns_sound_effect_for_an_untagged_url() -> None:
    """A plain URL without music tags is a one-off clip, not a track."""
    provider = _make_provider()
    provider._get_media_info = AsyncMock(  # type: ignore[method-assign]
        return_value=DummyMediaInfo(title="notification", duration=3)
    )

    result = await provider.parse_item(
        "http://example.com/notification.mp3",
        requested_media_type=MediaType.UNKNOWN,
    )

    assert isinstance(result, SoundEffect)
    assert result.name == "notification"
    assert result.duration == 3


@pytest.mark.asyncio
async def test_parse_item_returns_track_for_a_tagged_url() -> None:
    """A plain URL carrying music tags stays a track, with its artists."""
    provider = _make_provider()
    provider._get_media_info = AsyncMock(  # type: ignore[method-assign]
        return_value=DummyMediaInfo(
            title="Song", duration=240, artists=["Artist"], extra={"artist": "Artist"}
        )
    )

    result = await provider.parse_item(
        "http://example.com/song.mp3",
        requested_media_type=MediaType.UNKNOWN,
    )

    assert isinstance(result, Track)
    assert result.name == "Song"
    assert [artist.name for artist in result.artists] == ["Artist"]


@pytest.mark.asyncio
async def test_parse_item_keeps_an_untagged_stream_a_radio_station() -> None:
    """An untagged stream without a duration is still recognized as radio."""
    provider = _make_provider()
    provider._get_media_info = AsyncMock(  # type: ignore[method-assign]
        return_value=DummyMediaInfo(title="Stream", duration=None)
    )

    result = await provider.parse_item(
        "http://example.com/stream.mp3",
        requested_media_type=MediaType.UNKNOWN,
    )

    assert isinstance(result, Radio)


@pytest.mark.asyncio
async def test_parse_item_keeps_an_untagged_url_a_track_when_asked_for_one() -> None:
    """A URL stored in the library as a track stays a track, tags or not."""
    provider = _make_provider()
    provider._get_media_info = AsyncMock(  # type: ignore[method-assign]
        return_value=DummyMediaInfo(title="Clip", duration=3)
    )

    result = await provider.parse_item(
        "http://example.com/clip.mp3",
        requested_media_type=MediaType.TRACK,
    )

    assert isinstance(result, Track)
