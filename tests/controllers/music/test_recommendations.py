"""Tests for the recommendations subcontroller."""

from __future__ import annotations

from music_assistant.mass import MusicAssistant

EXPECTED_DEFAULT_ORDER = [
    "in_progress",
    "recently_played",
    "recently_added_tracks",
    "recently_added_albums",
    "random_artists",
    "random_albums",
    "recent_favorite_tracks",
    "favorite_playlists",
    "favorite_radio",
]


async def test_default_recommendations_order(mass: MusicAssistant) -> None:
    """The default library rows appear in their canonical order."""
    folders = await mass.music.recommendations()
    defaults = [f.item_id for f in folders if f.item_id in EXPECTED_DEFAULT_ORDER]
    assert defaults == EXPECTED_DEFAULT_ORDER
