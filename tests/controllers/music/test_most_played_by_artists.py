"""Tests for the most-played library tracks of a set of artists."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from music_assistant.controllers.music.media.tracks import TrackPlayCount

from .helpers import create_track

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

PROV = "spotify--1"


async def _add_track(mass: MusicAssistant, name: str, artist: str, play_count: int) -> int:
    """
    Add a library track by the given artist with the given play count, return its library id.

    :param mass: The MusicAssistant instance to seed.
    :param name: The track name.
    :param artist: The name of the track's artist.
    :param play_count: The play count to store for the track.
    """
    track = create_track(PROV, uuid4().hex, name=name, isrc=uuid4().hex)
    track.artists[0].name = artist
    db_track = await mass.music.tracks.add_item_to_library(track)
    await mass.music.database.execute(
        f"UPDATE {mass.music.tracks.db_table} SET play_count = {play_count} "
        f"WHERE item_id = {db_track.item_id}"
    )
    await mass.music.database.commit()
    return int(db_track.item_id)


async def test_most_played_tracks_of_the_given_artists(music_mass: MusicAssistant) -> None:
    """Played tracks by any of the artists come back most played first, up to the limit."""
    mass = music_mass
    top = await _add_track(mass, "Brightest Lights", "Lane 8", 42)
    second = await _add_track(mass, "Innerbloom", "RÜFÜS DU SOL", 17)
    await _add_track(mass, "Fingerprint", "Lane 8", 3)
    await _add_track(mass, "Never Played", "Lane 8", 0)
    await _add_track(mass, "Someone Else", "Other Artist", 99)

    result = await mass.music.tracks.most_played_by_artists(
        ["Lane 8", "RÜFÜS DU SOL", "Lane 8"], limit=2
    )

    assert result == [
        TrackPlayCount(item_id=top, name="Brightest Lights", artist="Lane 8", play_count=42),
        TrackPlayCount(item_id=second, name="Innerbloom", artist="RÜFÜS DU SOL", play_count=17),
    ]


async def test_no_artists_returns_nothing(music_mass: MusicAssistant) -> None:
    """Without artists there is nothing to look up."""
    await _add_track(music_mass, "Brightest Lights", "Lane 8", 42)

    assert await music_mass.music.tracks.most_played_by_artists([]) == []
