"""Helpers to discover, select and configure the AI/TTS engines exposed by plugins."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, ProviderFeature, ProviderType

from music_assistant.constants import MASS_LOGGER_NAME
from music_assistant.models.plugin import (
    AIEngine,
    PluginEngine,
    PluginProvider,
    TTSEngine,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from music_assistant.mass import MusicAssistant

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.helpers.plugin_engines")

# sentinel value meaning "no explicit choice, use whatever is available"
ENGINE_AUTO = "auto"


async def get_ai_engines(mass: MusicAssistant) -> list[AIEngine]:
    """
    Return all AI engines currently exposed by the loaded plugins, in a stable order.

    :param mass: The Music Assistant instance to query.
    """
    return await _collect_engines(
        mass, ProviderFeature.AI_QUERY, lambda provider: provider.get_ai_engines()
    )


async def get_tts_engines(mass: MusicAssistant) -> list[TTSEngine]:
    """
    Return all TTS engines currently exposed by the loaded plugins, in a stable order.

    :param mass: The Music Assistant instance to query.
    """
    return await _collect_engines(
        mass, ProviderFeature.TTS, lambda provider: provider.get_tts_engines()
    )


async def resolve_ai_engines(mass: MusicAssistant, selected: str | None) -> list[AIEngine]:
    """
    Return the AI engines to try for a configured selection, in preference order.

    :param mass: The Music Assistant instance to query.
    :param selected: The configured engine uid, or None/empty/``ENGINE_AUTO`` for automatic
        selection. A concrete uid yields a single-element list, or an empty list when that
        engine no longer exists - it is never silently substituted by another engine.
    """
    return _resolve(await get_ai_engines(mass), selected)


async def resolve_tts_engines(mass: MusicAssistant, selected: str | None) -> list[TTSEngine]:
    """
    Return the TTS engines to try for a configured selection, in preference order.

    :param mass: The Music Assistant instance to query.
    :param selected: The configured engine uid, or None/empty/``ENGINE_AUTO`` for automatic
        selection. A concrete uid yields a single-element list, or an empty list when that
        engine no longer exists - it is never silently substituted by another engine.
    """
    return _resolve(await get_tts_engines(mass), selected)


async def create_ai_engine_config_entries(
    mass: MusicAssistant, key: str, depends_on: str | None = None
) -> tuple[ConfigEntry, ...]:
    """
    Return the config entries letting the user pick an AI engine.

    Adds a second (alert) entry with key ``<key>_unavailable`` when no engine is available.

    :param mass: The Music Assistant instance to query.
    :param key: The config entry key holding the selected engine uid.
    :param depends_on: Optional key of the entry this picker should be shown for.
    """
    return _create_engine_config_entries(await get_ai_engines(mass), key, depends_on)


async def create_tts_engine_config_entries(
    mass: MusicAssistant, key: str, depends_on: str | None = None
) -> tuple[ConfigEntry, ...]:
    """
    Return the config entries letting the user pick a TTS engine.

    Adds a second (alert) entry with key ``<key>_unavailable`` when no engine is available.

    :param mass: The Music Assistant instance to query.
    :param key: The config entry key holding the selected engine uid.
    :param depends_on: Optional key of the entry this picker should be shown for.
    """
    return _create_engine_config_entries(await get_tts_engines(mass), key, depends_on)


async def _collect_engines[EngineT: PluginEngine](
    mass: MusicAssistant,
    feature: ProviderFeature,
    fetch: Callable[[PluginProvider], Coroutine[Any, Any, list[EngineT]]],
) -> list[EngineT]:
    """Collect the engines of every available plugin declaring the given feature."""
    result: list[EngineT] = []
    for provider in mass.get_providers_supporting_feature(feature, priority=(ProviderType.PLUGIN,)):
        if not isinstance(provider, PluginProvider):
            continue
        try:
            engines = await fetch(provider)
        except Exception as err:
            LOGGER.warning(
                "Could not retrieve %s engines from %s: %s",
                feature,
                provider.instance_id,
                err,
            )
            continue
        result.extend(sorted(engines, key=lambda engine: engine.name))
    return result


def _resolve[EngineT: PluginEngine](engines: list[EngineT], selected: str | None) -> list[EngineT]:
    """Narrow the available engines down to the ordered candidates for a configured selection."""
    if not selected or selected == ENGINE_AUTO:
        return engines
    return [engine for engine in engines if engine.uid == selected]


def _create_engine_config_entries(
    engines: list[AIEngine] | list[TTSEngine], key: str, depends_on: str | None
) -> tuple[ConfigEntry, ...]:
    """Build the picker (and unavailable alert) config entries for the given engines."""
    entry = ConfigEntry(
        key=key,
        type=ConfigEntryType.STRING,
        required=False,
        default_value=ENGINE_AUTO,
        options=[
            ConfigValueOption(ENGINE_AUTO),
            *(ConfigValueOption(engine.uid, title=engine.name) for engine in engines),
        ],
        depends_on=depends_on,
        category="features",
        read_only=not engines,
    )
    if engines:
        return (entry,)
    return (
        entry,
        ConfigEntry(
            key=f"{key}_unavailable",
            type=ConfigEntryType.ALERT,
        ),
    )
