"""Tests for the shared RecencyEngine (music/recency.py)."""

from __future__ import annotations

import time

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import ItemMapping, ProviderMapping, Track
from music_assistant_models.unique_list import UniqueList

from music_assistant.constants import DB_TABLE_PLAYLOG
from music_assistant.controllers.music.recency import RecencyWindows
from music_assistant.helpers.json import serialize_to_json
from music_assistant.mass import MusicAssistant


async def _add_playlog_row(
    mass: MusicAssistant,
    *,
    item_id: str,
    provider: str,
    media_type: MediaType,
    name: str,
    timestamp: int,
    userid: str = "user-a",
    artist_names: list[str] | None = None,
) -> None:
    """Insert a single row into the playlog."""
    await mass.music.database.insert(
        DB_TABLE_PLAYLOG,
        {
            "item_id": item_id,
            "provider": provider,
            "media_type": media_type.value,
            "name": name,
            "artists": serialize_to_json([{"name": artist_name} for artist_name in artist_names])
            if artist_names
            else None,
            "timestamp": timestamp,
            "fully_played": True,
            "seconds_played": 180,
            "userid": userid,
            "user_initiated": True,
        },
    )


def _track(item_id: str, provider: str, *, mapping: ProviderMapping | None = None) -> Track:
    """Build a minimal Track with an optional single provider mapping."""
    mappings = {mapping} if mapping else set()
    return Track(
        item_id=item_id,
        provider=provider,
        name=f"Track {item_id}",
        uri=f"{provider}://track/{item_id}",
        provider_mappings=mappings,
    )


def _named_track(item_id: str, name: str, artist_names: list[str]) -> Track:
    """Build a Track with the given title and artist names (no provider mappings)."""
    return Track(
        item_id=item_id,
        provider="library",
        name=name,
        uri=f"library://track/{item_id}",
        provider_mappings=set(),
        artists=UniqueList(
            [
                ItemMapping(
                    item_id=artist_name.lower(),
                    provider="library",
                    name=artist_name,
                    media_type=MediaType.ARTIST,
                )
                for artist_name in artist_names
            ]
        ),
    )


async def test_snapshot_builds_song_and_artist_maps(mass: MusicAssistant) -> None:
    """Track rows populate song_ts; artist rows populate artist_ts (lowercased)."""
    now = int(time.time())
    await _add_playlog_row(
        mass,
        item_id="t1",
        provider="library",
        media_type=MediaType.TRACK,
        name="T1",
        timestamp=now - 60,
    )
    await _add_playlog_row(
        mass,
        item_id="a1",
        provider="library",
        media_type=MediaType.ARTIST,
        name="The Beatles",
        timestamp=now - 60,
    )

    snapshot = await mass.music.recency.snapshot(
        RecencyWindows(song_seconds=3600, artist_seconds=3600), userid="user-a"
    )

    assert ("library", "t1") in snapshot.song_ts
    assert "the beatles" in snapshot.artist_ts


async def test_snapshot_is_user_scoped(mass: MusicAssistant) -> None:
    """A snapshot for one user does not see another user's plays."""
    now = int(time.time())
    await _add_playlog_row(
        mass,
        item_id="mine",
        provider="library",
        media_type=MediaType.TRACK,
        name="Mine",
        timestamp=now - 60,
        userid="user-a",
    )
    await _add_playlog_row(
        mass,
        item_id="theirs",
        provider="library",
        media_type=MediaType.TRACK,
        name="Theirs",
        timestamp=now - 60,
        userid="user-b",
    )

    snapshot = await mass.music.recency.snapshot(RecencyWindows(song_seconds=3600), userid="user-a")

    assert ("library", "mine") in snapshot.song_ts
    assert ("library", "theirs") not in snapshot.song_ts


async def test_track_recent_matches_own_id_and_provider_mapping(mass: MusicAssistant) -> None:
    """track_recent matches both a library play (own id) and a streaming play (via mapping)."""
    now = int(time.time())
    # a library play and a streaming play, recorded under different provider keys
    await _add_playlog_row(
        mass,
        item_id="lib1",
        provider="library",
        media_type=MediaType.TRACK,
        name="Lib",
        timestamp=now - 60,
    )
    await _add_playlog_row(
        mass,
        item_id="sp1",
        provider="spotify--x",
        media_type=MediaType.TRACK,
        name="Stream",
        timestamp=now - 60,
    )
    snapshot = await mass.music.recency.snapshot(RecencyWindows(song_seconds=3600), userid="user-a")
    stream_mapping = ProviderMapping(
        item_id="sp1", provider_domain="spotify", provider_instance="spotify--x"
    )

    # matched via the track's own (library) id
    assert snapshot.track_recent(_track("lib1", "library"), 3600) is True
    # matched via a streaming provider mapping while the track itself is the library item
    assert snapshot.track_recent(_track("libZ", "library", mapping=stream_mapping), 3600) is True
    # a track that matches neither key is not recent
    other_mapping = ProviderMapping(
        item_id="other", provider_domain="spotify", provider_instance="spotify--x"
    )
    assert snapshot.track_recent(_track("nope", "library", mapping=other_mapping), 3600) is False


