"""Tests for the tracks listing of a provider artist."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from music_assistant_models.enums import ProviderFeature
from music_assistant_models.media_items import ProviderMapping, Track

from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

pytestmark = pytest.mark.asyncio

_PROVIDER = "streaming_inst"


def _track(name: str) -> Track:
    mapping = ProviderMapping(
        item_id=name, provider_domain="streaming", provider_instance=_PROVIDER
    )
    return Track(item_id=name, provider=_PROVIDER, name=name, provider_mappings={mapping})


def _provider(*, artist_tracks: bool) -> MagicMock:
    """Return an available provider that offers every feature, artist tracks as given."""
    provider = MagicMock(spec=MusicProvider)
    provider.instance_id = _PROVIDER
    provider.available = True
    provider.supports_feature = MagicMock(
        side_effect=lambda feature: artist_tracks or feature != ProviderFeature.ARTIST_TRACKS
    )
    provider.get_artist_tracks.return_value = [_track("Waterloo")]
    return provider


@pytest.mark.parametrize("listing", ["tracks", "tracks_count"])
async def test_a_provider_without_artist_tracks_lists_none(
    music_mass_with_cache: MusicAssistant, listing: str
) -> None:
    """The artist's albums are never listed or expanded to stand in for its tracks."""
    provider = _provider(artist_tracks=False)
    with patch.object(music_mass_with_cache, "get_provider", return_value=provider):
        result = await getattr(music_mass_with_cache.music.artists, listing)("artist1", _PROVIDER)

    assert not result
    provider.get_artist_tracks.assert_not_awaited()
    provider.get_artist_albums.assert_not_awaited()
    provider.get_album_tracks.assert_not_awaited()


@pytest.mark.parametrize("listing", ["tracks", "tracks_count"])
async def test_a_provider_with_artist_tracks_lists_its_own(
    music_mass_with_cache: MusicAssistant, listing: str
) -> None:
    """A provider with an artist tracks listing serves it, without touching the albums."""
    provider = _provider(artist_tracks=True)
    with patch.object(music_mass_with_cache, "get_provider", return_value=provider):
        result = await getattr(music_mass_with_cache.music.artists, listing)("artist1", _PROVIDER)

    assert (result if listing == "tracks_count" else len(result)) == 1
    provider.get_artist_tracks.assert_awaited_once_with("artist1")
    provider.get_artist_albums.assert_not_awaited()
    provider.get_album_tracks.assert_not_awaited()
