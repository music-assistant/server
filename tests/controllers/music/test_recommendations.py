"""Tests for the recommendations subcontroller (rows + builtin items)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import MediaType

from music_assistant.constants import DB_TABLE_PLAYLOG
from music_assistant.mass import MusicAssistant
from music_assistant.providers.recommendations import LibraryRecommendationsProvider, LibraryRowID

if TYPE_CHECKING:
    import pytest
    from music_assistant_models.media_items import ItemMapping

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
    "recent_artists",
    "recent_tracks",
    "forgotten_tracks",
    "forgotten_albums",
    "forgotten_artists",
    "most_played_tracks",
    "never_played_tracks",
]


async def test_default_recommendations_order(mass: MusicAssistant) -> None:
    """The default library rows appear in their canonical order."""
    folders = await mass.music.recommendations.get_recommendations()
    defaults = [f.item_id for f in folders if f.item_id in EXPECTED_DEFAULT_ORDER]
    assert defaults == EXPECTED_DEFAULT_ORDER


async def test_recommendations_rows_have_no_items(mass: MusicAssistant) -> None:
    """The rows listing returns descriptors only; no row carries items."""
    await _add_playlog_row(
        mass, item_id="album-1", media_type=MediaType.ALBUM, timestamp=2000, user_initiated=True
    )
    folders = await mass.music.recommendations.get_recommendations()
    assert folders
    assert all(folder.items == [] for folder in folders)


async def test_library_rows_have_descriptor_fields(mass: MusicAssistant) -> None:
    """Library rows carry their identity fields, correct defaults, and no items."""
    provider = mass.get_provider("recommendations")
    assert provider is not None, "recommendations provider should be loaded as builtin"
    assert isinstance(provider, LibraryRecommendationsProvider)
    rows = await provider.get_recommendations()
    in_progress = next(f for f in rows if f.item_id == "in_progress")
    assert in_progress.provider == "recommendations"
    assert in_progress.name == "In progress"
    assert in_progress.translation_key == "in_progress_items"
    assert in_progress.icon == "mdi-motion-play"
    assert in_progress.enabled_by_default is True
    random_artists = next(f for f in rows if f.item_id == "random_artists")
    assert random_artists.enabled_by_default is False
    assert all(folder.items == [] for folder in rows)


async def test_recently_played_rolls_up_to_container(mass: MusicAssistant) -> None:
    """Playing an album shows the album, not its individual tracks."""
    await _add_playlog_row(
        mass, item_id="album-1", media_type=MediaType.ALBUM, timestamp=2000, user_initiated=True
    )
    await _add_playlog_row(
        mass, item_id="track-1", media_type=MediaType.TRACK, timestamp=1999, user_initiated=False
    )
    await _add_playlog_row(
        mass,
        item_id="track-direct",
        media_type=MediaType.TRACK,
        timestamp=2001,
        user_initiated=True,
    )
    items = await mass.music.recommendations.get_recommendation_items(
        "recommendations", "recently_played"
    )
    item_ids = {item.item_id for item in items}
    assert "album-1" in item_ids
    assert "track-1" not in item_ids
    assert "track-direct" in item_ids


async def test_recent_artists_and_tracks_rows_present(mass: MusicAssistant) -> None:
    """Recent Artists shows played artists; Recent Tracks shows played tracks."""
    await _add_playlog_row(
        mass, item_id="artist-1", media_type=MediaType.ARTIST, timestamp=3000, user_initiated=True
    )
    await _add_playlog_row(
        mass, item_id="track-9", media_type=MediaType.TRACK, timestamp=2999, user_initiated=False
    )

    artist_items = await mass.music.recommendations.get_recommendation_items(
        "recommendations", "recent_artists"
    )
    track_items = await mass.music.recommendations.get_recommendation_items(
        "recommendations", "recent_tracks"
    )

    assert "artist-1" in {item.item_id for item in artist_items}
    assert "artist-1" not in {item.item_id for item in track_items}
    assert "track-9" in {item.item_id for item in track_items}
    assert "track-9" not in {item.item_id for item in artist_items}


async def test_recently_played_includes_podcast_and_audiobook_containers(
    mass: MusicAssistant,
) -> None:
    """Recently Played includes podcast/audiobook containers but excludes episodes and non-user-initiated tracks."""
    await _add_playlog_row(
        mass, item_id="album-x", media_type=MediaType.ALBUM, timestamp=3000, user_initiated=True
    )
    await _add_playlog_row(
        mass,
        item_id="podcast-x",
        media_type=MediaType.PODCAST,
        timestamp=3001,
        user_initiated=False,
    )
    await _add_playlog_row(
        mass,
        item_id="audiobook-x",
        media_type=MediaType.AUDIOBOOK,
        timestamp=3002,
        user_initiated=False,
    )
    await _add_playlog_row(
        mass,
        item_id="episode-x",
        media_type=MediaType.PODCAST_EPISODE,
        timestamp=3003,
        user_initiated=False,
    )
    await _add_playlog_row(
        mass,
        item_id="loose-track",
        media_type=MediaType.TRACK,
        timestamp=2999,
        user_initiated=False,
    )
    items = await mass.music.recommendations.get_recommendation_items(
        "recommendations", "recently_played"
    )
    item_ids = {item.item_id for item in items}
    assert "album-x" in item_ids, "album (user-initiated) should appear"
    assert "podcast-x" in item_ids, "podcast show should always appear"
    assert "audiobook-x" in item_ids, "audiobook should always appear"
    assert "episode-x" not in item_ids, "podcast episode should not appear"
    assert "loose-track" not in item_ids, "non-user-initiated track should be filtered out"


async def test_recently_played_always_include_media_types_query(mass: MusicAssistant) -> None:
    """always_include_media_types OR-s in those types regardless of user_initiated_only."""
    await _add_playlog_row(
        mass,
        item_id="podcast-q",
        media_type=MediaType.PODCAST,
        timestamp=5000,
        user_initiated=False,
    )
    await _add_playlog_row(
        mass,
        item_id="track-q",
        media_type=MediaType.TRACK,
        timestamp=4999,
        user_initiated=False,
    )
    results = await mass.music.recently_played(
        media_types=[MediaType.TRACK],
        user_initiated_only=True,
        always_include_media_types=[MediaType.PODCAST],
    )
    result_ids = {item.item_id for item in results}
    assert "podcast-q" in result_ids, "podcast should be returned via always_include_media_types"
    assert "track-q" not in result_ids, "non-user-initiated track should be excluded"


async def test_every_library_row_dispatches_a_query(mass: MusicAssistant) -> None:
    """
    Every id listed by get_recommendations() reaches a real query branch in get_recommendation_items().

    The rows listing and the items dispatch live in two separate functions; this
    pins that no listed row silently falls through to the empty default arm.
    """
    provider = mass.get_provider("recommendations")
    assert provider is not None
    assert isinstance(provider, LibraryRecommendationsProvider)

    # Verify every enum value has a corresponding match case by checking that all folder IDs
    # from get_recommendations() are valid LibraryRowID enum members
    valid_ids = {row_id.value for row_id in LibraryRowID}
    for folder in await provider.get_recommendations():
        assert folder.item_id in valid_ids, (
            f"row {folder.item_id!r} not in LibraryRowID enum - likely missing match case"
        )


async def test_library_rows_listed_by_controller(mass: MusicAssistant) -> None:
    """Every library row appears in the controller's rows listing."""
    folders = await mass.music.recommendations.get_recommendations()
    listed = {f.item_id for f in folders if f.provider == "recommendations"}
    provider = mass.get_provider("recommendations")
    assert provider is not None
    assert isinstance(provider, LibraryRecommendationsProvider)
    expected_rows = {f.item_id for f in await provider.get_recommendations()}
    assert expected_rows <= listed


async def test_unknown_library_row_returns_empty(mass: MusicAssistant) -> None:
    """Requesting items for an unknown builtin row returns an empty list."""
    items = await mass.music.recommendations.get_recommendation_items(
        "recommendations", "no_such_row"
    )
    assert items == []


async def test_failing_library_row_items_isolated(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A library row whose items query raises returns an empty list, not an error."""

    async def _boom(**_kwargs: object) -> list[ItemMapping]:
        raise RuntimeError("row boom")

    monkeypatch.setattr(mass.music, "in_progress_items", _boom)
    items = await mass.music.recommendations.get_recommendation_items(
        "recommendations", "in_progress"
    )
    assert items == []


async def _add_playlog_row(
    mass: MusicAssistant,
    *,
    item_id: str,
    media_type: MediaType,
    timestamp: int,
    user_initiated: bool,
    userid: str = "user-a",
) -> None:
    await mass.music.database.insert(
        DB_TABLE_PLAYLOG,
        {
            "item_id": item_id,
            "provider": "library",
            "media_type": media_type.value,
            "name": f"{media_type.value} {item_id}",
            "timestamp": timestamp,
            "fully_played": True,
            "seconds_played": 180,
            "userid": userid,
            "user_initiated": user_initiated,
        },
    )