async def test_track_recent_respects_window(mass: MusicAssistant) -> None:
    """A play older than the window is not considered recent."""
    now = int(time.time())
    await _add_playlog_row(
        mass,
        item_id="t1",
        provider="library",
        media_type=MediaType.TRACK,
        name="T1",
        timestamp=now - 7200,
    )
    snapshot = await mass.music.recency.snapshot(
        RecencyWindows(song_seconds=10800), userid="user-a"
    )

    assert snapshot.track_recent(_track("t1", "library"), 10800) is True  # within 3h
    assert snapshot.track_recent(_track("t1", "library"), 3600) is False  # but not within 1h


async def test_snapshot_disabled_windows_skips_query(mass: MusicAssistant) -> None:
    """With every window disabled the snapshot is empty even when plays exist."""
    now = int(time.time())
    await _add_playlog_row(
        mass,
        item_id="t1",
        provider="library",
        media_type=MediaType.TRACK,
        name="T1",
        timestamp=now - 60,
    )

    snapshot = await mass.music.recency.snapshot(RecencyWindows(), userid="user-a")

    assert not snapshot.song_ts
    assert not snapshot.artist_ts


async def test_song_lookback_uses_duplicate_gap_when_song_window_unset(
    mass: MusicAssistant,
) -> None:
    """When only the duplicate gap is set, track rows are still read back to that gap."""
    now = int(time.time())
    await _add_playlog_row(
        mass,
        item_id="recent",
        provider="library",
        media_type=MediaType.TRACK,
        name="R",
        timestamp=now - 1800,
    )
    await _add_playlog_row(
        mass,
        item_id="old",
        provider="library",
        media_type=MediaType.TRACK,
        name="O",
        timestamp=now - 7200,
    )

    snapshot = await mass.music.recency.snapshot(
        RecencyWindows(song_seconds=None, duplicate_gap_seconds=3600), userid="user-a"
    )

    assert ("library", "recent") in snapshot.song_ts
    assert ("library", "old") not in snapshot.song_ts


async def test_track_recent_fuzzy_matches_other_version(mass: MusicAssistant) -> None:
    """A different release/version of a played song (same title + shared artist) is recent too."""
    now = int(time.time())
    await _add_playlog_row(
        mass,
        item_id="amber-1",
        provider="spotify--x",
        media_type=MediaType.TRACK,
        name="Amber",
        timestamp=now - 60,
        artist_names=["The Thrillseekers"],
    )
    snapshot = await mass.music.recency.snapshot(RecencyWindows(song_seconds=3600), userid="user-a")

    # same song, different catalog item with differing artist credits: matched fuzzily
    other_version = _named_track("amber-2", "Amber", ["The Thrillseekers", "Hydra"])
    assert snapshot.track_recent(other_version, 3600) is True
    # a bracketed version suffix on the candidate title is stripped before matching
    remaster = _named_track("amber-3", "Amber (Remastered 2019)", ["The Thrillseekers"])
    assert snapshot.track_recent(remaster, 3600) is True
    # same title by an unrelated artist is NOT matched
    unrelated = _named_track("amber-4", "Amber", ["Someone Else"])
    assert snapshot.track_recent(unrelated, 3600) is False


async def test_track_recent_no_fuzzy_match_for_legacy_rows(mass: MusicAssistant) -> None:
    """Playlog rows without stored artists (legacy) only match on exact ids."""
    now = int(time.time())
    await _add_playlog_row(
        mass,
        item_id="amber-1",
        provider="spotify--x",
        media_type=MediaType.TRACK,
        name="Amber",
        timestamp=now - 60,
    )
    snapshot = await mass.music.recency.snapshot(RecencyWindows(song_seconds=3600), userid="user-a")

    other_version = _named_track("amber-2", "Amber", ["The Thrillseekers"])
    assert snapshot.track_recent(other_version, 3600) is False
