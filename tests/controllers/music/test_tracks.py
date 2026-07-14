"""Tests for the tracks controller explicit filter."""

from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import patch

import pytest

from music_assistant.controllers.music import MusicController
from music_assistant.mass import MusicAssistant


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
