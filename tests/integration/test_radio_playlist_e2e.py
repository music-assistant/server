"""
End-to-end test for the radio_playlist provider.

Boots a hermetic MusicAssistant (the always-on `radio_playlist` builtin provider loads automatically,
plus the deterministic `test` music provider), builds a radio-playlist URI for a seed track, and
asserts it resolves to a dynamic playlist whose tracks are the seed + similar tracks.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest
from music_assistant_models.media_items import Playlist

from music_assistant.mass import MusicAssistant
from music_assistant.providers.radio_playlist import radio_playlist_uri

if TYPE_CHECKING:
    from music_assistant.models.music_provider import MusicProvider


@pytest.mark.asyncio
async def test_radio_playlist_generates_dynamic_playlist(e2e_mass: MusicAssistant) -> None:
    """A radio_playlist:// URI resolves to a dynamic playlist of the seed's own + similar tracks."""
    test_prov = cast("MusicProvider", e2e_mass.get_provider("test"))
    assert test_prov is not None
    # the radio_playlist provider is always-on (builtin) and loaded by mass.start()
    radio_prov = cast("MusicProvider", e2e_mass.get_provider("radio_playlist"))
    assert radio_prov is not None

    seed = await test_prov.get_track("0_0_0")
    uri = radio_playlist_uri(seed)
    assert uri == f"radio_playlist://playlist/{seed.uri}"

    # the URI resolves to a virtual, dynamic playlist
    playlist = await e2e_mass.music.get_item_by_uri(uri)
    assert isinstance(playlist, Playlist)
    assert playlist.is_dynamic
    assert "Radio" in playlist.name

    # its tracks are the seed (base) + similar tracks (deterministic via the test provider)
    tracks = await radio_prov.get_playlist_tracks(playlist.item_id)
    assert tracks
    track_ids = {track.item_id for track in tracks}
    assert seed.item_id in track_ids
    assert len(track_ids) > 1
