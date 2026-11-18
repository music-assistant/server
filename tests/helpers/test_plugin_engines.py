"""Tests for the plugin engine discovery/selection helper."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ConfigEntryType

from music_assistant.constants import CONF_AI, CONF_VALUE_GLOBAL
from music_assistant.helpers.plugin_engines import (
    EngineKind,
    create_ai_engine_config_entries,
    create_default_engine_config_entries,
    create_image_engine_config_entries,
    create_tts_engine_config_entries,
    get_ai_engines,
    get_default_engine,
    get_image_engines,
    get_tts_engines,
    has_engine_choice,
    resolve_ai_engine,
    resolve_ai_engine_setting,
    resolve_image_engine,
    resolve_image_engine_setting,
    resolve_tts_engine,
    resolve_tts_engine_setting,
)
from music_assistant.models.plugin import AIEngine, ImageEngine, PluginProvider, TTSEngine

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


def _create_plugin(instance_id: str, name: str | None = None) -> MagicMock:
    """Create a mock plugin provider exposing no engines."""
    provider = MagicMock(spec=PluginProvider)
    provider.instance_id = instance_id
    provider.name = name or instance_id
    provider.get_ai_engines = AsyncMock(return_value=[])
    provider.get_tts_engines = AsyncMock(return_value=[])
    provider.get_image_engines = AsyncMock(return_value=[])
    return provider


def _create_mass(*providers: MagicMock) -> MagicMock:
    """Create a mock MusicAssistant returning the given providers for any feature."""
    mass = MagicMock()
    mass.get_providers_supporting_feature.return_value = list(providers)
    # no default engine is stored
    mass.config.get_raw_core_config_value.return_value = None
    return mass


def _serve(mass: MusicAssistant, *providers: MagicMock) -> None:
    """Let a real MusicAssistant serve the given plugins for any feature."""
    mass.get_providers_supporting_feature = MagicMock(  # type: ignore[method-assign]
        return_value=list(providers)
    )


def _ai_plugin(instance_id: str = "plugin_a", *engine_ids: str) -> MagicMock:
    """Create a mock plugin exposing an AI engine for each id, named after the id."""
    plugin = _create_plugin(instance_id)
    plugin.get_ai_engines.return_value = [
        AIEngine(id=engine_id, name=engine_id.title(), provider=plugin)
        for engine_id in engine_ids or ("alpha", "bravo")
    ]
    return plugin


def test_engine_uid_composes_provider_and_engine_id() -> None:
    """The uid is the owning provider's instance id joined with the engine id."""
    provider = _create_plugin("hass--abc")
    assert AIEngine(id="ai_task.google", name="Google", provider=provider).uid == (
        "hass--abc/ai_task.google"
    )


async def test_engines_ordered_by_provider_then_name() -> None:
    """Provider order is preserved and engines are sorted by name within a provider."""
    first = _create_plugin("plugin_a")
    second = _create_plugin("plugin_b")
    first.get_ai_engines.return_value = [
        AIEngine(id="zulu", name="Zulu", provider=first),
        AIEngine(id="alpha", name="Alpha", provider=first),
    ]
    second.get_ai_engines.return_value = [
        AIEngine(id="mike", name="Mike", provider=second),
    ]
    mass = _create_mass(first, second)
    assert [engine.uid for engine in await get_ai_engines(mass)] == [
        "plugin_a/alpha",
        "plugin_a/zulu",
        "plugin_b/mike",
    ]


async def test_failing_provider_is_skipped() -> None:
    """A provider raising while listing its engines is skipped, not propagated."""
    broken = _create_plugin("broken")
    healthy = _create_plugin("healthy")
    broken.get_ai_engines.side_effect = RuntimeError("boom")
    healthy.get_ai_engines.return_value = [AIEngine(id="one", name="One", provider=healthy)]
    mass = _create_mass(broken, healthy)
    assert [engine.uid for engine in await get_ai_engines(mass)] == ["healthy/one"]


async def test_tts_engines_are_collected() -> None:
    """TTS discovery uses the plugin's TTS engine listing."""
    plugin = _create_plugin("plugin_a")
    plugin.get_tts_engines.return_value = [TTSEngine(id="voice", name="Voice", provider=plugin)]
    mass = _create_mass(plugin)
    assert [engine.uid for engine in await get_tts_engines(mass)] == ["plugin_a/voice"]


async def test_image_engines_are_collected() -> None:
    """Image discovery uses the plugin's image engine listing."""
    plugin = _create_plugin("plugin_a")
    plugin.get_image_engines.return_value = [
        ImageEngine(id="zulu", name="Zulu", provider=plugin),
        ImageEngine(id="draw", name="Draw", provider=plugin),
    ]
    mass = _create_mass(plugin)
    assert [engine.uid for engine in await get_image_engines(mass)] == [
        "plugin_a/draw",
        "plugin_a/zulu",
    ]


