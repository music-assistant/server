"""Tests for the AI core controller holding the default engines."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import ConfigEntryType

from music_assistant.constants import CONF_AI, CONF_CORE
from music_assistant.controllers.ai import AIController
from music_assistant.models.plugin import AIEngine, PluginProvider, TTSEngine

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


def _create_plugin() -> MagicMock:
    """Create a mock plugin exposing two AI engines and a TTS engine, but no image engine."""
    plugin = MagicMock(spec=PluginProvider)
    plugin.instance_id = "plugin_a"
    plugin.name = "Plugin A"
    plugin.get_ai_engines = AsyncMock(
        return_value=[
            AIEngine(id="alpha", name="Alpha", provider=plugin),
            AIEngine(id="bravo", name="Bravo", provider=plugin),
        ]
    )
    plugin.get_tts_engines = AsyncMock(
        return_value=[TTSEngine(id="voice", name="Voice", provider=plugin)]
    )
    plugin.get_image_engines = AsyncMock(return_value=[])
    return plugin


async def _set_up(mass: MusicAssistant, *plugins: MagicMock) -> AIController:
    """Set up the AI controller on a minimal server serving the given plugins."""
    mass.get_providers_supporting_feature = MagicMock(  # type: ignore[method-assign]
        return_value=list(plugins)
    )
    mass.ai = AIController(mass)
    await mass.ai.setup(await mass.config.get_core_config(CONF_AI))
    return mass.ai


async def test_entries_pick_the_default_of_each_kind(mass_minimal: MusicAssistant) -> None:
    """Each kind gets a picker, required only while it has engines to pick from."""
    controller = await _set_up(mass_minimal, _create_plugin())

    entries = {entry.key: entry for entry in await controller.get_config_entries()}

    assert set(entries) == {
        "text_engine",
        "speech_engine",
        "image_engine",
        "image_engine_unavailable",
    }
    assert entries["text_engine"].required is True
    assert [option.value for option in entries["text_engine"].options] == [
        "plugin_a/alpha",
        "plugin_a/bravo",
    ]
    assert entries["speech_engine"].required is True
    assert entries["image_engine"].required is False
    assert entries["image_engine"].hidden is True
    assert entries["image_engine_unavailable"].type == ConfigEntryType.ALERT
    assert all(entry.category == "generic" for entry in entries.values())


async def test_setup_creates_the_config_block(mass_minimal: MusicAssistant) -> None:
    """The stored block exists after setup, also before any default was adopted."""
    await _set_up(mass_minimal)

    assert mass_minimal.config.get(f"{CONF_CORE}/{CONF_AI}") == {
        "domain": CONF_AI,
        "last_error": None,
        "values": {},
    }


async def test_config_shows_the_adopted_defaults(mass_minimal: MusicAssistant) -> None:
    """The first read of the config adopts the defaults and shows them."""
    await _set_up(mass_minimal, _create_plugin())

    config = await mass_minimal.config.get_core_config(CONF_AI)

    assert config.get_value("text_engine") == "plugin_a/alpha"
    assert config.get_value("speech_engine") == "plugin_a/voice"
    assert config.get_value("image_engine") is None
    config.validate()


async def test_saving_another_default(mass_minimal: MusicAssistant) -> None:
    """A default picked by the user replaces the adopted one."""
    controller = await _set_up(mass_minimal, _create_plugin())
    controller.config = await mass_minimal.config.get_core_config(CONF_AI)

    await mass_minimal.config.save_core_config(CONF_AI, {"text_engine": "plugin_a/bravo"})

    assert mass_minimal.config.get_raw_core_config_value(CONF_AI, "text_engine") == (
        "plugin_a/bravo"
    )
    assert controller.config.get_value("text_engine") == "plugin_a/bravo"
