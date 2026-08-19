"""Tests for the albums controller."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .helpers import create_album, create_track

if TYPE_CHECKING:
    import pytest

    from music_assistant.mass import MusicAssistant


async def test_overwrite_update_keeps_artists_when_none_are_given(
    mass: MusicAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """An overwrite update carrying no artists must not clear the stored ones."""
    db_album = await mass.music.albums.add_item_to_library(create_album("spotify_1", "album1"))

    update = create_album("spotify_1", "album1", artist_name=None)
    await mass.music.albums.update_item_in_library(db_album.item_id, update, overwrite=True)

    refreshed = await mass.music.albums.get_library_item(db_album.item_id)
    assert [artist.name for artist in refreshed.artists] == ["Test Artist"]
    assert "Ignoring request to clear all artists" in caplog.text


async def test_overwrite_update_replaces_artists(mass: MusicAssistant) -> None:
    """An overwrite update carrying artists still replaces the stored ones."""
    db_album = await mass.music.albums.add_item_to_library(create_album("spotify_1", "album1"))

    # a distinct artist id, so the stored relation is replaced rather than renamed
    update = create_album(
        "spotify_1", "album1", artist_name="Other Artist", artist_item_id="other_artist"
    )
    await mass.music.albums.update_item_in_library(db_album.item_id, update, overwrite=True)

    refreshed = await mass.music.albums.get_library_item(db_album.item_id)
    assert [artist.name for artist in refreshed.artists] == ["Other Artist"]


async def test_track_overwrite_keeps_album_artists(mass: MusicAssistant) -> None:
    """A track update carrying an artist-less album must not clear that album's artists."""
    db_album = await mass.music.albums.add_item_to_library(create_album("spotify_1", "album1"))
    track = create_track("spotify_1", "track1")
    track.album = create_album("spotify_1", "album1")
    db_track = await mass.music.tracks.add_item_to_library(track)

    # a provider that builds an album object without artists (as qqmusic does)
    update = create_track("spotify_1", "track1")
    update.album = create_album("spotify_1", "album1", artist_name=None)
    await mass.music.tracks.update_item_in_library(db_track.item_id, update, overwrite=True)

    refreshed = await mass.music.albums.get_library_item(db_album.item_id)
    assert [artist.name for artist in refreshed.artists] == ["Test Artist"]