@pytest.mark.parametrize("selected", [None, ""])
async def test_resolve_without_a_selection_returns_nothing(selected: str | None) -> None:
    """An unset selection resolves to nothing instead of an arbitrary engine."""
    mass = _create_mass(_ai_plugin())
    assert await resolve_ai_engine(mass, selected) is None
    mass.config.set_raw_core_config_value.assert_not_called()


async def test_resolve_concrete_uid_returns_the_selected_engine() -> None:
    """A concrete selection yields exactly the matching engine."""
    plugin = _create_plugin("plugin_a")
    plugin.get_tts_engines.return_value = [
        TTSEngine(id="alpha", name="Alpha", provider=plugin),
        TTSEngine(id="bravo", name="Bravo", provider=plugin),
    ]
    mass = _create_mass(plugin)
    resolved = await resolve_tts_engine(mass, "plugin_a/bravo")
    assert resolved is not None
    assert resolved.uid == "plugin_a/bravo"


async def test_resolve_vanished_uid_returns_none() -> None:
    """A selection that no longer exists resolves to nothing instead of another engine."""
    mass = _create_mass(_ai_plugin("plugin_a", "alpha"))
    assert await resolve_ai_engine(mass, "plugin_a/gone") is None


async def test_resolve_image_engine_returns_the_selected_engine() -> None:
    """An image selection yields exactly the matching image engine."""
    plugin = _create_plugin("plugin_a")
    plugin.get_image_engines.return_value = [ImageEngine(id="draw", name="Draw", provider=plugin)]
    mass = _create_mass(plugin)
    resolved = await resolve_image_engine(mass, "plugin_a/draw")
    assert resolved is not None
    assert resolved.uid == "plugin_a/draw"
    assert await resolve_image_engine(mass, "plugin_a/gone") is None


async def test_default_adopts_and_stores_the_first_engine(mass_minimal: MusicAssistant) -> None:
    """Without a stored default the first engine is adopted and kept as the default."""
    _serve(mass_minimal, _ai_plugin())

    engine = await get_default_engine(mass_minimal, EngineKind.TEXT)

    assert engine is not None
    assert engine.uid == "plugin_a/alpha"
    assert mass_minimal.config.get_raw_core_config_value(CONF_AI, "text_engine") == (
        "plugin_a/alpha"
    )
    assert mass_minimal.config.get(f"core/{CONF_AI}/domain") == CONF_AI


async def test_default_honours_the_stored_engine(mass_minimal: MusicAssistant) -> None:
    """A stored default is returned instead of the first engine."""
    _serve(mass_minimal, _ai_plugin())
    mass_minimal.config.set_raw_core_config_value(CONF_AI, "text_engine", "plugin_a/bravo")

    engine = await get_default_engine(mass_minimal, EngineKind.TEXT)

    assert engine is not None
    assert engine.uid == "plugin_a/bravo"


async def test_default_that_vanished_does_not_fall_back(mass_minimal: MusicAssistant) -> None:
    """A stored default that no longer exists is reported missing, never swapped."""
    _serve(mass_minimal, _ai_plugin())
    mass_minimal.config.set_raw_core_config_value(CONF_AI, "text_engine", "plugin_a/gone")

    assert await get_default_engine(mass_minimal, EngineKind.TEXT) is None
    assert mass_minimal.config.get_raw_core_config_value(CONF_AI, "text_engine") == (
        "plugin_a/gone"
    )


async def test_default_without_engines_stores_nothing(mass_minimal: MusicAssistant) -> None:
    """Without any engine nothing is stored, so a later engine is still adopted."""
    _serve(mass_minimal)

    assert await get_default_engine(mass_minimal, EngineKind.SPEECH) is None
    assert mass_minimal.config.get_raw_core_config_value(CONF_AI, "speech_engine") is None


@pytest.mark.parametrize("value", [None, "", CONF_VALUE_GLOBAL])
async def test_setting_that_follows_the_default_resolves_to_it(
    mass_minimal: MusicAssistant, value: str | None
) -> None:
    """A setting without a pick of its own resolves to the default engine of its kind."""
    _serve(mass_minimal, _ai_plugin())
    mass_minimal.config.set_raw_core_config_value(CONF_AI, "text_engine", "plugin_a/bravo")

    engine = await resolve_ai_engine_setting(mass_minimal, value)

    assert engine is not None
    assert engine.uid == "plugin_a/bravo"


async def test_setting_with_a_pick_ignores_the_default(mass_minimal: MusicAssistant) -> None:
    """A setting naming an engine resolves to exactly that engine."""
    _serve(mass_minimal, _ai_plugin())
    mass_minimal.config.set_raw_core_config_value(CONF_AI, "text_engine", "plugin_a/bravo")

    engine = await resolve_ai_engine_setting(mass_minimal, "plugin_a/alpha")

    assert engine is not None
    assert engine.uid == "plugin_a/alpha"


