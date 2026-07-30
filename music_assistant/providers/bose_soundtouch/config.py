"""
Preset configuration for the Bose SoundTouch provider.

Builds the config entries shown in the provider settings, allowing the user to map
every physical preset button (1-6) to a Music Assistant media item via a
search-and-select flow. The mapping is provider wide: pressing button 4 plays the
same content on every SoundTouch speaker of this provider instance.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, MediaType
from music_assistant_models.errors import MusicAssistantError
from music_assistant_models.media_items import (
    Album,
    Artist,
    Audiobook,
    Genre,
    ItemMapping,
    Playlist,
    Podcast,
    Radio,
    Track,
)

from .const import PRESET_IDS

if TYPE_CHECKING:
    from music_assistant_models.media_items import SearchResults

    from music_assistant.mass import MusicAssistant

    from .provider import BoseSoundTouchProvider

SearchResultItem = (
    Artist | Album | Track | Radio | Playlist | Audiobook | Podcast | Genre | ItemMapping
)

SEARCH_RESULT_LIMIT = 25
SEARCH_TIMEOUT = 10

# shared prefix of every preset config key, used to tell a preset-only config
# change apart from a change that needs a provider reload
PRESET_KEY_PREFIX = "preset_"
CONF_SEARCH_MEDIA_TYPE = "preset_search_media_type"
CONF_SEARCH_QUERY = "preset_search_query"
CONF_SEARCH_RESULT = "preset_search_result"
CONF_SEARCH_TARGET = "preset_search_target"
ACTION_SEARCH = "preset_search"
ACTION_ASSIGN = "preset_assign"
SEARCH_CATEGORY = "preset_search"
PRESET_CATEGORY = "presets"

SEARCHABLE_MEDIA_TYPES = (
    MediaType.ARTIST,
    MediaType.ALBUM,
    MediaType.TRACK,
    MediaType.PLAYLIST,
    MediaType.RADIO,
    MediaType.AUDIOBOOK,
    MediaType.PODCAST,
    MediaType.GENRE,
)
DEFAULT_MEDIA_TYPE = MediaType.PLAYLIST
MEDIA_TYPE_OPTIONS = [
    ConfigValueOption(title=media_type.value.replace("_", " ").title(), value=media_type.value)
    for media_type in SEARCHABLE_MEDIA_TYPES
]
PRESET_TARGET_OPTIONS = [
    ConfigValueOption(
        value="",
        disabled=True,
    ),
    *[
        ConfigValueOption(title=f"Preset {preset_id}", value=str(preset_id))
        for preset_id in PRESET_IDS
    ],
]


def preset_media_key(preset_id: int) -> str:
    """Return the config key holding the media URI for the given preset."""
    return f"{PRESET_KEY_PREFIX}{preset_id}_media"


async def build_preset_config_entries(
    provider: BoseSoundTouchProvider,
    *,
    refresh_results: bool = False,
) -> list[ConfigEntry]:
    """
    Return the preset config entries for the SoundTouch provider.

    A single search flow assigns its selected result to any of the six physical
    preset buttons. Existing preset mappings remain editable independently below it.

    :param provider: The SoundTouch provider whose stored config the entries are built from.
    :param refresh_results: Whether to run the media search for this render.
    """
    media_type = _media_type_config_value(provider, CONF_SEARCH_MEDIA_TYPE)
    query = _string_config_value(provider, CONF_SEARCH_QUERY).strip()
    selected_media = _string_config_value(provider, CONF_SEARCH_RESULT)
    selected_target = _string_config_value(provider, CONF_SEARCH_TARGET)
    media_options = await _build_search_result_options(
        mass=provider.mass,
        media_type=media_type,
        query=query,
        selected_media=selected_media,
        refresh_results=refresh_results,
    )

    entries = [
        ConfigEntry(
            key=CONF_SEARCH_MEDIA_TYPE,
            type=ConfigEntryType.STRING,
            translation_key="preset_search_media_type",
            required=False,
            default_value=DEFAULT_MEDIA_TYPE.value,
            value=media_type.value,
            options=MEDIA_TYPE_OPTIONS,
            category=SEARCH_CATEGORY,
            immediate_apply=True,
        ),
        ConfigEntry(
            key=CONF_SEARCH_QUERY,
            type=ConfigEntryType.STRING,
            translation_key="preset_search_query",
            required=False,
            default_value="",
            value=query,
            category=SEARCH_CATEGORY,
            immediate_apply=True,
        ),
        ConfigEntry(
            key="preset_do_search",
            type=ConfigEntryType.ACTION,
            translation_key="preset_search_action",
            action=ACTION_SEARCH,
            category=SEARCH_CATEGORY,
        ),
        ConfigEntry(
            key=CONF_SEARCH_RESULT,
            type=ConfigEntryType.STRING,
            translation_key="preset_search_result",
            required=False,
            default_value="",
            value=selected_media,
            options=media_options,
            category=SEARCH_CATEGORY,
            immediate_apply=True,
            hidden=not media_options,
        ),
        ConfigEntry(
            key=CONF_SEARCH_TARGET,
            type=ConfigEntryType.STRING,
            translation_key="preset_search_target",
            required=False,
            default_value="",
            value=selected_target,
            options=PRESET_TARGET_OPTIONS,
            category=SEARCH_CATEGORY,
            immediate_apply=True,
            hidden=not media_options,
        ),
    ]
    can_assign = bool(
        media_options
        and selected_media
        and selected_target in {str(preset_id) for preset_id in PRESET_IDS}
    )
    entries.append(
        ConfigEntry(
            key="preset_do_assign",
            type=ConfigEntryType.ACTION,
            translation_key="preset_assign_action",
            translation_params=[selected_target],
            action=ACTION_ASSIGN,
            category=SEARCH_CATEGORY,
            immediate_apply=True,
            hidden=not can_assign,
        )
    )

    for preset_id in PRESET_IDS:
        entries.append(
            ConfigEntry(
                key=preset_media_key(preset_id),
                type=ConfigEntryType.STRING,
                translation_key="preset_media",
                translation_params=[str(preset_id)],
                required=False,
                default_value="",
                value=_string_config_value(provider, preset_media_key(preset_id)),
                category=PRESET_CATEGORY,
            )
        )
    return entries


async def _build_search_result_options(
    mass: MusicAssistant,
    media_type: MediaType,
    query: str,
    selected_media: str,
    refresh_results: bool,
) -> list[ConfigValueOption]:
    """Build the result dropdown for a preset without losing the current selection."""
    media_options = await _build_media_options(mass, media_type, query) if refresh_results else []
    if selected_media and selected_media not in {option.value for option in media_options}:
        media_options.append(ConfigValueOption(title=selected_media, value=selected_media))
    return media_options


async def _build_media_options(
    mass: MusicAssistant,
    media_type: MediaType,
    query: str,
) -> list[ConfigValueOption]:
    """Build dropdown options for a preset media search."""
    options_by_value: dict[str, ConfigValueOption] = {}
    for item in await _search_media_items(mass, media_type, query):
        value = item.uri
        if not value or value in options_by_value:
            continue
        options_by_value[value] = ConfigValueOption(
            title=f"{item.name} ({item.media_type.value}, {item.provider})",
            value=value,
        )
    return sorted(options_by_value.values(), key=lambda option: (option.title or "").lower())


async def _search_media_items(
    mass: MusicAssistant,
    media_type: MediaType,
    query: str,
) -> list[SearchResultItem]:
    """Search MA media items for preset config options."""
    if not query or media_type not in SEARCHABLE_MEDIA_TYPES:
        return []
    try:
        search_result = await asyncio.wait_for(
            mass.music.search(
                search_query=query,
                media_types=[media_type],
                limit=SEARCH_RESULT_LIMIT,
                library_only=False,
            ),
            timeout=SEARCH_TIMEOUT,
        )
    except MusicAssistantError, TimeoutError:
        return []
    return _iter_search_result_items(search_result, media_type)


def _iter_search_result_items(
    search_result: SearchResults,
    media_type: MediaType,
) -> list[SearchResultItem]:
    """Extract media items from a typed MA search result."""
    match media_type:
        case MediaType.ARTIST:
            return list(search_result.artists)
        case MediaType.ALBUM:
            return list(search_result.albums)
        case MediaType.GENRE:
            return list(search_result.genres)
        case MediaType.TRACK:
            return list(search_result.tracks)
        case MediaType.PLAYLIST:
            return list(search_result.playlists)
        case MediaType.RADIO:
            return list(search_result.radio)
        case MediaType.AUDIOBOOK:
            return list(search_result.audiobooks)
        case MediaType.PODCAST:
            return list(search_result.podcasts)
        case _:
            return []


def _string_config_value(provider: BoseSoundTouchProvider, key: str) -> str:
    """Return a string config value from the provider's stored config (empty if unset)."""
    value = provider.get_config_value(key)
    return value if isinstance(value, str) else ""


def _media_type_config_value(provider: BoseSoundTouchProvider, key: str) -> MediaType:
    """Return a searchable media type from the provider's stored config."""
    media_type = MediaType(_string_config_value(provider, key) or DEFAULT_MEDIA_TYPE.value)
    return media_type if media_type in SEARCHABLE_MEDIA_TYPES else DEFAULT_MEDIA_TYPE
