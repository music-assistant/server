"""Tests for the source-specific artist playlist endpoint."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from music_assistant_models.media_items import Playlist, ProviderMapping

from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


@pytest.mark.parametrize(("available", "supported"), [(False, True), (True, False)])
async def test_unsupported_artist_playlists(
    music_mass_with_cache: MusicAssistant, available: bool, supported: bool
) -> None:
    """Unavailable and unsupported sources are never queried."""
    provider = MagicMock(spec=MusicProvider)
    provider.available = available
    provider.supports_feature.return_value = supported
    with patch.object(music_mass_with_cache.music, "get_visible_provider", return_value=provider):
        assert await music_mass_with_cache.music.artists.playlists("123", "soundcloud") == []
    provider.get_artist_playlists.assert_not_awaited()


async def test_artist_playlists_passes_exact_source_id(
    music_mass_with_cache: MusicAssistant,
) -> None:
    """Library artist IDs are not substituted for provider user IDs."""
    provider = MagicMock(spec=MusicProvider)
    provider.available = True
    provider.supports_feature.return_value = True
    playlist = Playlist(
        item_id="1",
        provider="soundcloud",
        name="Set",
        provider_mappings={
            ProviderMapping(
                item_id="1", provider_domain="soundcloud", provider_instance="soundcloud--1"
            )
        },
    )
    provider.get_artist_playlists.return_value = [playlist]
    with patch.object(music_mass_with_cache.music, "get_visible_provider", return_value=provider):
        assert await music_mass_with_cache.music.artists.playlists("123", "soundcloud--1") == [
            playlist
        ]
    provider.get_artist_playlists.assert_awaited_once_with("123")
