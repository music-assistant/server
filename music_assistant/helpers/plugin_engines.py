"""Helpers to discover, select and configure the AI/TTS/image engines exposed by plugins."""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, ProviderFeature, ProviderType

from music_assistant.constants import (
    CONF_AI,
    CONF_DEFAULT_IMAGE_ENGINE,
    CONF_DEFAULT_SPEECH_ENGINE,
    CONF_DEFAULT_TEXT_ENGINE,
    CONF_VALUE_GLOBAL,
    MASS_LOGGER_NAME,
)
from music_assistant.models.plugin import (
    AIEngine,
    ImageEngine,
    PluginEngine,
    PluginProvider,
    TTSEngine,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Sequence

    from music_assistant_models.config_entries import ConfigValueType

    from music_assistant.mass import MusicAssistant

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.helpers.plugin_engines")


class EngineKind(StrEnum):
    """The kinds of engine: text (AIEngine), speech (TTSEngine) and image (ImageEngine)."""

    TEXT = "text"
    SPEECH = "speech"
    IMAGE = "image"


# the AI core module config key holding the default engine of each kind
_DEFAULT_ENGINE_KEYS: Final[dict[EngineKind, str]] = {
    EngineKind.TEXT: CONF_DEFAULT_TEXT_ENGINE,
    EngineKind.SPEECH: CONF_DEFAULT_SPEECH_ENGINE,
    EngineKind.IMAGE: CONF_DEFAULT_IMAGE_ENGINE,
}
# the alert texts do not depend on what a picker is used for, so every owner shares
# one source string per alert instead of authoring its own copy
_UNAVAILABLE_TRANSLATION_KEYS: Final[dict[EngineKind, str]] = {
    EngineKind.TEXT: "ai_engine_unavailable",
    EngineKind.SPEECH: "tts_engine_unavailable",
    EngineKind.IMAGE: "image_engine_unavailable",
}
_DEFAULT_UNAVAILABLE_TRANSLATION_KEY: Final[str] = "default_engine_unavailable"
_SELECTED_UNAVAILABLE_TRANSLATION_KEY: Final[str] = "selected_engine_unavailable"


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


async def get_image_engines(mass: MusicAssistant) -> list[ImageEngine]:
    """
    Return all image generation engines currently exposed by the loaded plugins, in a stable order.

    :param mass: The Music Assistant instance to query.
    """
    # image generation has no ProviderFeature of its own (the enum lives in the models
    # package), so it is offered by the AI plugins that expose any image engines
    return await _collect_engines(
        mass, ProviderFeature.AI_QUERY, lambda provider: provider.get_image_engines()
    )


def engine_display_name(engine: PluginEngine) -> str:
    """
    Return the name to show for an engine, naming the plugin it comes from.

    :param engine: The engine to name. Engine names alone are ambiguous, since the same
        name can be exposed by more than one plugin.
    """
    return f"{engine.provider.name} | {engine.name}"


async def get_default_engine(mass: MusicAssistant, kind: EngineKind) -> PluginEngine | None:
    """
    Return the default engine of a kind, adopting the first available one when none is set.

    A default that was set is honoured or, when that engine no longer exists, reported as
    missing - another engine is never substituted for it.

    :param mass: The Music Assistant instance to query.
    :param kind: The kind of engine to return the default of.
    """
    return _default_engine(mass, kind, await _get_engines(mass, kind))


def has_engine_choice(mass: MusicAssistant, kind: EngineKind, value: ConfigValueType) -> bool:
    """
    Return whether a stored feature setting chooses an engine at all.

    That is an explicit engine, or the default engine while one is set. A setting that
    follows the default before any default was set chooses none.

    :param mass: The Music Assistant instance to query.
    :param kind: The kind of engine the setting selects.
    :param value: The stored setting, as passed to the ``resolve_*_engine_setting`` helpers.
    """
    return not _follows_default(value) or bool(_stored_default(mass, kind))


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


async def resolve_image_engine(mass: MusicAssistant, selected: str | None) -> ImageEngine | None:
    """
    Return the image generation engine for a configured selection.

    :param mass: The Music Assistant instance to query.
    :param selected: The configured engine uid. Returns None when it is empty/unset or names
        an engine that no longer exists - another engine is never substituted for it.
    """
    return _resolve(await get_image_engines(mass), selected)


async def resolve_ai_engine_setting(
    mass: MusicAssistant, value: ConfigValueType
) -> AIEngine | None:
    """
    Return the AI engine a stored feature setting selects.

    :param mass: The Music Assistant instance to query.
    :param value: The stored setting: an engine uid, or unset/empty/"global" to follow the
        default AI engine. Returns None when the selected engine is not available - another
        engine is never substituted for it.
    """
    return _resolve_setting(mass, EngineKind.TEXT, await get_ai_engines(mass), value)


async def resolve_tts_engine_setting(
    mass: MusicAssistant, value: ConfigValueType
) -> TTSEngine | None:
    """
    Return the TTS engine a stored feature setting selects.

    :param mass: The Music Assistant instance to query.
    :param value: The stored setting: an engine uid, or unset/empty/"global" to follow the
        default TTS engine. Returns None when the selected engine is not available - another
        engine is never substituted for it.
    """
    return _resolve_setting(mass, EngineKind.SPEECH, await get_tts_engines(mass), value)


async def resolve_image_engine_setting(
    mass: MusicAssistant, value: ConfigValueType
) -> ImageEngine | None:
    """
    Return the image generation engine a stored feature setting selects.

    :param mass: The Music Assistant instance to query.
    :param value: The stored setting: an engine uid, or unset/empty/"global" to follow the
        default image engine. Returns None when the selected engine is not available - another
        engine is never substituted for it.
    """
    return _resolve_setting(mass, EngineKind.IMAGE, await get_image_engines(mass), value)


async def create_ai_engine_config_entries(
    mass: MusicAssistant,
    key: str,
    depends_on: str | None = None,
    required: bool = False,
    category: str = "features",
) -> tuple[ConfigEntry, ...]:
    """
    Return the config entries letting the user pick an AI engine for a feature.

    The picker follows the default AI engine unless the user picks one. Adds an alert entry
    with key ``<key>_unavailable`` when no engine is available, and one with key
    ``<key>_default_unavailable`` when the default engine is not.

    :param mass: The Music Assistant instance to query.
    :param key: The config entry key holding the setting, see resolve_ai_engine_setting.
    :param depends_on: Optional key of the entry this picker should be shown for.
    :param required: Reject a submission that leaves the picker empty. Only for setup flows;
        an options entry must stay optional so a provider with no selection still loads.
    :param category: The settings category to show the entries under.
    """
    return _create_feature_engine_config_entries(
        mass, EngineKind.TEXT, await get_ai_engines(mass), key, depends_on, required, category
    )


async def create_tts_engine_config_entries(
    mass: MusicAssistant,
    key: str,
    depends_on: str | None = None,
    required: bool = False,
    category: str = "features",
) -> tuple[ConfigEntry, ...]:
    """
    Return the config entries letting the user pick a TTS engine for a feature.

    The picker follows the default TTS engine unless the user picks one. Adds an alert entry
    with key ``<key>_unavailable`` when no engine is available, and one with key
    ``<key>_default_unavailable`` when the default engine is not.

    :param mass: The Music Assistant instance to query.
    :param key: The config entry key holding the setting, see resolve_tts_engine_setting.
    :param depends_on: Optional key of the entry this picker should be shown for.
    :param required: Reject a submission that leaves the picker empty. Only for setup flows;
        an options entry must stay optional so a provider with no selection still loads.
    :param category: The settings category to show the entries under.
    """
    return _create_feature_engine_config_entries(
        mass, EngineKind.SPEECH, await get_tts_engines(mass), key, depends_on, required, category
    )


async def create_image_engine_config_entries(
    mass: MusicAssistant,
    key: str,
    depends_on: str | None = None,
    required: bool = False,
    category: str = "features",
    depends_on_value: ConfigValueType | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return the config entries letting the user pick an image generation engine for a feature.

    The picker follows the default image engine unless the user picks one. Adds an alert
    entry with key ``<key>_unavailable`` when no engine is available, and one with key
    ``<key>_default_unavailable`` when the default engine is not.

    :param mass: The Music Assistant instance to query.
    :param key: The config entry key holding the setting, see resolve_image_engine_setting.
    :param depends_on: Optional key of the entry this picker should be shown for.
    :param depends_on_value: Optional value of the depends_on entry this picker is shown for,
        instead of for any set value.
    :param required: Reject a submission that leaves the picker empty. Only for setup flows;
        an options entry must stay optional so a provider with no selection still loads.
    :param category: The settings category to show the entries under.
    """
    return _create_feature_engine_config_entries(
        mass,
        EngineKind.IMAGE,
        await get_image_engines(mass),
        key,
        depends_on,
        required,
        category,
        depends_on_value=depends_on_value,
    )


async def create_default_engine_config_entries(
    mass: MusicAssistant, kind: EngineKind
) -> tuple[ConfigEntry, ...]:
    """
    Return the config entries letting the user pick the default engine of a kind.

    When no default is set yet, the first available engine is adopted as the default.
    Adds an alert entry with key ``<key>_unavailable`` when no engine is available, and one
    with key ``<key>_default_unavailable`` when the selected default is not.

    :param mass: The Music Assistant instance to query.
    :param kind: The kind of engine to pick the default of.
    """
    engines = await _get_engines(mass, kind)
    default = _default_engine(mass, kind, engines)
    picker = ConfigEntry(
        key=_DEFAULT_ENGINE_KEYS[kind],
        type=ConfigEntryType.STRING,
        # every kind with engines keeps a default, while a kind without any still saves
        required=bool(engines),
        options=_engine_options(engines),
        category="generic",
        hidden=not engines,
    )
    missing_value = _stored_default(mass, kind) if engines and default is None else None
    return (
        picker,
        *_engine_alerts(
            kind, picker, engines, missing_value, _SELECTED_UNAVAILABLE_TRANSLATION_KEY
        ),
    )


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


async def _get_engines(mass: MusicAssistant, kind: EngineKind) -> Sequence[PluginEngine]:
    """Return the available engines of a kind."""
    if kind == EngineKind.TEXT:
        return await get_ai_engines(mass)
    if kind == EngineKind.SPEECH:
        return await get_tts_engines(mass)
    return await get_image_engines(mass)


def _resolve[EngineT: PluginEngine](
    engines: Sequence[EngineT], selected: str | None
) -> EngineT | None:
    """Return the available engine matching a configured selection, if any."""
    if not selected:
        return None
    return next((engine for engine in engines if engine.uid == selected), None)


def _resolve_setting[EngineT: PluginEngine](
    mass: MusicAssistant, kind: EngineKind, engines: Sequence[EngineT], value: ConfigValueType
) -> EngineT | None:
    """Return the engine a stored feature setting selects: its own pick or the default."""
    if _follows_default(value):
        return _default_engine(mass, kind, engines)
    return _resolve(engines, value if isinstance(value, str) else None)


def _follows_default(value: ConfigValueType) -> bool:
    """Return whether a stored feature setting follows the default engine of its kind."""
    return value in (None, "", CONF_VALUE_GLOBAL)


def _stored_default(mass: MusicAssistant, kind: EngineKind) -> str | None:
    """Return the uid stored as the default engine of a kind, if any."""
    stored = mass.config.get_raw_core_config_value(CONF_AI, _DEFAULT_ENGINE_KEYS[kind])
    return stored if isinstance(stored, str) and stored else None


def _default_engine[EngineT: PluginEngine](
    mass: MusicAssistant, kind: EngineKind, engines: Sequence[EngineT]
) -> EngineT | None:
    """Return the default engine of a kind out of the given engines, adopting one if unset."""
    if (stored := _stored_default(mass, kind)) is not None:
        return _resolve(engines, stored)
    if not engines:
        return None
    engine = engines[0]
    mass.config.set_raw_core_config_value(CONF_AI, _DEFAULT_ENGINE_KEYS[kind], engine.uid)
    LOGGER.debug("Adopted engine %s as the default %s engine", engine.uid, kind)
    return engine


def _engine_options(engines: Sequence[PluginEngine]) -> list[ConfigValueOption]:
    """Return a picker option for each of the given engines."""
    return [ConfigValueOption(engine.uid, title=engine_display_name(engine)) for engine in engines]


def _create_feature_engine_config_entries(
    mass: MusicAssistant,
    kind: EngineKind,
    engines: Sequence[PluginEngine],
    key: str,
    depends_on: str | None,
    required: bool,
    category: str,
    depends_on_value: ConfigValueType | None = None,
) -> tuple[ConfigEntry, ...]:
    """Build a feature's engine picker, following the default engine unless one is picked."""
    default = _default_engine(mass, kind, engines)
    picker = ConfigEntry(
        key=key,
        type=ConfigEntryType.STRING,
        required=required,
        default_value=CONF_VALUE_GLOBAL,
        options=[
            # no options.global string exists on purpose: a translation would replace this title
            ConfigValueOption(
                CONF_VALUE_GLOBAL,
                title=engine_display_name(default) if default else _stored_default(mass, kind),
            ),
            *_engine_options(engines),
        ],
        depends_on=depends_on,
        depends_on_value=depends_on_value,
        category=category,
        hidden=not engines,
    )
    missing_value = CONF_VALUE_GLOBAL if engines and default is None else None
    return (
        picker,
        *_engine_alerts(kind, picker, engines, missing_value, _DEFAULT_UNAVAILABLE_TRANSLATION_KEY),
    )


def _engine_alerts(
    kind: EngineKind,
    picker: ConfigEntry,
    engines: Sequence[PluginEngine],
    missing_value: str | None,
    missing_translation_key: str,
) -> tuple[ConfigEntry, ...]:
    """
    Return the alert for an engine picker when no engine, or the engine it selects, is available.

    :param kind: The kind of engine the picker selects.
    :param picker: The picker entry.
    :param engines: The available engines of the kind.
    :param missing_value: The picker value selecting an engine that is not available, if any.
    :param missing_translation_key: The source string of the alert for missing_value.
    """
    if not engines:
        return (
            ConfigEntry(
                key=f"{picker.key}_unavailable",
                type=ConfigEntryType.ALERT,
                depends_on=picker.depends_on,
                depends_on_value=picker.depends_on_value,
                category=picker.category,
                translation_key=_UNAVAILABLE_TRANSLATION_KEYS[kind],
            ),
        )
    if missing_value is None:
        return ()
    return (
        ConfigEntry(
            key=f"{picker.key}_default_unavailable",
            type=ConfigEntryType.ALERT,
            depends_on=picker.key,
            depends_on_value=missing_value,
            category=picker.category,
            translation_key=missing_translation_key,
        ),
    )
