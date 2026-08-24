"""Tests for the Smart Playlist provider recommendations."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import EventType
from music_assistant_models.event import MassEvent
from music_assistant_models.media_items import ItemMapping, Playlist, ProviderMapping

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
    # rows must carry the instance_id: the items API resolves the provider from it
    assert folder.provider == plugin.instance_id
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
    assert mass.music.playlists.get_library_item_by_prov_id.await_count == 2


async def test_get_recommendation_items_unknown_id_returns_empty() -> None:
    """An unknown row item_id returns empty without building any playlist."""
    plugin, mass = _make_plugin()
    plugin._rules_store["abc"] = SmartPlaylistRules(limit=10)

    result = await plugin.get_recommendation_items("unknown_row")

    assert list(result) == []
    mass.music.playlists.get_library_item_by_prov_id.assert_not_awaited()


async def test_get_recommendation_items_returns_item_mapping_for_library_playlists() -> None:
    """Library-backed playlists are returned as ItemMapping with library DB ID."""
    plugin, mass = _make_plugin()
    plugin._rules_store["abc"] = SmartPlaylistRules(limit=10)
    plugin._names_store["abc"] = "Playlist A"

    library_playlist = Playlist(
        item_id="42",
        provider="library",
        name="Playlist A (Library)",
        owner="Smart Playlist",
        provider_mappings=set(),
    )
    mass.music.playlists.get_library_item_by_prov_id = AsyncMock(return_value=library_playlist)

    result = await plugin.get_recommendation_items("smart_playlists")

    assert len(result) == 1
    item = result[0]
    assert isinstance(item, ItemMapping)
    assert item.item_id == "42"
    assert item.provider == "library"
    assert item.name == "Playlist A (Library)"


async def test_delete_smart_playlist_signals_recommendations_updated() -> None:
    """Deleting a smart playlist emits a recommendations_updated event."""
    plugin, _mass = _make_plugin()
    plugin._rules_store["abc123"] = SmartPlaylistRules(limit=10)
    plugin._names_store["abc123"] = "Test Playlist"
    plugin._flush_rules_to_disk = AsyncMock()  # type: ignore[method-assign]
    plugin._invalidate_dynamic_sample_cache = AsyncMock()  # type: ignore[method-assign]
    plugin.signal_provider_event = MagicMock()  # type: ignore[method-assign, misc]

    library_playlist = Playlist(
        item_id="999",
        provider="library",
        name="Test Playlist",
        owner="Smart Playlist",
        provider_mappings={
            ProviderMapping(
                item_id="abc123",
                provider_domain="smart_playlist",
                provider_instance=plugin.instance_id,
            )
        },
    )
    event = MassEvent(
        event=EventType.MEDIA_ITEM_DELETED,
        object_id="999",
        data=library_playlist,
    )

    await plugin._on_media_item_deleted(event)

    assert "abc123" not in plugin._rules_store
    assert "abc123" not in plugin._names_store
    payload = plugin.signal_provider_event.call_args.args[0]
    assert payload["event"] == "recommendations_updated"


async def test_delete_unrelated_playlist_does_not_signal() -> None:
    """Deleting a playlist from a different provider does not emit an event."""
    plugin, _mass = _make_plugin()
    plugin._rules_store["abc123"] = SmartPlaylistRules(limit=10)
    plugin.signal_provider_event = MagicMock()  # type: ignore[method-assign, misc]

    unrelated_playlist = Playlist(
        item_id="999",
        provider="library",
        name="Unrelated Playlist",
        owner="Other",
        provider_mappings={
            ProviderMapping(
                item_id="other_id",
                provider_domain="other_provider",
                provider_instance="other_instance",
            )
        },
    )
    event = MassEvent(
        event=EventType.MEDIA_ITEM_DELETED,
        object_id="999",
        data=unrelated_playlist,
    )

    await plugin._on_media_item_deleted(event)

    assert "abc123" in plugin._rules_store
    plugin.signal_provider_event.assert_not_called()


async def test_create_smart_playlist_signals_recommendations_updated() -> None:
    """Creating a smart playlist emits a recommendations_updated event."""
    plugin, mass = _make_plugin()
    plugin._validate_rules = MagicMock()  # type: ignore[method-assign]
    plugin._save_rules = AsyncMock()  # type: ignore[method-assign]
    plugin._schedule_ai_description_refresh = MagicMock()  # type: ignore[method-assign]
    plugin.signal_provider_event = MagicMock()  # type: ignore[method-assign, misc]

    library_playlist = Playlist(
        item_id="42",
        provider="library",
        name="New Playlist",
        owner="Smart Playlist",
        provider_mappings=set(),
    )
    mass.music.playlists.add_item_to_library = AsyncMock(return_value=library_playlist)
    mass.metadata.schedule_update_metadata = MagicMock()

    await plugin.create_smart_playlist(
        name="New Playlist",
        rules={"limit": 10},
        is_dynamic=False,
    )

    payload = plugin.signal_provider_event.call_args.args[0]
    assert payload["event"] == "recommendations_updated"
