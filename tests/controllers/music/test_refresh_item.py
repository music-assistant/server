"""Tests for refreshing a library item from its source."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import Album, Artist, ProviderMapping, Track, UniqueList

from music_assistant.mass import MusicAssistant
from music_assistant.providers.musicbrainz.api_client import _REFRESH_MB


def _mapping() -> ProviderMapping:
    """Return the item's streaming service mapping."""
    return ProviderMapping(item_id="abc", provider_domain="spotify", provider_instance="spotify_1")


def _album(provider: str) -> Album:
    """Return an album of the library or of its source."""
    return Album(
        item_id="1" if provider == "library" else "abc",
        provider=provider,
        name="Album",
        provider_mappings={_mapping()},
    )


def _file_mapping(item_id: str) -> ProviderMapping:
    """Return a mapping of an item read from a file."""
    return ProviderMapping(
        item_id=item_id, provider_domain="filesystem_local", provider_instance="filesystem_1"
    )


def _file_track(**kwargs: Any) -> Track:
    """Return a track as its file gives it."""
    return Track(
        item_id="f1",
        provider="filesystem_1",
        name="No. 2 Scene",
        provider_mappings={_file_mapping("f1")},
        artists=UniqueList(
            [
                Artist(
                    item_id="orchestra",
                    provider="filesystem_1",
                    name="Orchestra",
                    provider_mappings={_file_mapping("orchestra")},
                )
            ]
        ),
        **kwargs,
    )


@pytest.mark.parametrize("media_type", [MediaType.ALBUM, MediaType.TRACK])
async def test_refresh_reads_musicbrainz_past_the_cache(
    music_mass: MusicAssistant, media_type: MediaType
) -> None:
    """Refreshing an item bypasses the MusicBrainz cache so a fix there comes through."""
    ctrl: Any = music_mass.music.get_controller(media_type)
    stored: Album | Track
    source: Album | Track
    if media_type == MediaType.ALBUM:
        stored, source = _album("filesystem_1"), _album("filesystem_1")
    else:
        stored, source = _file_track(), _file_track()
    library_item = await ctrl.add_item_to_library(stored)
    refreshing_mb: list[bool] = []

    async def _update(*_args: Any, **_kwargs: Any) -> None:
        refreshing_mb.append(_REFRESH_MB.get())

    metadata = Mock()
    metadata.update_metadata = AsyncMock(side_effect=_update)
    streams = Mock()
    streams.audio_analysis.delete_measured_loudness = AsyncMock()

    with (
        patch.object(music_mass.music, "_visible_provider_for", return_value=Mock()),
        patch.object(ctrl, "get_provider_item", AsyncMock(return_value=source)),
        patch.object(ctrl, "match_providers", AsyncMock()),
        patch.object(music_mass, "metadata", metadata, create=True),
        patch.object(music_mass, "streams", streams, create=True),
        patch("music_assistant.controllers.music.controller.invalidate_listings", AsyncMock()),
    ):
        await music_mass.music.refresh_item(library_item)

    assert refreshing_mb == [True]
