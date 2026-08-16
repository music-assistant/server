"""Tests for the Library Automations rules JSON persistence."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from music_assistant.providers.library_automations import LibraryAutomationsProvider
from music_assistant.providers.library_automations.models import RULES_FILENAME


async def _make_plugin(tmp_path: Path) -> LibraryAutomationsProvider:
    mass = MagicMock()
    mass.storage_path = str(tmp_path)
    manifest = MagicMock()
    manifest.domain = "library_automations"
    config = MagicMock()
    config.get_value.return_value = None
    plugin = LibraryAutomationsProvider(mass, manifest, config, set())
    await plugin.handle_async_init()
    return plugin


_TRIGGER = {"type": "media_item_unfavorited", "media_types": ["track"]}
_ACTION = {"type": "add_to_playlist", "params": {"playlist_name": "Sorted Out"}}


async def test_handle_async_init_creates_storage_dir(tmp_path: Path) -> None:
    """handle_async_init creates the plugin's storage subdirectory if missing."""
    await _make_plugin(tmp_path)
    assert (tmp_path / "library_automations").is_dir()


async def test_created_rule_is_written_to_disk(tmp_path: Path) -> None:
    """create_rule persists the rule to the expected JSON file on disk."""
    plugin = await _make_plugin(tmp_path)
    created = await plugin.create_rule(name="my rule", trigger=_TRIGGER, action=_ACTION)
    rules_file = tmp_path / "library_automations" / RULES_FILENAME
    assert rules_file.is_file()
    data = json.loads(rules_file.read_text())
    assert created["id"] in data
    assert data[created["id"]]["name"] == "my rule"


async def test_rules_survive_a_reload(tmp_path: Path) -> None:
    """Rules created by one provider instance are loaded by a fresh instance on init."""
    plugin = await _make_plugin(tmp_path)
    created = await plugin.create_rule(name="my rule", trigger=_TRIGGER, action=_ACTION)

    reloaded = await _make_plugin(tmp_path)
    rules = await reloaded.list_rules()
    assert len(rules) == 1
    assert rules[0]["id"] == created["id"]
    assert rules[0]["name"] == "my rule"


async def test_deleted_rule_is_removed_from_disk(tmp_path: Path) -> None:
    """delete_rule's flush removes the rule from the persisted file too."""
    plugin = await _make_plugin(tmp_path)
    created = await plugin.create_rule(name="my rule", trigger=_TRIGGER, action=_ACTION)
    await plugin.delete_rule(created["id"])

    reloaded = await _make_plugin(tmp_path)
    assert await reloaded.list_rules() == []


async def test_unload_when_removed_deletes_rules_file(tmp_path: Path) -> None:
    """unload(is_removed=True) removes the persisted rules file entirely."""
    plugin = await _make_plugin(tmp_path)
    await plugin.create_rule(name="my rule", trigger=_TRIGGER, action=_ACTION)
    rules_file = tmp_path / "library_automations" / RULES_FILENAME
    assert rules_file.is_file()

    await plugin.unload(is_removed=True)
    assert not rules_file.is_file()
