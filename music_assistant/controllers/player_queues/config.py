"""
Config-entry schema for the Player Queues controller.

Two related schemas are built from shared builders:

* the **core-module** entries — the *global* queue-controller defaults: the per-media-type enqueue
  behaviour, plus the global smart-shuffle / autoplay / crossfade / volume-normalization settings.
* the **per-queue** entries — the same feature settings, but each headline setting gains a "global"
  option (its default) that follows the matching global value, mirroring the ``log_level`` "GLOBAL"
  pattern. The smart-shuffle recency windows and the crossfade duration are global-only (they can't
  sensibly follow-or-override per queue), so they appear only in the core schema.

Kept separate from the controller so the schema reads as one self-contained unit; the controller
exposes these through thin delegators.
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
    CONF_VALUE_DISABLED,
    CONF_VALUE_ENABLED,
    CONF_VALUE_GLOBAL,
    CONF_VOLUME_NORMALIZATION,
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
    CONF_DEFAULT_ENQUEUE_OPTION_COLLECTION,
    CONF_DEFAULT_ENQUEUE_OPTION_FOLDER,
    CONF_DEFAULT_ENQUEUE_OPTION_GENRE,
    CONF_DEFAULT_ENQUEUE_OPTION_LIVE_SOURCES,
    CONF_DEFAULT_ENQUEUE_OPTION_PLAYLIST,
    CONF_DEFAULT_ENQUEUE_OPTION_PODCAST,
    CONF_DEFAULT_ENQUEUE_OPTION_PODCAST_EPISODE,
    CONF_DEFAULT_ENQUEUE_OPTION_SOUND_EFFECT,
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
    SMART_SHUFFLE_SONG_RECENCY_DEFAULT,
    SMART_SHUFFLE_SONG_RECENCY_OPTIONS,
)

if TYPE_CHECKING:
    from music_assistant import MusicAssistant

# category names for frontend grouping (labels live under config_categories.<name> in strings.json)
CATEGORY_ITEMS_TO_SELECT = "items_to_select"
CATEGORY_DEFAULT_ENQUEUE_OPTION = "default_enqueue_option"
CATEGORY_SMART_SHUFFLE = "smart_shuffle"
CATEGORY_AUTOPLAY = "autoplay"
CATEGORY_CROSSFADE = "crossfade"
CATEGORY_AUDIO = "audio"


def core_config_entries(mass: MusicAssistant) -> tuple[ConfigEntry, ...]:
    """
    Return the core-module (global) config entries for the Player Queues controller.

    These are the queue-controller-wide defaults: the per-media-type enqueue behaviour plus the
    global smart-shuffle, autoplay, crossfade and volume-normalization settings that individual
    queues follow (via their "global" option) or override.

    Kept option-free (the global autoplay-playlist dropdown is populated by the config controller
    when serving the entries to the UI) so this stays cheap for the config parse/value path.

    :param mass: The MusicAssistant instance (read for provider/smart-fades availability).
    """
    return (
        *_enqueue_default_entries(),
        *_smart_shuffle_entries(per_queue=False),
        *_autoplay_entries(mass, None, per_queue=False),
        *_crossfade_entries(mass, per_queue=False),
        _volume_normalization_entry(per_queue=False),
    )


def queue_config_entries(
    mass: MusicAssistant, playlist_options: list[ConfigValueOption] | None = None
) -> list[ConfigEntry]:
    """
    Return the per-queue config entries.

    Each headline setting (smart shuffle, autoplay mode, crossfade mode, volume normalization)
    offers a "global" option — the default — that follows the matching queue-controller value.
    The smart-shuffle recency windows and the crossfade duration are global-only and therefore not
    part of this per-queue schema.

    The autoplay_mode select disables the 'similar' option when no provider can supply similar
    tracks. The crossfade_mode select's smart option is disabled (shown but not selectable) when
    smart fades can't be used on this server.

    :param mass: The MusicAssistant instance (read for provider/smart-fades availability).
    :param playlist_options: Library playlists to offer for the 'playlist' autoplay mode.
        Only populated when serving the entries to the UI; the parse path can omit it.
    """
    return [
        *_smart_shuffle_entries(per_queue=True),
        *_autoplay_entries(mass, playlist_options, per_queue=True),
        *_crossfade_entries(mass, per_queue=True),
        _volume_normalization_entry(per_queue=True),
    ]


def _onoff_entry(
    key: str,
    category: str,
    *,
    per_queue: bool,
    global_default: str,
    reload_on_change: bool = False,
) -> ConfigEntry:
    """
    Build an enabled/disabled select entry, adding the "global" option for the per-queue variant.

    :param key: The config key.
    :param category: The frontend category to group the entry under.
    :param per_queue: When True, append the "global" option and default to it; otherwise use
        ``global_default`` (the queue-controller default that "global" resolves to).
    :param global_default: The default value for the global (core) variant.
    :param reload_on_change: When True, changing the per-queue value restarts that queue's playback
        so the change applies immediately (a global change is left to apply on the next track, to
        avoid a disruptive reload of the whole queues core controller).
    """
    options = [ConfigValueOption(CONF_VALUE_ENABLED), ConfigValueOption(CONF_VALUE_DISABLED)]
    if per_queue:
        options.append(ConfigValueOption(CONF_VALUE_GLOBAL))
    return ConfigEntry(
        key=key,
        type=ConfigEntryType.STRING,
        options=options,
        default_value=CONF_VALUE_GLOBAL if per_queue else global_default,
        category=category,
        requires_reload=per_queue and reload_on_change,
    )


def _enqueue_default_entries() -> list[ConfigEntry]:
    """Per-media-type enqueue defaults: which tracks to select, and how to enqueue them."""
    enqueue_options = [
        ConfigValueOption(QueueOption.PLAY.value),
        ConfigValueOption(QueueOption.REPLACE.value),
    ]

    def _option_entry(key: str, default: str, *, hidden: bool = False) -> ConfigEntry:
        return ConfigEntry(
            key=key,
            type=ConfigEntryType.STRING,
            default_value=default,
            options=enqueue_options,
            category=CATEGORY_DEFAULT_ENQUEUE_OPTION,
            hidden=hidden,
        )

    return [
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
            category=CATEGORY_ITEMS_TO_SELECT,
        ),
        ConfigEntry(
            key=CONF_DEFAULT_ENQUEUE_SELECT_ALBUM,
            type=ConfigEntryType.STRING,
            default_value=ENQUEUE_SELECT_ALBUM_DEFAULT_VALUE,
            options=[
                ConfigValueOption("library_tracks"),
                ConfigValueOption("all_tracks"),
            ],
            category=CATEGORY_ITEMS_TO_SELECT,
        ),
        _option_entry(CONF_DEFAULT_ENQUEUE_OPTION_ARTIST, QueueOption.REPLACE.value),
        _option_entry(CONF_DEFAULT_ENQUEUE_OPTION_ALBUM, QueueOption.REPLACE.value),
        _option_entry(CONF_DEFAULT_ENQUEUE_OPTION_TRACK, QueueOption.PLAY.value),
        _option_entry(CONF_DEFAULT_ENQUEUE_OPTION_GENRE, QueueOption.REPLACE.value),
        _option_entry(CONF_DEFAULT_ENQUEUE_OPTION_LIVE_SOURCES, QueueOption.REPLACE.value),
        _option_entry(CONF_DEFAULT_ENQUEUE_OPTION_PLAYLIST, QueueOption.REPLACE.value),
        _option_entry(
            CONF_DEFAULT_ENQUEUE_OPTION_COLLECTION, QueueOption.REPLACE.value, hidden=True
        ),
        _option_entry(
            CONF_DEFAULT_ENQUEUE_OPTION_AUDIOBOOK, QueueOption.REPLACE.value, hidden=True
        ),
        _option_entry(CONF_DEFAULT_ENQUEUE_OPTION_PODCAST, QueueOption.REPLACE.value, hidden=True),
        _option_entry(
            CONF_DEFAULT_ENQUEUE_OPTION_PODCAST_EPISODE, QueueOption.REPLACE.value, hidden=True
        ),
        _option_entry(
            CONF_DEFAULT_ENQUEUE_OPTION_SOUND_EFFECT, QueueOption.REPLACE.value, hidden=True
        ),
        _option_entry(CONF_DEFAULT_ENQUEUE_OPTION_FOLDER, QueueOption.REPLACE.value, hidden=True),
    ]


def _smart_shuffle_entries(*, per_queue: bool) -> list[ConfigEntry]:
    """Smart-shuffle entries: the enabled toggle (per-queue overridable) + global-only tuning."""
    entries = [
        ConfigEntry(
            key=CONF_SMART_SHUFFLE_LABEL,
            type=ConfigEntryType.LABEL,
            category=CATEGORY_SMART_SHUFFLE,
        ),
        _onoff_entry(
            CONF_SMART_SHUFFLE_ENABLED,
            CATEGORY_SMART_SHUFFLE,
            per_queue=per_queue,
            global_default=CONF_VALUE_DISABLED,
        ),
    ]
    if per_queue:
        # the recency windows are global-only tuning (not overridable per queue)
        return entries
    # global tuning: shown unconditionally, since a per-queue override may enable smart shuffle
    # even while it is off globally, and these windows still apply to it.
    entries += [
        _recency_entry(
            CONF_SMART_SHUFFLE_SONG_RECENCY,
            SMART_SHUFFLE_SONG_RECENCY_OPTIONS,
            SMART_SHUFFLE_SONG_RECENCY_DEFAULT,
        ),
        _recency_entry(
            CONF_SMART_SHUFFLE_ARTIST_RECENCY,
            SMART_SHUFFLE_ARTIST_RECENCY_OPTIONS,
            SMART_SHUFFLE_ARTIST_RECENCY_DEFAULT,
        ),
        _recency_entry(
            CONF_SMART_SHUFFLE_DUPLICATE_GAP,
            SMART_SHUFFLE_DUPLICATE_GAP_OPTIONS,
            SMART_SHUFFLE_DUPLICATE_GAP_DEFAULT,
            advanced=True,
        ),
    ]
    return entries


def _recency_entry(
    key: str, options: tuple[int, ...], default: int, *, advanced: bool = False
) -> ConfigEntry:
    """Build a smart-shuffle recency-window select (values are seconds; 0 = off)."""
    return ConfigEntry(
        key=key,
        type=ConfigEntryType.STRING,
        options=[ConfigValueOption(str(seconds)) for seconds in options],
        default_value=str(default),
        category=CATEGORY_SMART_SHUFFLE,
        advanced=advanced,
    )


def _autoplay_entries(
    mass: MusicAssistant, playlist_options: list[ConfigValueOption] | None, *, per_queue: bool
) -> list[ConfigEntry]:
    """Autoplay entries: the mode select (per-queue overridable) + its playlist companion."""
    similar_available = any(
        ProviderFeature.SIMILAR_TRACKS in provider.supported_features
        for provider in mass.music.providers
    )
    mode_options = [
        ConfigValueOption(AutoplayMode.AUTO.value),
        ConfigValueOption(AutoplayMode.SIMILAR.value, disabled=not similar_available),
        ConfigValueOption(AutoplayMode.LIBRARY.value),
        ConfigValueOption(AutoplayMode.PLAYLIST.value),
    ]
    if per_queue:
        mode_options.append(ConfigValueOption(CONF_VALUE_GLOBAL))
    return [
        ConfigEntry(
            key=CONF_AUTOPLAY_LABEL,
            type=ConfigEntryType.LABEL,
            category=CATEGORY_AUTOPLAY,
        ),
        ConfigEntry(
            key=CONF_AUTOPLAY_MODE,
            type=ConfigEntryType.STRING,
            options=mode_options,
            default_value=CONF_VALUE_GLOBAL if per_queue else AUTOPLAY_MODE_DEFAULT_VALUE,
            category=CATEGORY_AUTOPLAY,
        ),
        ConfigEntry(
            key=CONF_AUTOPLAY_PLAYLIST,
            type=ConfigEntryType.STRING,
            options=playlist_options or [],
            default_value=None,
            required=False,
            depends_on=CONF_AUTOPLAY_MODE,
            depends_on_value=AutoplayMode.PLAYLIST.value,
            category=CATEGORY_AUTOPLAY,
        ),
    ]


def _crossfade_entries(mass: MusicAssistant, *, per_queue: bool) -> list[ConfigEntry]:
    """Crossfade entries: the mode select (per-queue overridable) + global-only duration."""
    smart_fades_available = mass.streams.smart_fades_available
    mode_options = [
        ConfigValueOption(CrossfadeMode.STANDARD_CROSSFADE.value),
        ConfigValueOption(CrossfadeMode.SMART_CROSSFADE.value, disabled=not smart_fades_available),
    ]
    if per_queue:
        mode_options.append(ConfigValueOption(CONF_VALUE_GLOBAL))
    global_default = (
        CrossfadeMode.SMART_CROSSFADE.value
        if smart_fades_available
        else CrossfadeMode.STANDARD_CROSSFADE.value
    )
    entries = [
        ConfigEntry(
            key=CONF_CROSSFADE_LABEL,
            type=ConfigEntryType.LABEL,
            category=CATEGORY_CROSSFADE,
        ),
        ConfigEntry(
            key=CONF_CROSSFADE_MODE,
            type=ConfigEntryType.STRING,
            options=mode_options,
            default_value=CONF_VALUE_GLOBAL if per_queue else global_default,
            category=CATEGORY_CROSSFADE,
            # per-queue override restarts just that queue; a global change applies on the next track
            requires_reload=per_queue,
        ),
    ]
    if not per_queue:
        # a numeric range can't carry a "global" option, so the duration is global-only
        entries.append(CONF_ENTRY_CROSSFADE_DURATION)
    return entries


def _volume_normalization_entry(*, per_queue: bool) -> ConfigEntry:
    """Volume-normalization enabled/disabled select (per-queue adds the "global" option)."""
    # a per-queue change restarts playback so the (re)baked gain filter applies right away
    return _onoff_entry(
        CONF_VOLUME_NORMALIZATION,
        CATEGORY_AUDIO,
        per_queue=per_queue,
        global_default=CONF_VALUE_ENABLED,
        reload_on_change=True,
    )
