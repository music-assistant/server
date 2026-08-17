"""Tests for the Library Automations rule CRUD API commands."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from music_assistant_models.errors import InvalidDataError, MediaNotFoundError

from music_assistant.providers.library_automations import (
    CONF_MAX_RULES,
    DEFAULT_MAX_RULES,
    LibraryAutomationsProvider,
)


async def _make_plugin(tmp_path: Path, max_rules: int | None = None) -> LibraryAutomationsProvider:
    """Create a LibraryAutomationsProvider wired to a real (tmp) storage directory."""
    mass = MagicMock()
    mass.storage_path = str(tmp_path)
    manifest = MagicMock()
    manifest.domain = "library_automations"
    config = MagicMock()
    config.values = {}
    config.get_value.side_effect = lambda key, default=None: (
        max_rules if key == CONF_MAX_RULES and max_rules is not None else default
    )
    plugin = LibraryAutomationsProvider(mass, manifest, config, set())
    await plugin.handle_async_init()
    return plugin


_TRIGGER = {"type": "media_item_unfavorited", "media_types": ["track"]}
_ACTION = {"type": "add_to_playlist", "params": {"playlist_name": "Sorted Out"}}


async def test_create_and_list_rules(tmp_path: Path) -> None:
    """create_rule adds a rule that list_rules then returns."""
    plugin = await _make_plugin(tmp_path)
    created = await plugin.create_rule(name="my rule", trigger=_TRIGGER, action=_ACTION)
    assert created["name"] == "my rule"
    assert created["enabled"] is True
    rules = await plugin.list_rules()
    assert len(rules) == 1
    assert rules[0]["id"] == created["id"]


async def test_create_rule_rejects_unsafe_name(tmp_path: Path) -> None:
    """A rule name containing path separators is rejected."""
    plugin = await _make_plugin(tmp_path)
    with pytest.raises(InvalidDataError):
        await plugin.create_rule(name="../evil", trigger=_TRIGGER, action=_ACTION)


async def test_create_rule_rejects_unknown_trigger_type(tmp_path: Path) -> None:
    """An unknown trigger type is rejected at creation time."""
    plugin = await _make_plugin(tmp_path)
    with pytest.raises(InvalidDataError):
        await plugin.create_rule(name="bad", trigger={"type": "not_a_real_trigger"}, action=_ACTION)


async def test_create_rule_rejects_unknown_action_type(tmp_path: Path) -> None:
    """An unknown action type is rejected at creation time."""
    plugin = await _make_plugin(tmp_path)
    with pytest.raises(InvalidDataError):
        await plugin.create_rule(name="bad", trigger=_TRIGGER, action={"type": "not_a_real_action"})


async def test_create_rule_enforces_max_rules(tmp_path: Path) -> None:
    """create_rule refuses to exceed the configured maximum rule count."""
    plugin = await _make_plugin(tmp_path, max_rules=1)
    await plugin.create_rule(name="first", trigger=_TRIGGER, action=_ACTION)
    with pytest.raises(InvalidDataError):
        await plugin.create_rule(name="second", trigger=_TRIGGER, action=_ACTION)


async def test_default_max_rules_used_when_unconfigured(tmp_path: Path) -> None:
    """DEFAULT_MAX_RULES is used as the cap when the config value is unset."""
    plugin = await _make_plugin(tmp_path, max_rules=None)
    assert DEFAULT_MAX_RULES > 1
    await plugin.create_rule(name="first", trigger=_TRIGGER, action=_ACTION)  # should not raise


async def test_get_rule_returns_none_for_unknown_id(tmp_path: Path) -> None:
    """get_rule returns None (not an error) for an unknown id."""
    plugin = await _make_plugin(tmp_path)
    assert await plugin.get_rule("does-not-exist") is None


async def test_update_rule_merges_fields(tmp_path: Path) -> None:
    """update_rule overwrites only the given fields, keeping the rest intact."""
    plugin = await _make_plugin(tmp_path)
    created = await plugin.create_rule(name="my rule", trigger=_TRIGGER, action=_ACTION)
    updated = await plugin.update_rule(created["id"], name="renamed")
    assert updated["name"] == "renamed"
    assert updated["trigger"]["type"] == _TRIGGER["type"]


async def test_update_rule_unknown_id_raises(tmp_path: Path) -> None:
    """update_rule on an unknown id raises MediaNotFoundError."""
    plugin = await _make_plugin(tmp_path)
    with pytest.raises(MediaNotFoundError):
        await plugin.update_rule("does-not-exist", name="x")


async def test_delete_rule_removes_it(tmp_path: Path) -> None:
    """delete_rule removes the rule from list_rules."""
    plugin = await _make_plugin(tmp_path)
    created = await plugin.create_rule(name="my rule", trigger=_TRIGGER, action=_ACTION)
    await plugin.delete_rule(created["id"])
    assert await plugin.list_rules() == []


async def test_delete_rule_unknown_id_is_a_noop(tmp_path: Path) -> None:
    """Deleting an unknown rule id does not raise."""
    plugin = await _make_plugin(tmp_path)
    await plugin.delete_rule("does-not-exist")  # should not raise


async def test_set_rule_enabled_toggles_flag(tmp_path: Path) -> None:
    """set_rule_enabled flips the enabled flag on the stored rule."""
    plugin = await _make_plugin(tmp_path)
    created = await plugin.create_rule(name="my rule", trigger=_TRIGGER, action=_ACTION)
    await plugin.set_rule_enabled(created["id"], False)
    updated = await plugin.get_rule(created["id"])
    assert updated is not None
    assert updated["enabled"] is False


async def test_set_rule_enabled_unknown_id_raises(tmp_path: Path) -> None:
    """set_rule_enabled on an unknown id raises MediaNotFoundError."""
    plugin = await _make_plugin(tmp_path)
    with pytest.raises(MediaNotFoundError):
        await plugin.set_rule_enabled("does-not-exist", True)


async def test_list_trigger_types_and_action_types(tmp_path: Path) -> None:
    """list_trigger_types / list_action_types expose the full registries."""
    plugin = await _make_plugin(tmp_path)
    trigger_ids = {t["id"] for t in await plugin.list_trigger_types()}
    action_ids = {a["id"] for a in await plugin.list_action_types()}
    assert "media_item_unfavorited" in trigger_ids
    assert "media_item_favorited" in trigger_ids
    assert "media_item_added_to_library" in trigger_ids
    assert "add_to_playlist" in action_ids
    assert "remove_from_playlist" in action_ids
    assert "remove_from_library" in action_ids