async def test_setting_with_a_vanished_pick_does_not_fall_back(
    mass_minimal: MusicAssistant,
) -> None:
    """A picked engine that no longer exists resolves to nothing, not to the default."""
    _serve(mass_minimal, _ai_plugin())

    assert await resolve_ai_engine_setting(mass_minimal, "plugin_a/gone") is None
    assert mass_minimal.config.get_raw_core_config_value(CONF_AI, "text_engine") is None


async def test_setting_follows_the_default_of_its_own_kind(mass_minimal: MusicAssistant) -> None:
    """The speech and image settings follow their own default, not the text one."""
    plugin = _ai_plugin()
    plugin.get_tts_engines.return_value = [TTSEngine(id="voice", name="Voice", provider=plugin)]
    plugin.get_image_engines.return_value = [ImageEngine(id="draw", name="Draw", provider=plugin)]
    _serve(mass_minimal, plugin)

    tts_engine = await resolve_tts_engine_setting(mass_minimal, CONF_VALUE_GLOBAL)
    image_engine = await resolve_image_engine_setting(mass_minimal, None)

    assert tts_engine is not None
    assert tts_engine.uid == "plugin_a/voice"
    assert image_engine is not None
    assert image_engine.uid == "plugin_a/draw"
    config = mass_minimal.config
    assert config.get_raw_core_config_value(CONF_AI, "speech_engine") == "plugin_a/voice"
    assert config.get_raw_core_config_value(CONF_AI, "image_engine") == "plugin_a/draw"
    assert config.get_raw_core_config_value(CONF_AI, "text_engine") is None


async def test_engine_choice(mass_minimal: MusicAssistant) -> None:
    """A pick always chooses an engine, following the default only once one is set."""
    assert has_engine_choice(mass_minimal, EngineKind.TEXT, "plugin_a/alpha") is True
    assert has_engine_choice(mass_minimal, EngineKind.TEXT, CONF_VALUE_GLOBAL) is False
    assert has_engine_choice(mass_minimal, EngineKind.TEXT, None) is False

    mass_minimal.config.set_raw_core_config_value(CONF_AI, "text_engine", "plugin_a/gone")

    assert has_engine_choice(mass_minimal, EngineKind.TEXT, CONF_VALUE_GLOBAL) is True
    assert has_engine_choice(mass_minimal, EngineKind.IMAGE, CONF_VALUE_GLOBAL) is False


async def test_config_entries_follow_the_default_engine() -> None:
    """The picker defaults to following the default engine, offered first under its name."""
    mass = _create_mass(_ai_plugin("plugin_a", "alpha"))
    mass.config.get_raw_core_config_value.return_value = "plugin_a/alpha"

    entries = await create_ai_engine_config_entries(mass, "ai_engine", depends_on="use_ai")

    assert len(entries) == 1
    entry = entries[0]
    assert entry.key == "ai_engine"
    assert entry.default_value == CONF_VALUE_GLOBAL
    assert entry.depends_on == "use_ai"
    assert entry.category == "features"
    assert entry.hidden is False
    assert [(option.value, option.title) for option in entry.options] == [
        (CONF_VALUE_GLOBAL, "plugin_a | Alpha"),
        ("plugin_a/alpha", "plugin_a | Alpha"),
    ]


async def test_config_entries_adopt_a_default_to_name(mass_minimal: MusicAssistant) -> None:
    """Without a default yet, the picker names the engine adopted as the default."""
    _serve(mass_minimal, _ai_plugin())

    entries = await create_ai_engine_config_entries(mass_minimal, "ai_engine")

    assert entries[0].options[0].title == "plugin_a | Alpha"
    assert mass_minimal.config.get_raw_core_config_value(CONF_AI, "text_engine") == (
        "plugin_a/alpha"
    )


async def test_config_entries_name_the_plugin_each_engine_came_from() -> None:
    """Engines from different plugins stay distinguishable even when named the same."""
    first = _create_plugin("plugin_a", name="Home Assistant")
    second = _create_plugin("plugin_b", name="Chatterbox")
    first.get_ai_engines.return_value = [AIEngine(id="shared", name="Shared", provider=first)]
    second.get_ai_engines.return_value = [AIEngine(id="shared", name="Shared", provider=second)]
    mass = _create_mass(first, second)

    entries = await create_ai_engine_config_entries(mass, "ai_engine")

    assert [(option.value, option.title) for option in entries[0].options[1:]] == [
        ("plugin_a/shared", "Home Assistant | Shared"),
        ("plugin_b/shared", "Chatterbox | Shared"),
    ]


