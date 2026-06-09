"""Integration tests: a fully played track credits its primary artist.

Uses the ``mass`` fixture from ``tests/conftest.py`` which creates a full
MusicAssistant instance with a real SQLite database in a temporary directory.
"""

from __future__ import annotations

from uuid import uuid4

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import Artist, ProviderMapping, Track
from music_assistant_models.unique_list import UniqueList

from music_assistant.constants import DB_TABLE_ARTISTS, DB_TABLE_PLAYLOG
from music_assistant.mass import MusicAssistant


def _library_mapping() -> set[ProviderMapping]:
    """Create a single library provider mapping with a unique provider item id."""
    return {
        ProviderMapping(
            item_id=uuid4().hex,
            provider_domain="library",
            provider_instance="library",
            in_library=True,
        )
    }


async def _add_artist(mass: MusicAssistant, name: str) -> Artist:
    """Add a minimal artist to the library and return the stored item."""
    artist = Artist(
        item_id="0",
        provider="library",
        name=name,
        provider_mappings=_library_mapping(),
    )
    return await mass.music.artists.add_item_to_library(artist)


async def _add_track(mass: MusicAssistant, name: str, artists: list[Artist]) -> Track:
    """Add a minimal track with the given artists and return the stored item."""
    track = Track(
        item_id="0",
        provider="library",
        name=name,
        provider_mappings=_library_mapping(),
        artists=UniqueList(artists),
    )
    added = await mass.music.tracks.add_item_to_library(track)
    return await mass.music.tracks.get_library_item(added.item_id)


async def test_played_track_credits_primary_artist(mass: MusicAssistant) -> None:
    """Marking a track fully played bumps its primary artist and logs an artist play."""
    user = await mass.webserver.auth.create_user("playcredit")
    artist = await _add_artist(mass, "Primary Artist")
    track = await _add_track(mass, "Some Track", [artist])

    before = await mass.music.database.get_row(DB_TABLE_ARTISTS, {"item_id": artist.item_id})
    assert before is not None
    assert before["play_count"] == 0

    await mass.music.mark_item_played(
        track, fully_played=True, user_initiated=True, userid=user.user_id
    )

    after = await mass.music.database.get_row(DB_TABLE_ARTISTS, {"item_id": artist.item_id})
    assert after is not None
    assert after["play_count"] == 1
    assert after["last_played"] > 0

    playlog = await mass.music.database.get_rows(
        DB_TABLE_PLAYLOG,
        {
            "media_type": MediaType.ARTIST.value,
            "item_id": artist.item_id,
            "userid": user.user_id,
        },
    )
    assert len(playlog) == 1


async def test_played_track_does_not_credit_featured_artist(mass: MusicAssistant) -> None:
    """Only the primary artist (artists[0]) is credited; featured artists are not."""
    primary = await _add_artist(mass, "Primary")
    featured = await _add_artist(mass, "Featured")
    track = await _add_track(mass, "Collab Track", [primary, featured])

    await mass.music.mark_item_played(track, fully_played=True, user_initiated=True)

    primary_row = await mass.music.database.get_row(DB_TABLE_ARTISTS, {"item_id": primary.item_id})
    featured_row = await mass.music.database.get_row(
        DB_TABLE_ARTISTS, {"item_id": featured.item_id}
    )
    assert primary_row is not None
    assert featured_row is not None
    assert primary_row["play_count"] == 1
    assert featured_row["play_count"] == 0
