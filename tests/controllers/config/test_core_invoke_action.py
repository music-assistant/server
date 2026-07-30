"""Tests for the core-module config action contract (config/core/invoke_action)."""

import pytest
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import ActionUnavailable

from music_assistant.controllers.cache.constants import CONF_CLEAR_CACHE
from music_assistant.mass import MusicAssistant


async def test_get_entries_omits_action_results(mass: MusicAssistant) -> None:
    """The plain options render only exposes the action button, never its result."""
    entries = await mass.config.get_core_config_entries("cache")
    by_key = {entry.key: entry for entry in entries}
    assert by_key[CONF_CLEAR_CACHE].type == ConfigEntryType.ACTION
    assert "clear_cache_result" not in by_key


async def test_invoke_action_runs_side_effect_and_re_renders(mass: MusicAssistant) -> None:
    """Invoking an action runs it and returns the re-rendered entries with its result."""
    await mass.cache.set("some_key", "some_value")
    assert await mass.cache.get("some_key") == "some_value"

    entries = await mass.config.invoke_core_config_action("cache", CONF_CLEAR_CACHE)

    assert await mass.cache.get("some_key") is None
    by_key = {entry.key: entry for entry in entries}
    # the action button is still there, alongside the result label
    assert by_key[CONF_CLEAR_CACHE].type == ConfigEntryType.ACTION
    assert by_key["clear_cache_result"].type == ConfigEntryType.LABEL
    # the server default entries are appended for both commands
    assert "log_level" in by_key


async def test_invoke_unknown_action_raises(mass: MusicAssistant) -> None:
    """An action a core module does not declare is rejected."""
    with pytest.raises(ActionUnavailable):
        await mass.config.invoke_core_config_action("cache", "no_such_action")


async def test_invoke_action_on_module_without_actions_raises(mass: MusicAssistant) -> None:
    """A core module that declares no actions at all falls through to the base handler."""
    with pytest.raises(ActionUnavailable):
        await mass.config.invoke_core_config_action("streams", "no_such_action")
