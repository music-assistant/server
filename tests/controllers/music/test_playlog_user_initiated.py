"""
Tests for the stickiness of the playlog's ``user_initiated`` flag.

The flag is what the Discover "Recently played" row filters on, so a play the user
explicitly asked for must not be demoted by a later background play of the same item.
These integration tests use the ``mass`` fixture from ``tests/conftest.py`` which
creates a full MusicAssistant instance with a real SQLite database in a temporary
directory.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import Artist, ProviderMapping, Track
from music_assistant_models.unique_list import UniqueList

from music_assistant.constants import DB_TABLE_PLAYLOG
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


async def _add_track(mass: MusicAssistant, name: str) -> Track:
    """Add a minimal track to the library and return the (re-fetched) stored item."""
    artist = await mass.music.artists.add_item_to_library(
        Artist(
            item_id="0",
            provider="library",
            name=f"{name} Artist",
            provider_mappings=_library_mapping(),
        )
    )
    added = await mass.music.tracks.add_item_to_library(
        Track(
            item_id="0",
            provider="library",
            name=name,
            duration=180,
            provider_mappings=_library_mapping(),
            artists=UniqueList([artist]),
        )
    )
    return await mass.music.tracks.get_library_item(added.item_id)


async def _playlog_row(mass: MusicAssistant, track: Track, userid: str) -> dict[str, Any]:
    """Read the track's playlog row for the given user."""
    row = await mass.music.database.get_row(
        DB_TABLE_PLAYLOG,
        {
            "item_id": track.item_id,
            "provider": "library",
            "media_type": MediaType.TRACK.value,
            "userid": userid,
        },
    )
    assert row is not None
    return dict(row)


async def test_explicit_track_play_survives_later_autoplay(mass: MusicAssistant) -> None:
    """An autoplay/DSTM replay of an explicitly played track must not clear the flag."""
    user = await mass.webserver.auth.create_user("stickytrack")
    track = await _add_track(mass, "Sticky")

    await mass.music.mark_item_played(
        track, fully_played=True, user_initiated=True, userid=user.user_id
    )
    assert (await _playlog_row(mass, track, user.user_id))["user_initiated"] == 1

    # the same track comes round again as part of a radio/autoplay mix
    await mass.music.mark_item_played(
        track, fully_played=True, user_initiated=False, seconds_played=180, userid=user.user_id
    )

    row = await _playlog_row(mass, track, user.user_id)
    # the explicit play is remembered, so the track stays in the "Recently played" row ...
    assert row["user_initiated"] == 1
    # ... while everything else about the play is updated as usual
    assert row["seconds_played"] == 180


async def test_background_play_is_promoted_by_a_later_explicit_play(
    mass: MusicAssistant,
) -> None:
    """A track first heard via autoplay becomes user-initiated once explicitly played."""
    user = await mass.webserver.auth.create_user("promotedtrack")
    track = await _add_track(mass, "Promoted")

    await mass.music.mark_item_played(
        track, fully_played=True, user_initiated=False, userid=user.user_id
    )
    assert (await _playlog_row(mass, track, user.user_id))["user_initiated"] == 0

    await mass.music.mark_item_played(
        track, fully_played=True, user_initiated=True, userid=user.user_id
    )

    assert (await _playlog_row(mass, track, user.user_id))["user_initiated"] == 1


async def test_omitted_user_initiated_defaults_to_an_explicit_play(mass: MusicAssistant) -> None:
    """
    A caller that omits ``user_initiated`` records an explicit play.

    ``music/mark_played`` is a public API command and clients back to ``MIN_SCHEMA_VERSION``
    call it without the flag to report a play the user asked for, so the default is part of
    the wire contract rather than an implementation detail. Because the flag is sticky, a
    writer reporting playback it did not initiate must pass ``user_initiated=False`` itself.
    """
    user = await mass.webserver.auth.create_user("defaultedtrack")
    track = await _add_track(mass, "Defaulted")

    await mass.music.mark_item_played(track, userid=user.user_id)

    assert (await _playlog_row(mass, track, user.user_id))["user_initiated"] == 1
