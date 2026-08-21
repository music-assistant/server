"""Tests for the tracks controller."""

from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.media_items import Artist, ProviderMapping, UniqueList

from music_assistant.controllers.music import MusicController
from music_assistant.mass import MusicAssistant

from .helpers import create_track


@pytest.fixture
async def music(mass_minimal: MusicAssistant) -> AsyncGenerator[MusicController]:
    """Return a music controller attached to the minimal mass instance."""
    controller = MusicController(mass_minimal)
    mass_minimal.music = controller
    yield controller
    if controller._database:
        await controller._database.close()


@pytest.mark.asyncio
async def test_explicit_filter_true_generates_sql(music: MusicController) -> None:
    """Test that explicit=True generates correct SQL filter."""
    captured_parts: list[str] = []

    async def mock_query(*_args: Any, **kwargs: Any) -> list[Any]:
        captured_parts.extend(kwargs.get("extra_query_parts", []))
        return []

    with patch.object(music.tracks, "get_library_items_by_query", mock_query):
        await music.tracks.library_items(explicit=True, limit=10)
        assert any(
            "json_extract(tracks.metadata, '$.explicit') = 1" in part for part in captured_parts
        )


@pytest.mark.asyncio
async def test_explicit_filter_false_generates_sql(music: MusicController) -> None:
    """Test that explicit=False generates correct SQL filter."""
    captured_parts: list[str] = []

    async def mock_query(*_args: Any, **kwargs: Any) -> list[Any]:
        captured_parts.extend(kwargs.get("extra_query_parts", []))
        return []

    with patch.object(music.tracks, "get_library_items_by_query", mock_query):
        await music.tracks.library_items(explicit=False, limit=10)
        assert any("IS NULL" in part and "= 0" in part for part in captured_parts)


@pytest.mark.asyncio
async def test_explicit_filter_none_generates_no_sql(music: MusicController) -> None:
    """Test that explicit=None generates no explicit filter."""
    captured_parts: list[str] = []

    async def mock_query(*_args: Any, **kwargs: Any) -> list[Any]:
        captured_parts.extend(kwargs.get("extra_query_parts", []))
        return []

    with patch.object(music.tracks, "get_library_items_by_query", mock_query):
        await music.tracks.library_items(explicit=None, limit=10)
        assert not any("explicit" in part.lower() for part in captured_parts)


@pytest.mark.asyncio
async def test_explicit_filter_default_is_none(music: MusicController) -> None:
    """Test that omitting explicit parameter behaves like explicit=None."""
    captured_parts: list[str] = []

    async def mock_query(*_args: Any, **kwargs: Any) -> list[Any]:
        captured_parts.extend(kwargs.get("extra_query_parts", []))
        return []

    with patch.object(music.tracks, "get_library_items_by_query", mock_query):
        await music.tracks.library_items(limit=10)
        assert not any("explicit" in part.lower() for part in captured_parts)


@pytest.mark.asyncio
async def test_by_prov_id_batches_item_ids_into_in_clause(music: MusicController) -> None:
    """provider_item_ids builds a single parameterized IN (...) subquery."""
    captured_parts: list[str] = []
    captured_params: dict[str, Any] = {}

    async def mock_query(*_args: Any, **kwargs: Any) -> list[Any]:
        captured_parts.extend(kwargs.get("extra_query_parts", []))
        captured_params.update(kwargs.get("extra_query_params", {}))
        return []

    with patch.object(music.tracks, "get_library_items_by_query", mock_query):
        await music.tracks.get_library_items_by_prov_id(
            provider_instance_id_or_domain="spotify",
            provider_item_ids=["x", "y", "z"],
        )

    subquery = " ".join(captured_parts)
    assert "provider_mappings.provider_item_id IN (:item_id_0, :item_id_1, :item_id_2)" in subquery
    assert captured_params["prov_id"] == "spotify"
    assert [captured_params[f"item_id_{i}"] for i in range(3)] == ["x", "y", "z"]


@pytest.mark.asyncio
async def test_by_prov_id_empty_item_ids_matches_nothing(music: MusicController) -> None:
    """An explicit empty provider_item_ids returns [] (not the whole provider library)."""
    ran = False

    async def mock_query(*_args: Any, **_kwargs: Any) -> list[Any]:
        nonlocal ran
        ran = True
        return []

    with patch.object(music.tracks, "get_library_items_by_query", mock_query):
        result = await music.tracks.get_library_items_by_prov_id(
            provider_instance_id_or_domain="spotify", provider_item_ids=[]
        )

    assert result == []
    assert ran is False  # short-circuits before it can build an unconstrained query


async def test_match_provider_uses_full_track_mapping(music: MusicController) -> None:
    """Provider matching stores mapping details from the fetched track."""
    base_track = create_track("spotify_1", "base")
    search_track = create_track("qobuz_1", "candidate")
    full_track = create_track("qobuz_1", "candidate")
    full_track.provider_mappings = {
        ProviderMapping(
            item_id="candidate",
            provider_domain="qobuz",
            provider_instance="qobuz_1",
            url="https://provider.example/full",
        )
    }
    provider = MagicMock()
    provider.name = "Qobuz"
    provider.domain = "qobuz"

    with (
        patch.object(music.tracks, "search", AsyncMock(return_value=[search_track])),
        patch.object(
            music.tracks,
            "get_provider_item",
            AsyncMock(return_value=full_track),
        ),
        patch(
            "music_assistant.controllers.music.media.tracks.compare_media_item",
            return_value=True,
        ),
        patch(
            "music_assistant.controllers.music.media.tracks.compare_track",
            return_value=True,
        ),
    ):
        mappings = await music.tracks.match_provider(base_track, provider, ref_albums=[])

    assert mappings == list(full_track.provider_mappings)


async def test_overwrite_update_keeps_artists_when_none_are_given(
    mass: MusicAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """An overwrite update carrying no artists must not clear the stored ones."""
    db_track = await mass.music.tracks.add_item_to_library(create_track("spotify_1", "track1"))

    update = create_track("spotify_1", "track1")
    update.artists = UniqueList()
    await mass.music.tracks.update_item_in_library(db_track.item_id, update, overwrite=True)

    refreshed = await mass.music.tracks.get_library_item(db_track.item_id)
    assert [artist.name for artist in refreshed.artists] == ["Test Artist"]
    assert "Ignoring request to clear all artists" in caplog.text


async def test_overwrite_update_replaces_artists(mass: MusicAssistant) -> None:
    """An overwrite update carrying artists still replaces the stored ones."""
    db_track = await mass.music.tracks.add_item_to_library(create_track("spotify_1", "track1"))

    update = create_track("spotify_1", "track1")
    update.artists = UniqueList(
        [
            Artist(
                item_id="other_artist",
                provider="spotify_1",
                name="Other Artist",
                provider_mappings={
                    ProviderMapping(
                        item_id="other_artist",
                        provider_domain="spotify",
                        provider_instance="spotify_1",
                    )
                },
            )
        ]
    )
    await mass.music.tracks.update_item_in_library(db_track.item_id, update, overwrite=True)

    refreshed = await mass.music.tracks.get_library_item(db_track.item_id)
    assert [artist.name for artist in refreshed.artists] == ["Other Artist"]
