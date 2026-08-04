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
    from music_assistant.models.provider import Provider

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.helpers.plugin_engines")


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


async def resolve_ai_engine(mass: MusicAssistant, selected: str | None) -> AIEngine | None:
    """
    Return the AI engine for a configured selection.

    :param mass: The Music Assistant instance to query.
    :param selected: The configured engine uid. Returns None when it is empty/unset or names
        an engine that no longer exists - another engine is never substituted for it.
    """
    return _resolve(await get_ai_engines(mass), selected)


async def resolve_tts_engine(mass: MusicAssistant, selected: str | None) -> TTSEngine | None:
    """
    Return the TTS engine for a configured selection.

    :param mass: The Music Assistant instance to query.
    :param selected: The configured engine uid. Returns None when it is empty/unset or names
        an engine that no longer exists - another engine is never substituted for it.
    """
    return _resolve(await get_tts_engines(mass), selected)


async def select_ai_engine(
    provider: Provider, key: str, *, in_setup_data: bool = False
) -> AIEngine | None:
    """
    Return the provider's AI engine, adopting the first available one when it has none yet.

    A selection that was made explicitly is honoured or, when that engine no longer exists,
    reported as missing - another engine is never substituted for it.

    :param provider: The provider whose selection to read and, if unset, persist.
    :param key: The config key holding the selected engine uid.
    :param in_setup_data: Store in (and read from) the provider's setup_data instead of its
        regular config values.
    """
    return await _select_engine(
        provider, key, await get_ai_engines(provider.mass), in_setup_data=in_setup_data
    )


async def select_tts_engine(
    provider: Provider, key: str, *, in_setup_data: bool = False
) -> TTSEngine | None:
    """
    Return the provider's TTS engine, adopting the first available one when it has none yet.

    A selection that was made explicitly is honoured or, when that engine no longer exists,
    reported as missing - another engine is never substituted for it.

    :param provider: The provider whose selection to read and, if unset, persist.
    :param key: The config key holding the selected engine uid.
    :param in_setup_data: Store in (and read from) the provider's setup_data instead of its
        regular config values.
    """
    return await _select_engine(
        provider, key, await get_tts_engines(provider.mass), in_setup_data=in_setup_data
    )


async def create_ai_engine_config_entries(
    mass: MusicAssistant, key: str, depends_on: str | None = None, required: bool = False
) -> tuple[ConfigEntry, ...]:
    """
    Return the config entries letting the user pick an AI engine.

    Adds a second (alert) entry with key ``<key>_unavailable`` when no engine is available.

    :param mass: The Music Assistant instance to query.
    :param key: The config entry key holding the selected engine uid.
    :param depends_on: Optional key of the entry this picker should be shown for.
    :param required: Reject a submission that leaves the picker empty. Only for setup flows;
        an options entry must stay optional so a provider with no selection still loads.
    """
    return _create_engine_config_entries(await get_ai_engines(mass), key, depends_on, required)


async def create_tts_engine_config_entries(
    mass: MusicAssistant, key: str, depends_on: str | None = None, required: bool = False
) -> tuple[ConfigEntry, ...]:
    """
    Return the config entries letting the user pick a TTS engine.

    Adds a second (alert) entry with key ``<key>_unavailable`` when no engine is available.

    :param mass: The Music Assistant instance to query.
    :param key: The config entry key holding the selected engine uid.
    :param depends_on: Optional key of the entry this picker should be shown for.
    :param required: Reject a submission that leaves the picker empty. Only for setup flows;
        an options entry must stay optional so a provider with no selection still loads.
    """
    return _create_engine_config_entries(await get_tts_engines(mass), key, depends_on, required)


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


def _resolve[EngineT: PluginEngine](engines: list[EngineT], selected: str | None) -> EngineT | None:
    """Return the available engine matching a configured selection, if any."""
    if not selected:
        return None
    return next((engine for engine in engines if engine.uid == selected), None)


async def _select_engine[EngineT: PluginEngine](
    provider: Provider,
    key: str,
    engines: list[EngineT],
    *,
    in_setup_data: bool,
) -> EngineT | None:
    """Return the stored engine, or persist and return the first available one."""
    config = provider.mass.config
    stored = (
        config.get_provider_setup_value(provider.instance_id, key)
        if in_setup_data
        else config.get_raw_provider_config_value(provider.instance_id, key)
    )
    if isinstance(stored, str) and stored:
        return _resolve(engines, stored)
    if not engines:
        return None
    engine = engines[0]
    if in_setup_data:
        provider._update_setup_data(key, engine.uid)
    else:
        provider._update_config_value(key, engine.uid)
    LOGGER.debug("Selected engine %s as %s for %s", engine.uid, key, provider.instance_id)
    return engine


def _create_engine_config_entries(
    engines: list[AIEngine] | list[TTSEngine],
    key: str,
    depends_on: str | None,
    required: bool = False,
) -> tuple[ConfigEntry, ...]:
    """Build the picker (and unavailable alert) config entries for the given engines."""
    entry = ConfigEntry(
        key=key,
        type=ConfigEntryType.STRING,
        required=required,
        # a concrete default would make the seeded selection equal to it and therefore
        # not persisted (to_raw only stores values differing from the default)
        default_value=None,
        # the picker aggregates engines from every plugin, so each option names the
        # plugin it came from - engine names alone are ambiguous across providers
        options=[
            ConfigValueOption(engine.uid, title=f"{engine.provider.name} | {engine.name}")
            for engine in engines
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
            depends_on=depends_on,
        ),
    )
