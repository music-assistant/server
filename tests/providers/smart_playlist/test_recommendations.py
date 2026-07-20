"""Tests for the Smart Playlist provider recommendations."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.media_items import Playlist

from music_assistant.providers.smart_playlist import SmartPlaylistProvider
from music_assistant.providers.smart_playlist.helpers import SmartPlaylistRules


def _make_plugin() -> tuple[SmartPlaylistProvider, MagicMock]:
    """Create a SmartPlaylistProvider with mocked mass and empty stores."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.domain = "smart_playlist"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    plugin = SmartPlaylistProvider(mass, manifest, config, set())
    plugin._rules_store = {}
    plugin._names_store = {}
    plugin._descriptions_store = {}
    # the only backend touchpoint of the items path (playlist artwork lookup)
    mass.music.playlists.get_library_item_by_prov_id = AsyncMock(return_value=None)
    return plugin, mass


async def test_get_recommendations_returns_static_row() -> None:
    """A populated rules store yields the single static row, without items or backend I/O."""
    plugin, mass = _make_plugin()
    plugin._rules_store["abc"] = SmartPlaylistRules(limit=10)

    result = await plugin.get_recommendations()

    assert len(result) == 1
    folder = result[0]
    assert folder.item_id == "smart_playlists"
    assert folder.provider == "smart_playlist"
    assert folder.name == "Smart Playlists"
    assert folder.translation_key == "smart_playlists"
    assert len(folder.items) == 0
    mass.music.playlists.get_library_item_by_prov_id.assert_not_awaited()


async def test_get_recommendations_empty_store_returns_no_rows() -> None:
    """An empty rules store yields no rows."""
    plugin, _mass = _make_plugin()

    assert await plugin.get_recommendations() == []


async def test_get_recommendation_items_builds_playlists() -> None:
    """The smart_playlists row builds one playlist per stored rule set."""
    plugin, mass = _make_plugin()
    plugin._rules_store["abc"] = SmartPlaylistRules(limit=10, is_dynamic=True)
    plugin._rules_store["def"] = SmartPlaylistRules(limit=20, is_dynamic=False)
    plugin._names_store["abc"] = "Playlist A"
    plugin._names_store["def"] = "Playlist B"

    result = await plugin.get_recommendation_items("smart_playlists")

    assert [item.item_id for item in result] == ["abc", "def"]
    assert all(isinstance(item, Playlist) for item in result)
    assert [item.name for item in result] == ["Playlist A", "Playlist B"]
    # the per-playlist artwork lookup ran for each built playlist
    assert mass.music.playlists.get_library_item_by_prov_id.await_count == 2


async def test_get_recommendation_items_unknown_id_returns_empty() -> None:
    """An unknown row item_id returns empty without building any playlist."""
    plugin, mass = _make_plugin()
    plugin._rules_store["abc"] = SmartPlaylistRules(limit=10)

    result = await plugin.get_recommendation_items("unknown_row")

    assert list(result) == []
    mass.music.playlists.get_library_item_by_prov_id.assert_not_awaited()
