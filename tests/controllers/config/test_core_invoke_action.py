"""Tests for the core-module config action contract (config/core/invoke_action)."""

import pytest
from music_assistant_models.config_entries import ConfigActionResult
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import ActionUnavailable

from music_assistant.controllers.cache.constants import CONF_CLEAR_CACHE
from music_assistant.mass import MusicAssistant


async def test_get_entries_exposes_the_action_button(mass: MusicAssistant) -> None:
    """The plain options render exposes the action button alongside the server defaults."""
    entries = await mass.config.get_core_config_entries("cache")
    by_key = {entry.key: entry for entry in entries}
    assert by_key[CONF_CLEAR_CACHE].type == ConfigEntryType.ACTION
    assert "log_level" in by_key


async def test_invoke_action_runs_side_effect_and_reports_the_outcome(
    mass: MusicAssistant,
) -> None:
    """Invoking an action runs it and reports its outcome, without re-rendering the form."""
    await mass.cache.set("some_key", "some_value")
    assert await mass.cache.get("some_key") == "some_value"

    result = await mass.config.invoke_core_config_action("cache", CONF_CLEAR_CACHE)

    assert await mass.cache.get("some_key") is None
    assert isinstance(result, ConfigActionResult)
    assert result.translation_key == f"{CONF_CLEAR_CACHE}.result"
    assert result.translation_owner == "core.cache"
    assert result.open_url is None


async def test_action_result_message_resolves_from_strings_json(mass: MusicAssistant) -> None:
    """The result's translation key resolves under its stamped owner to the English text."""
    result = await mass.config.invoke_core_config_action("cache", CONF_CLEAR_CACHE)

    assert isinstance(result, ConfigActionResult)
    assert (
        mass.translations.get_translation(
            f"config_actions.{result.translation_key}", owner=result.translation_owner
        )
        == "The cache has been cleared"
    )


async def test_invoke_unknown_action_raises(mass: MusicAssistant) -> None:
    """An action a core module does not declare is rejected."""
    with pytest.raises(ActionUnavailable):
        await mass.config.invoke_core_config_action("cache", "no_such_action")


async def test_invoke_action_on_module_without_actions_raises(mass: MusicAssistant) -> None:
    """A core module that declares no actions at all falls through to the base handler."""
    with pytest.raises(ActionUnavailable):
        await mass.config.invoke_core_config_action("streams", "no_such_action")