async def test_config_entries_without_engines_are_hidden_with_alert() -> None:
    """Without any engine the picker is hidden and an alert entry is appended."""
    mass = _create_mass()
    entries = await create_tts_engine_config_entries(mass, "tts_engine")
    assert len(entries) == 2
    picker, alert = entries
    assert picker.hidden is True
    assert picker.default_value == CONF_VALUE_GLOBAL
    assert alert.key == "tts_engine_unavailable"
    assert alert.type == ConfigEntryType.ALERT


async def test_create_config_entries_alert_follows_picker_visibility() -> None:
    """Test the unavailable alert is gated by the same entry as the picker."""
    mass = _create_mass()
    picker, alert = await create_ai_engine_config_entries(
        mass, "ai_engine", depends_on="ai_descriptions"
    )
    assert picker.depends_on == "ai_descriptions"
    assert alert.depends_on == "ai_descriptions"


async def test_image_config_entries_alert_follows_picker_value_gate() -> None:
    """The image picker's value gate is shared by its unavailable alert."""
    mass = _create_mass()
    picker, alert = await create_image_engine_config_entries(
        mass, "ai_image_engine", depends_on="template", depends_on_value="ai", category="generic"
    )
    assert (picker.depends_on, picker.depends_on_value) == ("template", "ai")
    assert (alert.depends_on, alert.depends_on_value) == ("template", "ai")
    assert alert.key == "ai_image_engine_unavailable"
    assert alert.translation_key == "image_engine_unavailable"
    assert alert.category == "generic"


async def test_alert_reuses_the_shared_source_string() -> None:
    """However the picker is keyed, its alert resolves to the one shared alert string."""
    mass = _create_mass()
    _picker, alert = await create_tts_engine_config_entries(mass, "announce_tts_engine")
    assert alert.key == "announce_tts_engine_unavailable"
    assert alert.translation_key == "tts_engine_unavailable"


async def test_config_entries_warn_when_the_default_is_unavailable() -> None:
    """A stored default that is gone is named by its uid and announced while followed."""
    mass = _create_mass(_ai_plugin("plugin_a", "alpha"))
    mass.config.get_raw_core_config_value.return_value = "plugin_b/gone"

    picker, alert = await create_ai_engine_config_entries(
        mass, "ai_engine", depends_on="ai_descriptions"
    )

    assert picker.options[0].title == "plugin_b/gone"
    assert alert.type == ConfigEntryType.ALERT
    assert alert.key == "ai_engine_default_unavailable"
    assert alert.translation_key == "default_engine_unavailable"
    assert (alert.depends_on, alert.depends_on_value) == ("ai_engine", CONF_VALUE_GLOBAL)
    mass.config.set_raw_core_config_value.assert_not_called()


async def test_default_engine_entries_offer_the_engines(mass_minimal: MusicAssistant) -> None:
    """The default picker offers only the engines and shows the adopted default."""
    _serve(mass_minimal, _ai_plugin())

    entries = await create_default_engine_config_entries(mass_minimal, EngineKind.TEXT)

    assert len(entries) == 1
    picker = entries[0]
    assert picker.key == "text_engine"
    assert picker.default_value is None
    assert picker.required is True
    assert picker.hidden is False
    assert picker.category == "generic"
    assert [option.value for option in picker.options] == ["plugin_a/alpha", "plugin_a/bravo"]
    assert mass_minimal.config.get_raw_core_config_value(CONF_AI, "text_engine") == (
        "plugin_a/alpha"
    )


async def test_default_engine_entries_without_engines(mass_minimal: MusicAssistant) -> None:
    """Without engines the default picker is optional and hidden behind the shared alert."""
    _serve(mass_minimal)

    picker, alert = await create_default_engine_config_entries(mass_minimal, EngineKind.IMAGE)

    assert picker.key == "image_engine"
    assert picker.required is False
    assert picker.hidden is True
    assert alert.key == "image_engine_unavailable"
    assert alert.translation_key == "image_engine_unavailable"


async def test_default_engine_entries_warn_when_it_is_unavailable(
    mass_minimal: MusicAssistant,
) -> None:
    """A stored default that is gone is kept and announced until another one is picked."""
    _serve(mass_minimal, _ai_plugin())
    mass_minimal.config.set_raw_core_config_value(CONF_AI, "text_engine", "plugin_b/gone")

    picker, alert = await create_default_engine_config_entries(mass_minimal, EngineKind.TEXT)

    assert picker.required is True
    assert alert.key == "text_engine_default_unavailable"
    assert alert.translation_key == "selected_engine_unavailable"
    assert (alert.depends_on, alert.depends_on_value) == ("text_engine", "plugin_b/gone")
    assert mass_minimal.config.get_raw_core_config_value(CONF_AI, "text_engine") == (
        "plugin_b/gone"
    )
