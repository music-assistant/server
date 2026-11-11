"""Tests for the Qobuz recommendation rows."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.media_items import Album

from music_assistant.providers.qobuz import SUPPORTED_FEATURES, QobuzProvider
from tests.common import use_real_create_task

ALBUM_OBJ = {
    "id": 789012,
    "title": "Test Album",
    "artist": {"id": 123456, "name": "Test Artist"},
    "streamable": True,
    "displayable": True,
    "maximum_sampling_rate": 96,
    "maximum_bit_depth": 24,
}
UNSTREAMABLE_ALBUM_OBJ = {**ALBUM_OBJ, "id": 789013, "streamable": False}
ARTISTLESS_ALBUM_OBJ = {k: v for k, v in ALBUM_OBJ.items() if k != "artist"} | {"id": 789014}
MALFORMED_ALBUM_OBJ = {k: v for k, v in ALBUM_OBJ.items() if k != "maximum_sampling_rate"} | {
    "id": 789015
}
NEW_RELEASE_OBJ = {
    "id": "0123456789012",
    "title": "New Album",
    "artists": [
        {"id": 123456, "name": "Test Artist", "roles": ["main-artist"]},
        {"id": 654321, "name": "Guest Artist", "roles": ["featured-artist"]},
    ],
    "rights": {"streamable": True, "purchasable": True},
    "audio_info": {"maximum_sampling_rate": 44.1, "maximum_bit_depth": 16},
    "dates": {"original": "2026-10-30"},
}
UNSTREAMABLE_NEW_RELEASE_OBJ = {
    **NEW_RELEASE_OBJ,
    "id": "0123456789013",
    "rights": {"streamable": False, "purchasable": True},
}
MALFORMED_NEW_RELEASE_OBJ = {k: v for k, v in NEW_RELEASE_OBJ.items() if k != "audio_info"} | {
    "id": "0123456789014"
}


@pytest.fixture
def provider() -> QobuzProvider:
    """Create a QobuzProvider with a mocked API and an empty cache."""
    mass = Mock()
    mass.cache.get_with_freshness = AsyncMock(return_value=(None, False, False))
    mass.cache.set = AsyncMock()
    use_real_create_task(mass)
    manifest = Mock()
    manifest.domain = "qobuz"
    config = Mock()
    config.instance_id = "qobuz_test"
    config.get_value.side_effect = lambda _key, default=None: default
    provider = QobuzProvider(mass, manifest, config, SUPPORTED_FEATURES)
    provider._get_data = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "albums": {
                "items": [
                    ALBUM_OBJ,
                    None,
                    {"id": None},
                    UNSTREAMABLE_ALBUM_OBJ,
                    ARTISTLESS_ALBUM_OBJ,
                    MALFORMED_ALBUM_OBJ,
                ]
            }
        }
    )
    provider.get_album = AsyncMock()  # type: ignore[method-assign]
    return provider


async def test_recommendation_rows(provider: QobuzProvider) -> None:
    """Niche rows are hidden by default and the Qobuzissime brand name is not translated."""
    rows = {row.item_id: row for row in await provider.get_recommendations()}

    assert {row_id for row_id, row in rows.items() if row.enabled_by_default} == {
        "favorite_artist_releases",
        "new_releases",
        "editor_picks",
    }
    assert rows["qobuzissime"].translation_key is None
    assert rows["press_awards"].translation_key == "press_awards"


async def test_recommendation_row_returns_featured_albums(provider: QobuzProvider) -> None:
    """A row returns the parsed albums of its featured list, skipping unusable entries."""
    items = await provider.get_recommendation_items("press_awards")

    provider._get_data.assert_awaited_once_with(  # type: ignore[attr-defined]
        "album/getFeatured", type="press-awards", limit=50
    )
    assert [item.item_id for item in items] == ["789012"]
    provider.get_album.assert_not_called()  # type: ignore[attr-defined]


async def test_unknown_recommendation_row_is_empty(provider: QobuzProvider) -> None:
    """An unknown row id returns no items and does not call the API."""
    assert len(await provider.get_recommendation_items("unknown")) == 0
    provider._get_data.assert_not_called()  # type: ignore[attr-defined]


async def test_favorite_artist_releases_row(provider: QobuzProvider) -> None:
    """The personal row returns the usable new releases of the user's favorite artists."""
    provider._get_data.return_value = {  # type: ignore[attr-defined]
        "has_more": False,
        "items": [
            NEW_RELEASE_OBJ,
            None,
            {"id": 1, "artists": []},
            UNSTREAMABLE_NEW_RELEASE_OBJ,
            MALFORMED_NEW_RELEASE_OBJ,
        ],
    }

    items = await provider.get_recommendation_items("favorite_artist_releases")

    provider._get_data.assert_awaited_once_with(  # type: ignore[attr-defined]
        "favorite/getNewReleases", type="artists", limit=50
    )
    assert [item.item_id for item in items] == ["0123456789012"]
    album = items[0]
    assert isinstance(album, Album)
    assert [artist.name for artist in album.artists] == ["Test Artist"]
    assert album.year == 2026
    mapping = next(iter(album.provider_mappings))
    assert mapping.available
    assert mapping.audio_format.sample_rate == 44100
    assert mapping.audio_format.bit_depth == 16
