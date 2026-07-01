"""
Config-entry schema for the Player Queues controller.

Builds the static core-module config entries (per-type enqueue defaults) and the per-queue config
entries (autoplay, crossfade and smart-shuffle settings). Kept separate from the controller so the
schema reads as one self-contained unit; the controller exposes these through thin delegators.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import (
    ConfigEntryType,
    CrossfadeMode,
    ProviderFeature,
    QueueOption,
)

from music_assistant.constants import (
    CONF_CROSSFADE_MODE,
    CONF_ENTRY_CROSSFADE_DURATION,
    CONF_ENTRY_VOLUME_NORMALIZATION,
)
from music_assistant.controllers.player_queues.autoplay import (
    AUTOPLAY_MODE_DEFAULT_VALUE,
    AutoplayMode,
)
from music_assistant.controllers.player_queues.constants import (
    CONF_AUTOPLAY_LABEL,
    CONF_AUTOPLAY_MODE,
    CONF_AUTOPLAY_PLAYLIST,
    CONF_CROSSFADE_LABEL,
    CONF_DEFAULT_ENQUEUE_OPTION_ALBUM,
    CONF_DEFAULT_ENQUEUE_OPTION_ARTIST,
    CONF_DEFAULT_ENQUEUE_OPTION_AUDIOBOOK,
    CONF_DEFAULT_ENQUEUE_OPTION_FOLDER,
    CONF_DEFAULT_ENQUEUE_OPTION_GENRE,
    CONF_DEFAULT_ENQUEUE_OPTION_LIVE_SOURCES,
    CONF_DEFAULT_ENQUEUE_OPTION_PLAYLIST,
    CONF_DEFAULT_ENQUEUE_OPTION_PODCAST,
    CONF_DEFAULT_ENQUEUE_OPTION_PODCAST_EPISODE,
    CONF_DEFAULT_ENQUEUE_OPTION_TRACK,
    CONF_DEFAULT_ENQUEUE_SELECT_ALBUM,
    CONF_DEFAULT_ENQUEUE_SELECT_ARTIST,
    CONF_SMART_SHUFFLE_ARTIST_RECENCY,
    CONF_SMART_SHUFFLE_DUPLICATE_GAP,
    CONF_SMART_SHUFFLE_ENABLED,
    CONF_SMART_SHUFFLE_LABEL,
    CONF_SMART_SHUFFLE_SONG_RECENCY,
    ENQUEUE_SELECT_ALBUM_DEFAULT_VALUE,
    ENQUEUE_SELECT_ARTIST_DEFAULT_VALUE,
    SMART_SHUFFLE_ARTIST_RECENCY_DEFAULT,
    SMART_SHUFFLE_ARTIST_RECENCY_OPTIONS,
    SMART_SHUFFLE_DUPLICATE_GAP_DEFAULT,
    SMART_SHUFFLE_DUPLICATE_GAP_OPTIONS,
    SMART_SHUFFLE_ENABLED_DEFAULT,
    SMART_SHUFFLE_SONG_RECENCY_DEFAULT,
    SMART_SHUFFLE_SONG_RECENCY_OPTIONS,
)

if TYPE_CHECKING:
    from music_assistant import MusicAssistant


def core_config_entries() -> tuple[ConfigEntry, ...]:
    """Return the core-module config entries (per-media-type default enqueue options)."""
    enqueue_options = [
        ConfigValueOption(QueueOption.PLAY.value),
        ConfigValueOption(QueueOption.REPLACE.value),
    ]
    return (
        ConfigEntry(
            key=CONF_DEFAULT_ENQUEUE_SELECT_ARTIST,
            type=ConfigEntryType.STRING,
            default_value=ENQUEUE_SELECT_ARTIST_DEFAULT_VALUE,
            options=[
                ConfigValueOption("top_tracks"),
                ConfigValueOption("library_tracks"),
                ConfigValueOption("prefer_library"),
                ConfigValueOption("all_tracks"),
            ],
        ),
        ConfigEntry(
            key=CONF_DEFAULT_ENQUEUE_SELECT_ALBUM,
            type=ConfigEntryType.STRING,
            default_value=ENQUEUE_SELECT_ALBUM_DEFAULT_VALUE,
            options=[
                ConfigValueOption("library_tracks"),
                ConfigValueOption("all_tracks"),
            ],
        ),
        ConfigEntry(
            key=CONF_DEFAULT_ENQUEUE_OPTION_ARTIST,
            type=ConfigEntryType.STRING,
            default_value=QueueOption.REPLACE.value,
            options=enqueue_options,
        ),
        ConfigEntry(
            key=CONF_DEFAULT_ENQUEUE_OPTION_ALBUM,
            type=ConfigEntryType.STRING,
            default_value=QueueOption.REPLACE.value,
            options=enqueue_options,
        ),
        ConfigEntry(
            key=CONF_DEFAULT_ENQUEUE_OPTION_TRACK,
            type=ConfigEntryType.STRING,
            default_value=QueueOption.PLAY.value,
            options=enqueue_options,
        ),
        ConfigEntry(
            key=CONF_DEFAULT_ENQUEUE_OPTION_GENRE,
            type=ConfigEntryType.STRING,
            default_value=QueueOption.REPLACE.value,
            options=enqueue_options,
        ),
        ConfigEntry(
            key=CONF_DEFAULT_ENQUEUE_OPTION_LIVE_SOURCES,
            type=ConfigEntryType.STRING,
            default_value=QueueOption.REPLACE.value,
            options=enqueue_options,
        ),
        ConfigEntry(
            key=CONF_DEFAULT_ENQUEUE_OPTION_PLAYLIST,
            type=ConfigEntryType.STRING,
            default_value=QueueOption.REPLACE.value,
            options=enqueue_options,
        ),
        ConfigEntry(
            key=CONF_DEFAULT_ENQUEUE_OPTION_AUDIOBOOK,
            type=ConfigEntryType.STRING,
            default_value=QueueOption.REPLACE.value,
            options=enqueue_options,
            hidden=True,
        ),
        ConfigEntry(
            key=CONF_DEFAULT_ENQUEUE_OPTION_PODCAST,
            type=ConfigEntryType.STRING,
            default_value=QueueOption.REPLACE.value,
            options=enqueue_options,
            hidden=True,
        ),
        ConfigEntry(
            key=CONF_DEFAULT_ENQUEUE_OPTION_PODCAST_EPISODE,
            type=ConfigEntryType.STRING,
            default_value=QueueOption.REPLACE.value,
            options=enqueue_options,
            hidden=True,
        ),
        ConfigEntry(
            key=CONF_DEFAULT_ENQUEUE_OPTION_FOLDER,
            type=ConfigEntryType.STRING,
            default_value=QueueOption.REPLACE.value,
            options=enqueue_options,
            hidden=True,
        ),
    )


def queue_config_entries(
    mass: MusicAssistant, playlist_options: list[ConfigValueOption] | None = None
) -> list[ConfigEntry]:
    """
    Return the per-queue config entries.

    The autoplay_mode select disables the 'similar' option when no provider can supply
    similar tracks. The crossfade_mode select's options and default depend on whether smart
    fades are available: the smart option is disabled (shown but not selectable) and the
    default falls back to standard crossfade when smart fades can't be used on this server.

    :param mass: The MusicAssistant instance (read for provider/smart-fades availability).
    :param playlist_options: Library playlists to offer for the 'playlist' autoplay mode.
        Only populated when serving the entries to the UI; the parse path can omit it.
    """
    similar_tracks_available = any(
        ProviderFeature.SIMILAR_TRACKS in provider.supported_features
        for provider in mass.music.providers
    )
    autoplay_entries = [
        ConfigEntry(
            key=CONF_AUTOPLAY_LABEL,
            type=ConfigEntryType.LABEL,
            category="autoplay",
        ),
        ConfigEntry(
            key=CONF_AUTOPLAY_MODE,
            type=ConfigEntryType.STRING,
            options=[
                ConfigValueOption(AutoplayMode.AUTO.value),
                ConfigValueOption(
                    AutoplayMode.SIMILAR.value, disabled=not similar_tracks_available
                ),
                ConfigValueOption(AutoplayMode.LIBRARY.value),
                ConfigValueOption(AutoplayMode.PLAYLIST.value),
            ],
            default_value=AUTOPLAY_MODE_DEFAULT_VALUE,
            category="autoplay",
        ),
        ConfigEntry(
            key=CONF_AUTOPLAY_PLAYLIST,
            type=ConfigEntryType.STRING,
            options=playlist_options or [],
            default_value=None,
            required=False,
            depends_on=CONF_AUTOPLAY_MODE,
            depends_on_value=AutoplayMode.PLAYLIST.value,
            category="autoplay",
        ),
    ]
    smart_fades_available = mass.streams.smart_fades_available
    crossfade_mode_entry = ConfigEntry(
        key=CONF_CROSSFADE_MODE,
        type=ConfigEntryType.STRING,
        options=[
            ConfigValueOption(CrossfadeMode.STANDARD_CROSSFADE.value),
            ConfigValueOption(
                CrossfadeMode.SMART_CROSSFADE.value, disabled=not smart_fades_available
            ),
        ],
        default_value=(
            CrossfadeMode.SMART_CROSSFADE.value
            if smart_fades_available
            else CrossfadeMode.STANDARD_CROSSFADE.value
        ),
        category="crossfade",
        requires_reload=True,
    )
    crossfade_entries = [
        ConfigEntry(
            key=CONF_CROSSFADE_LABEL,
            type=ConfigEntryType.LABEL,
            category="crossfade",
        ),
        crossfade_mode_entry,
        CONF_ENTRY_CROSSFADE_DURATION,
    ]
    smart_shuffle_entries = [
        ConfigEntry(
            key=CONF_SMART_SHUFFLE_LABEL,
            type=ConfigEntryType.LABEL,
            category="smart_shuffle",
        ),
        ConfigEntry(
            key=CONF_SMART_SHUFFLE_ENABLED,
            type=ConfigEntryType.BOOLEAN,
            default_value=SMART_SHUFFLE_ENABLED_DEFAULT,
            category="smart_shuffle",
        ),
        ConfigEntry(
            key=CONF_SMART_SHUFFLE_SONG_RECENCY,
            type=ConfigEntryType.STRING,
            options=[
                ConfigValueOption(str(seconds)) for seconds in SMART_SHUFFLE_SONG_RECENCY_OPTIONS
            ],
            default_value=str(SMART_SHUFFLE_SONG_RECENCY_DEFAULT),
            category="smart_shuffle",
            depends_on=CONF_SMART_SHUFFLE_ENABLED,
            depends_on_value=True,
        ),
        ConfigEntry(
            key=CONF_SMART_SHUFFLE_ARTIST_RECENCY,
            type=ConfigEntryType.STRING,
            options=[
                ConfigValueOption(str(seconds)) for seconds in SMART_SHUFFLE_ARTIST_RECENCY_OPTIONS
            ],
            default_value=str(SMART_SHUFFLE_ARTIST_RECENCY_DEFAULT),
            category="smart_shuffle",
            depends_on=CONF_SMART_SHUFFLE_ENABLED,
            depends_on_value=True,
        ),
        ConfigEntry(
            key=CONF_SMART_SHUFFLE_DUPLICATE_GAP,
            type=ConfigEntryType.STRING,
            options=[
                ConfigValueOption(str(seconds)) for seconds in SMART_SHUFFLE_DUPLICATE_GAP_OPTIONS
            ],
            default_value=str(SMART_SHUFFLE_DUPLICATE_GAP_DEFAULT),
            category="smart_shuffle",
            depends_on=CONF_SMART_SHUFFLE_ENABLED,
            depends_on_value=True,
            advanced=True,
        ),
    ]
    return [
        *smart_shuffle_entries,
        *autoplay_entries,
        *crossfade_entries,
        CONF_ENTRY_VOLUME_NORMALIZATION,
    ]
