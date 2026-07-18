"""Tests for the recommendations subcontroller (rows + builtin items)."""

from __future__ import annotations

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import ItemMapping

from music_assistant.constants import DB_TABLE_PLAYLOG
from music_assistant.controllers.music.recommendations.sources.base import (
    CallableRecommendationSource,
)
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
    "recent_artists",
    "recent_tracks",
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


async def test_source_descriptor_fields(mass: MusicAssistant) -> None:
    """A source's descriptor carries the row identity fields without items."""
    source = CallableRecommendationSource(
        mass,
        item_id="x",
        name="X",
        translation_key="x_key",
        icon="mdi-x",
        items_factory=lambda: _async_return(_fake_items()),
        enabled_by_default=False,
    )
    folder = source.descriptor()
    assert folder.item_id == "x"
    assert folder.provider == "library"
    assert folder.name == "X"
    assert folder.translation_key == "x_key"
    assert folder.icon == "mdi-x"
    assert folder.enabled_by_default is False
    assert folder.items == []


async def test_callable_source_builds_folder(mass: MusicAssistant) -> None:
    """A callable source builds a RecommendationFolder from its factory output."""
    source = CallableRecommendationSource(
        mass,
        item_id="x",
        name="X",
        translation_key="x_key",
        icon="mdi-x",
        items_factory=lambda: _async_return(_fake_items()),
    )
    folder = await source.build()
    assert folder is not None
    assert folder.item_id == "x"
    assert folder.name == "X"
    assert folder.translation_key == "x_key"
    assert folder.icon == "mdi-x"
    assert [item.item_id for item in folder.items] == ["a"]


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
    items = await mass.music.recommendations.get_recommendation_items("library", "recently_played")
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
        "library", "recent_artists"
    )
    track_items = await mass.music.recommendations.get_recommendation_items(
        "library", "recent_tracks"
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
    items = await mass.music.recommendations.get_recommendation_items("library", "recently_played")
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


async def test_register_adds_a_custom_source(mass: MusicAssistant) -> None:
    """Registering a custom source adds its row and serves its items."""
    before = len(mass.music.recommendations.sources)

    source = CallableRecommendationSource(
        mass,
        item_id="custom_test_row",
        name="Custom Test Row",
        translation_key="custom_test_key",
        icon="mdi-custom",
        items_factory=lambda: _async_return(_fake_items()),
    )
    mass.music.recommendations.register(source)

    assert len(mass.music.recommendations.sources) == before + 1

    folders = await mass.music.recommendations.get_recommendations()
    folder = next(f for f in folders if f.item_id == "custom_test_row")
    assert folder.name == "Custom Test Row"
    assert folder.items == []

    items = await mass.music.recommendations.get_recommendation_items("library", "custom_test_row")
    assert [item.item_id for item in items] == ["a"]


async def test_unknown_library_row_returns_empty(mass: MusicAssistant) -> None:
    """Requesting items for an unknown builtin row returns an empty list."""
    items = await mass.music.recommendations.get_recommendation_items("library", "no_such_row")
    assert items == []


async def test_failing_source_items_isolated(mass: MusicAssistant) -> None:
    """A source whose items factory raises returns an empty list, not an error."""

    async def _boom() -> list[ItemMapping]:
        raise RuntimeError("source boom")

    mass.music.recommendations.register(
        CallableRecommendationSource(
            mass,
            item_id="boom_row",
            name="Boom Row",
            translation_key="boom_key",
            icon="mdi-boom",
            items_factory=_boom,
        )
    )
    # the row is still listed (descriptors never touch the items factory)
    folders = await mass.music.recommendations.get_recommendations()
    assert "boom_row" in {f.item_id for f in folders}
    items = await mass.music.recommendations.get_recommendation_items("library", "boom_row")
    assert items == []


def _fake_items() -> list[ItemMapping]:
    return [
        ItemMapping.from_dict(
            {
                "item_id": "a",
                "provider": "library",
                "media_type": MediaType.TRACK.value,
                "name": "Track A",
            }
        )
    ]


async def _async_return(value: list[ItemMapping]) -> list[ItemMapping]:
    return value


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
