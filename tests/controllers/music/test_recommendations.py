"""Tests for the recommendations subcontroller."""

from __future__ import annotations

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import ItemMapping, RecommendationFolder

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
]


async def test_default_recommendations_order(mass: MusicAssistant) -> None:
    """The default library rows appear in their canonical order."""
    folders = await mass.music.recommendations.get_recommendations()
    defaults = [f.item_id for f in folders if f.item_id in EXPECTED_DEFAULT_ORDER]
    assert defaults == EXPECTED_DEFAULT_ORDER


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


async def _recommendations_folder(mass: MusicAssistant, item_id: str) -> RecommendationFolder:
    folders = await mass.music.recommendations.get_recommendations()
    return next(f for f in folders if f.item_id == item_id)


async def test_recently_played_rolls_up_to_container(mass: MusicAssistant) -> None:
    """Playing an album shows the album, not its individual tracks."""
    await _add_playlog_row(
        mass, item_id="album-1", media_type=MediaType.ALBUM, timestamp=2000, user_initiated=True
    )
    await _add_playlog_row(
        mass, item_id="track-1", media_type=MediaType.TRACK, timestamp=1999, user_initiated=False
    )
    folder = await _recommendations_folder(mass, "recently_played")
    item_ids = {item.item_id for item in folder.items}
    assert "album-1" in item_ids
    assert "track-1" not in item_ids
