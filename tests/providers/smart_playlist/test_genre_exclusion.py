"""Tests for the genre exclusion rule of the Smart Playlist provider."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from music_assistant_models.enums import MediaType
from music_assistant_models.helpers import set_global_cache_values
from music_assistant_models.media_items import Genre, MediaItemMetadata

from music_assistant.providers.smart_playlist import SmartPlaylistProvider
from music_assistant.providers.smart_playlist.helpers import SmartPlaylistRules
from tests.controllers.music.helpers import create_track

if TYPE_CHECKING:
    from music_assistant_models.media_items import Track

    from music_assistant.mass import MusicAssistant

PROV = "spotify--1"


async def _add_track(mass: MusicAssistant, name: str) -> Track:
    """Add a library track whose tags name the Pop genre."""
    track = create_track(PROV, uuid4().hex, name=name, isrc=uuid4().hex)
    track.metadata = MediaItemMetadata(genres={"Pop"})
    for mapping in track.provider_mappings:
        mapping.in_library = True
    return await mass.music.tracks.add_item_to_library(track)


async def test_excluded_genre_drops_a_linked_track_with_other_tags(mass: MusicAssistant) -> None:
    """A track linked to an excluded genre is dropped, whatever genres its tags name."""
    linked = await _add_track(mass, "Linked")
    unlinked = await _add_track(mass, "Unlinked")
    genre = await mass.music.genres.add_item_to_library(
        Genre(item_id="0", provider="library", name=uuid4().hex, provider_mappings=set())
    )
    await mass.music.genres.add_media_mapping(genre.item_id, MediaType.TRACK, linked.item_id)
    # the tracks are only available while their provider counts as loaded
    await set_global_cache_values({"available_providers": {PROV}})
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, MagicMock(domain="smart_playlist"), config, set())
    rules = SmartPlaylistRules(excluded_genre_ids=[int(genre.item_id)])

    with patch.object(plugin, "_get_library_tracks", AsyncMock(return_value=[linked, unlinked])):
        result = await plugin._evaluate_rules(rules)

    assert [track.item_id for track in result] == [unlinked.item_id]
