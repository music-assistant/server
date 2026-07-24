"""Yandex Music provider support for Music Assistant."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, ConfigValueType
from music_assistant_models.enums import ConfigEntryType, ProviderFeature
from music_assistant_models.errors import InvalidDataError

from music_assistant.constants import CONF_ENTRY_UNOFFICIAL_PROVIDER

from .constants import (
    CONF_ACTION_DELETE_WAVE_PRESET,
    CONF_ACTION_SAVE_WAVE_PRESET,
    CONF_BASE_URL,
    CONF_LIKED_TRACKS_MAX_TRACKS,
    CONF_MY_WAVE_MAX_TRACKS,
    CONF_QUALITY,
    CONF_RESTRICTIVE_RATE_LIMITS,
    CONF_WAVE_PRESET_DRAFT_DIVERSITY,
    CONF_WAVE_PRESET_DRAFT_LANGUAGE,
    CONF_WAVE_PRESET_DRAFT_MOOD,
    CONF_WAVE_PRESET_DRAFT_NAME,
    CONF_WAVE_PRESET_TO_DELETE,
    CONF_WAVE_PRESETS_DATA,
    DEFAULT_BASE_URL,
    QUALITY_BALANCED,
    QUALITY_EFFICIENT,
    QUALITY_HIGH,
    QUALITY_SUPERB,
    WAVE_PRESET_DIVERSITY_VALUES,
    WAVE_PRESET_LANGUAGE_VALUES,
    WAVE_PRESET_MOOD_VALUES,
)
from .presets import parse_stored_presets as _parse_stored_presets
from .provider import YandexMusicProvider


def _save_wave_preset_action(values: dict[str, ConfigValueType]) -> None:
    """
    Merge the current draft fields into the stored preset list.

    Overwrites an existing preset with the same name instead of creating a
    duplicate. Clears draft fields after persisting so the UI returns to a
    blank state. Raises ``InvalidDataError`` when the name is blank.
    """
    name_raw = values.get(CONF_WAVE_PRESET_DRAFT_NAME)
    name = name_raw.strip() if isinstance(name_raw, str) else ""
    if not name:
        raise InvalidDataError("Please fill the preset name before saving.")
    presets = _parse_stored_presets(values.get(CONF_WAVE_PRESETS_DATA))
    presets = [p for p in presets if p["name"] != name]
    new_preset: dict[str, str] = {"name": name}
    for conf_key, api_key in (
        (CONF_WAVE_PRESET_DRAFT_DIVERSITY, "diversity"),
        (CONF_WAVE_PRESET_DRAFT_MOOD, "moodEnergy"),
        (CONF_WAVE_PRESET_DRAFT_LANGUAGE, "language"),
    ):
        val = values.get(conf_key)
        if isinstance(val, str) and val:
            new_preset[api_key] = val
    presets.append(new_preset)
    values[CONF_WAVE_PRESETS_DATA] = json.dumps(presets, ensure_ascii=False)
    # Clear draft so the UI is ready for the next preset
    values[CONF_WAVE_PRESET_DRAFT_NAME] = None
    values[CONF_WAVE_PRESET_DRAFT_DIVERSITY] = ""
    values[CONF_WAVE_PRESET_DRAFT_MOOD] = ""
    values[CONF_WAVE_PRESET_DRAFT_LANGUAGE] = ""


def _delete_wave_preset_action(values: dict[str, ConfigValueType]) -> None:
    """
    Remove the preset named by CONF_WAVE_PRESET_TO_DELETE from the store.

    Raises ``InvalidDataError`` when no name is selected. Idempotent — absent
    names simply rewrite an unchanged list.
    """
    target_raw = values.get(CONF_WAVE_PRESET_TO_DELETE)
    target = target_raw.strip() if isinstance(target_raw, str) else ""
    if not target:
        raise InvalidDataError("Please select a preset to delete.")
    presets = _parse_stored_presets(values.get(CONF_WAVE_PRESETS_DATA))
    presets = [p for p in presets if p["name"] != target]
    values[CONF_WAVE_PRESETS_DATA] = json.dumps(presets, ensure_ascii=False)
    values[CONF_WAVE_PRESET_TO_DELETE] = ""


def _wave_preset_config_entries(values: dict[str, ConfigValueType]) -> list[ConfigEntry]:
    """
    Return the wave-preset builder UI (all advanced settings).

    Layout:
      - Section label showing how many presets are saved.
      - Four "draft" fields (name + three dropdowns) the user fills in.
      - "Save preset" action → copies draft into the JSON store.
      - "Delete preset" dropdown + action (hidden when no presets exist).
      - Hidden STRING carrying the JSON store itself.

    Number of presets is unbounded; the user never edits JSON directly.
    """
    empty_title = "— Default —"
    diversity_options = [
        ConfigValueOption(v, title=empty_title if not v else v.title())
        for v in WAVE_PRESET_DIVERSITY_VALUES
    ]
    mood_options = [
        ConfigValueOption(v, title=empty_title if not v else v.title())
        for v in WAVE_PRESET_MOOD_VALUES
    ]
    language_options = [
        ConfigValueOption(v, title=empty_title if not v else v.replace("-", " ").title())
        for v in WAVE_PRESET_LANGUAGE_VALUES
    ]

    presets = _parse_stored_presets(values.get(CONF_WAVE_PRESETS_DATA))
    has_presets = bool(presets)
    delete_options = [ConfigValueOption(p["name"], title=p["name"]) for p in presets]
    if not delete_options:
        # Empty options can break some frontends; supply a no-op placeholder.
        delete_options = [ConfigValueOption("")]

    def _str_value(key: str) -> str | None:
        v = values.get(key)
        return v if isinstance(v, str) else None

    return [
        ConfigEntry(
            key="wave_preset_section_label",
            type=ConfigEntryType.LABEL,
            translation_key="wave_preset_section_saved" if has_presets else None,
            translation_params=[str(len(presets))] if has_presets else None,
            advanced=True,
        ),
        ConfigEntry(
            key=CONF_WAVE_PRESET_DRAFT_NAME,
            type=ConfigEntryType.STRING,
            default_value=None,
            required=False,
            advanced=True,
            value=_str_value(CONF_WAVE_PRESET_DRAFT_NAME),
        ),
        ConfigEntry(
            key=CONF_WAVE_PRESET_DRAFT_DIVERSITY,
            type=ConfigEntryType.STRING,
            options=diversity_options,
            default_value="",
            required=False,
            advanced=True,
            value=_str_value(CONF_WAVE_PRESET_DRAFT_DIVERSITY),
        ),
        ConfigEntry(
            key=CONF_WAVE_PRESET_DRAFT_MOOD,
            type=ConfigEntryType.STRING,
            options=mood_options,
            default_value="",
            required=False,
            advanced=True,
            value=_str_value(CONF_WAVE_PRESET_DRAFT_MOOD),
        ),
        ConfigEntry(
            key=CONF_WAVE_PRESET_DRAFT_LANGUAGE,
            type=ConfigEntryType.STRING,
            options=language_options,
            default_value="",
            required=False,
            advanced=True,
            value=_str_value(CONF_WAVE_PRESET_DRAFT_LANGUAGE),
        ),
        ConfigEntry(
            key=CONF_ACTION_SAVE_WAVE_PRESET,
            type=ConfigEntryType.ACTION,
            action=CONF_ACTION_SAVE_WAVE_PRESET,
            advanced=True,
        ),
        ConfigEntry(
            key=CONF_WAVE_PRESET_TO_DELETE,
            type=ConfigEntryType.STRING,
            options=delete_options,
            default_value="",
            required=False,
            advanced=True,
            hidden=not has_presets,
            value=_str_value(CONF_WAVE_PRESET_TO_DELETE),
        ),
        ConfigEntry(
            key=CONF_ACTION_DELETE_WAVE_PRESET,
            type=ConfigEntryType.ACTION,
            action=CONF_ACTION_DELETE_WAVE_PRESET,
            advanced=True,
            hidden=not has_presets,
        ),
        ConfigEntry(
            key=CONF_WAVE_PRESETS_DATA,
            type=ConfigEntryType.STRING,
            default_value="",
            required=False,
            advanced=True,
            hidden=True,
            value=_str_value(CONF_WAVE_PRESETS_DATA) or "",
        ),
    ]


if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.LIBRARY_PODCASTS,
    ProviderFeature.LIBRARY_AUDIOBOOKS,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.SEARCH,
    ProviderFeature.LIBRARY_ARTISTS_EDIT,
    ProviderFeature.LIBRARY_ALBUMS_EDIT,
    ProviderFeature.LIBRARY_TRACKS_EDIT,
    ProviderFeature.LIBRARY_PODCASTS_EDIT,
    ProviderFeature.LIBRARY_AUDIOBOOKS_EDIT,
    ProviderFeature.BROWSE,
    ProviderFeature.SIMILAR_TRACKS,
    ProviderFeature.SIMILAR_ARTISTS,
    ProviderFeature.RECOMMENDATIONS,
    ProviderFeature.LYRICS,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return YandexMusicProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to configure this provider.

    Authentication runs in the interactive setup flow (see setup_flow.py); this
    surface only exposes the genuine playback options and the My Wave preset builder
    (whose save/delete actions are handled here).
    """
    if values is None:
        values = {}

    # Wave-preset save/delete actions mutate the hidden JSON store and clear
    # the draft / selection fields so the UI re-renders in a clean state.
    if action == CONF_ACTION_SAVE_WAVE_PRESET:
        _save_wave_preset_action(values)
    if action == CONF_ACTION_DELETE_WAVE_PRESET:
        _delete_wave_preset_action(values)

    return (
        CONF_ENTRY_UNOFFICIAL_PROVIDER,
        # Quality
        ConfigEntry(
            key=CONF_QUALITY,
            type=ConfigEntryType.STRING,
            options=[
                ConfigValueOption(QUALITY_EFFICIENT),
                ConfigValueOption(QUALITY_BALANCED),
                ConfigValueOption(QUALITY_HIGH),
                ConfigValueOption(QUALITY_SUPERB),
            ],
            default_value=QUALITY_BALANCED,
        ),
        # My Wave maximum tracks (advanced)
        ConfigEntry(
            key=CONF_MY_WAVE_MAX_TRACKS,
            type=ConfigEntryType.INTEGER,
            range=(10, 1000),
            default_value=150,
            required=False,
            advanced=True,
        ),
        # User-defined wave presets: builder + save/delete actions (dynamic list)
        *_wave_preset_config_entries(values),
        # Liked Tracks maximum tracks (advanced)
        ConfigEntry(
            key=CONF_LIKED_TRACKS_MAX_TRACKS,
            type=ConfigEntryType.INTEGER,
            range=(50, 2000),
            default_value=200,
            required=False,
            advanced=True,
        ),
        # API Base URL (advanced)
        ConfigEntry(
            key=CONF_BASE_URL,
            type=ConfigEntryType.STRING,
            translation_params=[DEFAULT_BASE_URL],
            default_value=DEFAULT_BASE_URL,
            required=False,
            advanced=True,
        ),
        # Restrictive rate limits (advanced)
        ConfigEntry(
            key=CONF_RESTRICTIVE_RATE_LIMITS,
            type=ConfigEntryType.BOOLEAN,
            default_value=False,
            required=False,
            advanced=True,
        ),
    )
