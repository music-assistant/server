"""Tests for the AI core controller holding the default engines."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ConfigEntryType

from music_assistant.constants import CONF_AI, CONF_CORE, CONF_PROVIDERS
from music_assistant.controllers.ai import AIController
from music_assistant.controllers.players.controller import PlayerController
from music_assistant.helpers.plugin_engines import EngineKind, EngineSetting
from music_assistant.models.plugin import AIEngine, PluginProvider, TTSEngine
from music_assistant.providers.ai_radio.provider import AIRadioProvider
from music_assistant.providers.music_quiz import MusicQuizPlugin
from music_assistant.providers.playlist_metadata import PlaylistMetadataProvider
from music_assistant.providers.smart_playlist import SmartPlaylistProvider

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


def _create_owner(
    instance_id: str, name: str, engine_settings: tuple[EngineSetting, ...]
) -> MagicMock:
    """Create a mock loaded provider declaring the given engine settings."""
    provider = MagicMock(spec=PluginProvider)
    provider.instance_id = instance_id
    provider.domain = instance_id
    provider.name = name
    provider.engine_settings = engine_settings
    return provider


def _store_provider(
    mass: MusicAssistant,
    instance_id: str,
    values: dict[str, Any],
    setup_data: dict[str, Any] | None = None,
) -> None:
    """Store the config of a provider instance."""
    mass.config.set(
        f"{CONF_PROVIDERS}/{instance_id}",
        {"instance_id": instance_id, "values": values, "setup_data": setup_data or {}},
    )


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


async def test_engines_of_one_kind(mass_minimal: MusicAssistant) -> None:
    """The engines of the asked kind are listed with their plugin."""
    controller = await _set_up(mass_minimal, _create_plugin())

    assert await controller.get_engines(EngineKind.SPEECH) == [
        {
            "uid": "plugin_a/voice",
            "name": "Plugin A | Voice",
            "kind": EngineKind.SPEECH,
            "provider": "plugin_a",
        },
    ]


async def test_engines_of_all_kinds(mass_minimal: MusicAssistant) -> None:
    """Without a kind, the text, speech and image engines are listed in that order."""
    controller = await _set_up(mass_minimal, _create_plugin())

    assert [(engine["kind"], engine["uid"]) for engine in await controller.get_engines()] == [
        (EngineKind.TEXT, "plugin_a/alpha"),
        (EngineKind.TEXT, "plugin_a/bravo"),
        (EngineKind.SPEECH, "plugin_a/voice"),
    ]


async def test_usage_lists_the_engine_settings(mass_minimal: MusicAssistant) -> None:
    """Each declared setting is listed with the engine it selects, core modules first."""
    # every configurable core module must be there to be asked for its settings
    await mass_minimal._load_core_controllers()
    controller = await _set_up(mass_minimal, _create_plugin())
    quiz = _create_owner("music_quiz", "Music Quiz", (EngineSetting(EngineKind.TEXT, "ai_engine"),))
    radio = _create_owner(
        "ai_radio",
        "AI Radio",
        (
            EngineSetting(EngineKind.TEXT, "ai_engine", in_setup_data=True),
            EngineSetting(EngineKind.SPEECH, "tts_engine", in_setup_data=True),
        ),
    )
    plain = _create_owner("plain", "Plain", ())
    mass_minimal.get_providers = MagicMock(  # type: ignore[method-assign]
        return_value=[quiz, radio, plain]
    )
    _store_provider(mass_minimal, "music_quiz", values={"ai_engine": "plugin_a/bravo"})
    _store_provider(
        mass_minimal,
        "ai_radio",
        # the options value of the same key is not the one the setup flow set
        values={"ai_engine": "plugin_a/bravo"},
        setup_data={"ai_engine": "global", "tts_engine": "plugin_a/gone"},
    )

    assert await controller.get_usage() == [
        {
            "owner_type": "core",
            "owner": "players",
            "domain": "players",
            "name": "Player Controller",
            "key": "announce_tts_engine",
            "kind": EngineKind.SPEECH,
            "in_setup_data": False,
            "follows_default": True,
            "engine": "plugin_a/voice",
        },
        {
            "owner_type": "provider",
            "owner": "ai_radio",
            "domain": "ai_radio",
            "name": "AI Radio",
            "key": "ai_engine",
            "kind": EngineKind.TEXT,
            "in_setup_data": True,
            "follows_default": True,
            "engine": "plugin_a/alpha",
        },
        {
            "owner_type": "provider",
            "owner": "ai_radio",
            "domain": "ai_radio",
            "name": "AI Radio",
            "key": "tts_engine",
            "kind": EngineKind.SPEECH,
            "in_setup_data": True,
            "follows_default": False,
            "engine": None,
        },
        {
            "owner_type": "provider",
            "owner": "music_quiz",
            "domain": "music_quiz",
            "name": "Music Quiz",
            "key": "ai_engine",
            "kind": EngineKind.TEXT,
            "in_setup_data": False,
            "follows_default": False,
            "engine": "plugin_a/bravo",
        },
    ]


@pytest.mark.parametrize(
    ("owner", "expected"),
    [
        (PlayerController, (EngineSetting(EngineKind.SPEECH, "announce_tts_engine"),)),
        (
            AIRadioProvider,
            (
                EngineSetting(EngineKind.TEXT, "ai_engine", in_setup_data=True),
                EngineSetting(EngineKind.SPEECH, "tts_engine", in_setup_data=True),
            ),
        ),
        (SmartPlaylistProvider, (EngineSetting(EngineKind.TEXT, "ai_engine"),)),
        (MusicQuizPlugin, (EngineSetting(EngineKind.TEXT, "ai_engine"),)),
        (
            PlaylistMetadataProvider,
            (
                EngineSetting(EngineKind.IMAGE, "ai_image_engine"),
                EngineSetting(EngineKind.TEXT, "ai_text_engine"),
            ),
        ),
    ],
)
def test_features_declare_their_engine_settings(
    owner: type[Any], expected: tuple[EngineSetting, ...]
) -> None:
    """Every feature using an engine declares its engine settings for the usage overview."""
    assert owner.engine_settings == expected
