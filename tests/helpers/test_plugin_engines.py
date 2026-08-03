"""Tests for the plugin engine discovery/selection helper."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.config_entries import ProviderConfig
from music_assistant_models.enums import ConfigEntryType, ProviderType
from music_assistant_models.provider import ProviderManifest

from music_assistant.constants import CONF_PROVIDERS
from music_assistant.helpers.plugin_engines import (
    create_ai_engine_config_entries,
    create_tts_engine_config_entries,
    get_ai_engines,
    get_tts_engines,
    resolve_ai_engine,
    resolve_tts_engine,
    select_ai_engine,
    select_tts_engine,
)
from music_assistant.models.plugin import AIEngine, PluginProvider, TTSEngine

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

CONSUMER_INSTANCE = "consumer--test"


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


def _create_consumer(
    mass: MusicAssistant, *providers: MagicMock, values: dict[str, Any] | None = None
) -> PluginProvider:
    """Create a stored consumer provider on a real config controller, serving the plugins."""
    mass.config.set(
        f"{CONF_PROVIDERS}/{CONSUMER_INSTANCE}",
        {
            "type": ProviderType.PLUGIN.value,
            "domain": "consumer",
            "instance_id": CONSUMER_INSTANCE,
            "values": dict(values or {}),
        },
    )
    mass.get_providers_supporting_feature = MagicMock(  # type: ignore[method-assign]
        return_value=list(providers)
    )
    manifest = ProviderManifest(
        type=ProviderType.PLUGIN,
        domain="consumer",
        name="Consumer",
        description="",
        codeowners=[],
    )
    config = ProviderConfig(
        values={},
        type=ProviderType.PLUGIN,
        domain="consumer",
        instance_id=CONSUMER_INSTANCE,
    )
    return PluginProvider(mass, manifest, config)


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


@pytest.mark.parametrize("selected", [None, ""])
async def test_resolve_without_a_selection_returns_nothing(selected: str | None) -> None:
    """An unset selection resolves to nothing instead of an arbitrary engine."""
    plugin = _create_plugin("plugin_a")
    plugin.get_ai_engines.return_value = [
        AIEngine(id="alpha", name="Alpha", provider=plugin),
        AIEngine(id="bravo", name="Bravo", provider=plugin),
    ]
    mass = _create_mass(plugin)
    assert await resolve_ai_engine(mass, selected) is None


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
    plugin = _create_plugin("plugin_a")
    plugin.get_ai_engines.return_value = [AIEngine(id="alpha", name="Alpha", provider=plugin)]
    mass = _create_mass(plugin)
    assert await resolve_ai_engine(mass, "plugin_a/gone") is None


async def test_select_stores_the_first_engine_in_values(mass_minimal: MusicAssistant) -> None:
    """A consumer without a stored selection adopts the first engine as a concrete uid."""
    plugin = _create_plugin("plugin_a")
    plugin.get_ai_engines.return_value = [
        AIEngine(id="alpha", name="Alpha", provider=plugin),
        AIEngine(id="bravo", name="Bravo", provider=plugin),
    ]
    consumer = _create_consumer(mass_minimal, plugin)

    engine = await select_ai_engine(consumer, "ai_engine")
    assert engine is not None
    assert engine.uid == "plugin_a/alpha"

    assert (
        mass_minimal.config.get_raw_provider_config_value(CONSUMER_INSTANCE, "ai_engine")
        == "plugin_a/alpha"
    )


async def test_select_stores_the_first_engine_encrypted_in_setup_data(
    mass_minimal: MusicAssistant,
) -> None:
    """The setup_data variant persists the uid encrypted at rest, like the setup flows do."""
    plugin = _create_plugin("plugin_a")
    plugin.get_tts_engines.return_value = [TTSEngine(id="voice", name="Voice", provider=plugin)]
    consumer = _create_consumer(mass_minimal, plugin)

    engine = await select_tts_engine(consumer, "tts_engine", in_setup_data=True)

    assert engine is not None
    assert engine.uid == "plugin_a/voice"
    stored = mass_minimal.config.get(f"{CONF_PROVIDERS}/{CONSUMER_INSTANCE}/setup_data/tts_engine")
    assert stored != "plugin_a/voice"
    assert mass_minimal.config.decrypt_string(stored) == "plugin_a/voice"
    assert (
        mass_minimal.config.get_provider_setup_value(CONSUMER_INSTANCE, "tts_engine")
        == "plugin_a/voice"
    )


async def test_select_keeps_an_existing_selection(mass_minimal: MusicAssistant) -> None:
    """An already stored selection is returned untouched, never replaced by the first engine."""
    plugin = _create_plugin("plugin_a")
    plugin.get_ai_engines.return_value = [
        AIEngine(id="alpha", name="Alpha", provider=plugin),
        AIEngine(id="bravo", name="Bravo", provider=plugin),
    ]
    consumer = _create_consumer(mass_minimal, plugin, values={"ai_engine": "plugin_a/bravo"})

    engine = await select_ai_engine(consumer, "ai_engine")
    assert engine is not None
    assert engine.uid == "plugin_a/bravo"

    assert (
        mass_minimal.config.get_raw_provider_config_value(CONSUMER_INSTANCE, "ai_engine")
        == "plugin_a/bravo"
    )


async def test_select_without_engines_stores_nothing(mass_minimal: MusicAssistant) -> None:
    """Without any engine nothing is stored, so a later load can still seed a real one."""
    consumer = _create_consumer(mass_minimal)

    assert await select_ai_engine(consumer, "ai_engine") is None

    assert mass_minimal.config.get_raw_provider_config_value(CONSUMER_INSTANCE, "ai_engine") is None


async def test_selection_that_vanished_does_not_fall_back(
    mass_minimal: MusicAssistant,
) -> None:
    """A stored engine that disappeared is reported missing, not swapped for another."""
    plugin = _create_plugin("plugin_a")
    plugin.get_ai_engines.return_value = [AIEngine(id="alpha", name="Alpha", provider=plugin)]
    consumer = _create_consumer(mass_minimal, plugin, values={"ai_engine": "plugin_a/gone"})

    assert await select_ai_engine(consumer, "ai_engine") is None

    assert (
        mass_minimal.config.get_raw_provider_config_value(CONSUMER_INSTANCE, "ai_engine")
        == "plugin_a/gone"
    )


async def test_config_entries_list_the_concrete_engines() -> None:
    """The picker offers the available engines only, with no preselected default."""
    plugin = _create_plugin("plugin_a")
    plugin.get_ai_engines.return_value = [AIEngine(id="alpha", name="Alpha", provider=plugin)]
    mass = _create_mass(plugin)
    entries = await create_ai_engine_config_entries(mass, "ai_engine", depends_on="use_ai")
    assert len(entries) == 1
    entry = entries[0]
    assert entry.key == "ai_engine"
    assert entry.default_value is None
    assert entry.depends_on == "use_ai"
    assert entry.category == "features"
    assert entry.read_only is False
    assert [(option.value, option.title) for option in entry.options] == [
        ("plugin_a/alpha", "Alpha"),
    ]


async def test_config_entries_without_engines_are_read_only_with_alert() -> None:
    """Without any engine the picker is read-only and an alert entry is appended."""
    mass = _create_mass()
    entries = await create_tts_engine_config_entries(mass, "tts_engine")
    assert len(entries) == 2
    picker, alert = entries
    assert picker.read_only is True
    assert picker.options == []
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
