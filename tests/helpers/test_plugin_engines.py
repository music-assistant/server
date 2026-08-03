"""Tests for the plugin engine discovery/selection helper."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import ConfigEntryType

from music_assistant.helpers.plugin_engines import (
    ENGINE_AUTO,
    create_ai_engine_config_entries,
    create_tts_engine_config_entries,
    get_ai_engines,
    get_tts_engines,
    resolve_ai_engines,
    resolve_tts_engines,
)
from music_assistant.models.plugin import AIEngine, PluginProvider, TTSEngine


def _create_plugin(instance_id: str) -> MagicMock:
    """Create a mock plugin provider exposing no engines."""
    provider = MagicMock(spec=PluginProvider)
    provider.instance_id = instance_id
    provider.get_ai_engines = AsyncMock(return_value=[])
    provider.get_tts_engines = AsyncMock(return_value=[])
    return provider


def _create_mass(*providers: MagicMock) -> MagicMock:
    """Create a mock MusicAssistant returning the given providers for any feature."""
    mass = MagicMock()
    mass.get_providers_supporting_feature.return_value = list(providers)
    return mass


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


async def test_resolve_auto_returns_all_engines() -> None:
    """An unset/empty/auto selection yields every available engine in order."""
    plugin = _create_plugin("plugin_a")
    plugin.get_ai_engines.return_value = [
        AIEngine(id="alpha", name="Alpha", provider=plugin),
        AIEngine(id="bravo", name="Bravo", provider=plugin),
    ]
    mass = _create_mass(plugin)
    for selected in (None, "", ENGINE_AUTO):
        resolved = await resolve_ai_engines(mass, selected)
        assert [engine.uid for engine in resolved] == ["plugin_a/alpha", "plugin_a/bravo"]


async def test_resolve_concrete_uid_returns_single_engine() -> None:
    """A concrete selection yields only the matching engine."""
    plugin = _create_plugin("plugin_a")
    plugin.get_tts_engines.return_value = [
        TTSEngine(id="alpha", name="Alpha", provider=plugin),
        TTSEngine(id="bravo", name="Bravo", provider=plugin),
    ]
    mass = _create_mass(plugin)
    resolved = await resolve_tts_engines(mass, "plugin_a/bravo")
    assert [engine.uid for engine in resolved] == ["plugin_a/bravo"]


async def test_resolve_vanished_uid_returns_empty() -> None:
    """A selection that no longer exists resolves to nothing instead of another engine."""
    plugin = _create_plugin("plugin_a")
    plugin.get_ai_engines.return_value = [AIEngine(id="alpha", name="Alpha", provider=plugin)]
    mass = _create_mass(plugin)
    assert await resolve_ai_engines(mass, "plugin_a/gone") == []


async def test_config_entries_list_auto_and_engines() -> None:
    """The picker offers the auto sentinel followed by every engine, and no alert."""
    plugin = _create_plugin("plugin_a")
    plugin.get_ai_engines.return_value = [AIEngine(id="alpha", name="Alpha", provider=plugin)]
    mass = _create_mass(plugin)
    entries = await create_ai_engine_config_entries(mass, "ai_engine", depends_on="use_ai")
    assert len(entries) == 1
    entry = entries[0]
    assert entry.key == "ai_engine"
    assert entry.default_value == ENGINE_AUTO
    assert entry.depends_on == "use_ai"
    assert entry.category == "features"
    assert entry.read_only is False
    assert [(option.value, option.title) for option in entry.options] == [
        (ENGINE_AUTO, None),
        ("plugin_a/alpha", "Alpha"),
    ]


async def test_config_entries_without_engines_are_read_only_with_alert() -> None:
    """Without any engine the picker is read-only and an alert entry is appended."""
    mass = _create_mass()
    entries = await create_tts_engine_config_entries(mass, "tts_engine")
    assert len(entries) == 2
    picker, alert = entries
    assert picker.read_only is True
    assert [option.value for option in picker.options] == [ENGINE_AUTO]
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
