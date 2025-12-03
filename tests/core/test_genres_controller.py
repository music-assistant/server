"""Tests for GenresController."""

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from music_assistant import MusicAssistant


@pytest.mark.asyncio
async def test_genres_controller_basic(mass: "MusicAssistant") -> None:
    """Test GenresController basic functionality."""
    genres_ctrl = mass.music.genres

    # Test 1: resolve_genre creates new genres when they don't exist
    new_genre = await genres_ctrl.resolve_genre("Test Genre")
    assert new_genre is not None
    assert new_genre.name == "Test Genre"
    assert int(new_genre.item_id) > 0

    # Test 2: Resolving same genre again returns existing one
    same_genre = await genres_ctrl.resolve_genre("Test Genre")
    assert same_genre.item_id == new_genre.item_id

    # Test 3: Verify it's persisted in the DB
    db_genre = await genres_ctrl.get_library_item(new_genre.item_id)
    assert db_genre.name == "Test Genre"
    assert db_genre.item_id == new_genre.item_id

    # Test 4: Add an alias
    await genres_ctrl.add_alias(int(new_genre.item_id), "Test Alias")

    # Test 5: Resolve via alias should return the same genre
    resolved_via_alias = await genres_ctrl.resolve_genre("Test Alias")
    assert resolved_via_alias.item_id == new_genre.item_id
    assert resolved_via_alias.name == "Test Genre"

    # Test 6: Create multiple genres
    genre2 = await genres_ctrl.resolve_genre("Another Genre")
    assert genre2.item_id != new_genre.item_id
    assert genre2.name == "Another Genre"

    # Test 7: Search functionality
    search_results = await genres_ctrl.search("Test", "library")
    assert len(search_results) > 0
    # Should find our "Test Genre"
    assert any(g.name == "Test Genre" for g in search_results)
