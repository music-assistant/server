"""All constants for Music Assistant."""

import json
import os
import pathlib
from copy import deepcopy
from typing import Any, Final, cast

from music_assistant_models.config_entries import (
    MULTI_VALUE_SPLITTER,
    ConfigEntry,
    ConfigValueOption,
)
from music_assistant_models.constants import PLAYER_CONTROL_NONE
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    CrossfadeMode,
    MediaType,
    PlayerFeature,
)
from music_assistant_models.media_items import Audiobook, AudioFormat, PodcastEpisode, Radio, Track

APPLICATION_NAME: Final = "Music Assistant"

# Type alias for items that can be added to playlists
PlaylistPlayableItem = Track | Radio | PodcastEpisode | Audiobook

# Default number of tracks a music provider may return as a preview sample for a dynamic playlist
DYNAMIC_PLAYLIST_SAMPLE_SIZE: Final[int] = 25

# Corresponding MediaType enum values (must match PlaylistPlayableItem types above)
PLAYLIST_MEDIA_TYPES: Final[tuple[MediaType, ...]] = (
    MediaType.TRACK,
    MediaType.RADIO,
    MediaType.PODCAST_EPISODE,
    MediaType.AUDIOBOOK,
)

# API_SCHEMA_VERSION: bump this when adding new features to the API commands (and models)
# or small non-breaking changes to existing commands
API_SCHEMA_VERSION: Final[int] = 38

# MIN_SCHEMA_VERSION is the minimum API schema version that the current server
# version can work with. Only bump when there are breaking changes to existing
# API commands or models, such as removing fields or changing field types.
# Note that doing so will break compatibility with all clients in the field
# (including Home Assistant) that have not yet been updated to the new API
# schema version, so only bump this when absolutely necessary.
MIN_SCHEMA_VERSION: Final[int] = 28


MASS_LOGGER_NAME: Final[str] = "music_assistant"

# Home Assistant system user
HOMEASSISTANT_SYSTEM_USER: Final[str] = "homeassistant_system"
# Port used by the internal ingress webserver for the HA integration
INGRESS_SERVER_PORT: Final[int] = 8094

UNKNOWN_ARTIST: Final[str] = "[unknown]"
UNKNOWN_ARTIST_ID_MBID: Final[str] = "125ec42a-7229-4250-afc5-e057484327fe"
VARIOUS_ARTISTS_NAME: Final[str] = "Various Artists"
VARIOUS_ARTISTS_MBID: Final[str] = "89ad4ac3-39f7-470e-963a-56509c546377"


RESOURCES_DIR: Final[pathlib.Path] = (
    pathlib.Path(__file__).parent.resolve().joinpath("helpers/resources")
)
GENRE_ICONS_DIR_NAME: Final[str] = "genres"
GENRE_MAPPING_FILE: Final[pathlib.Path] = RESOURCES_DIR.joinpath(
    GENRE_ICONS_DIR_NAME, "genre_mapping.json"
)
PODCAST_GENRE_MAPPING_FILE: Final[pathlib.Path] = RESOURCES_DIR.joinpath(
    GENRE_ICONS_DIR_NAME, "podcast_genre_mapping.json"
)
AUDIOBOOK_GENRE_MAPPING_FILE: Final[pathlib.Path] = RESOURCES_DIR.joinpath(
    GENRE_ICONS_DIR_NAME, "audiobook_genre_mapping.json"
)

ANNOUNCE_ALERT_FILE: Final[str] = str(RESOURCES_DIR.joinpath("announce.mp3"))
SILENCE_FILE: Final[str] = str(RESOURCES_DIR.joinpath("silence.mp3"))
SILENCE_FILE_LONG: Final[str] = str(RESOURCES_DIR.joinpath("silence_long.ogg"))
VARIOUS_ARTISTS_FANART: Final[str] = str(RESOURCES_DIR.joinpath("fallback_fanart.jpeg"))
MASS_LOGO: Final[str] = str(RESOURCES_DIR.joinpath("logo.png"))


# config keys
CONF_ONBOARD_DONE: Final[str] = "onboard_done"
CONF_SERVER_ID: Final[str] = "server_id"
CONF_ENCRYPTION_KEY: Final[str] = "encryption_key"
CONF_ENCRYPTION_KEY_MIGRATED: Final[str] = "encryption_key_migrated"
CONF_IP_ADDRESS: Final[str] = "ip_address"
CONF_PORT: Final[str] = "port"
CONF_PROVIDERS: Final[str] = "providers"
CONF_PLAYERS: Final[str] = "players"
CONF_PLAYER_QUEUES: Final[str] = "player_queues"
CONF_CORE: Final[str] = "core"
CONF_PATH: Final[str] = "path"
CONF_NAME: Final[str] = "name"
CONF_USERNAME: Final[str] = "username"
CONF_PASSWORD: Final[str] = "password"
CONF_VOLUME_NORMALIZATION: Final[str] = "volume_normalization"
CONF_VOLUME_NORMALIZATION_TARGET: Final[str] = "volume_normalization_target"
CONF_OUTPUT_LIMITER: Final[str] = "output_limiter"
CONF_PLAYER_DSP: Final[str] = "player_dsp"
CONF_PLAYER_DSP_PRESETS: Final[str] = "player_dsp_presets"
CONF_OUTPUT_CHANNELS: Final[str] = "output_channels"
CONF_FLOW_MODE: Final[str] = "flow_mode"
CONF_FLOW_MODE_SAMPLE_RATE: Final[str] = "flow_mode_sample_rate"
CONF_LOG_LEVEL: Final[str] = "log_level"
CONF_HIDE_GROUP_CHILDS: Final[str] = "hide_group_childs"
CONF_CROSSFADE_DURATION: Final[str] = "crossfade_duration"
CONF_BIND_IP: Final[str] = "bind_ip"
CONF_BIND_PORT: Final[str] = "bind_port"
CONF_PUBLISH_IP: Final[str] = "publish_ip"
WILDCARD_BIND_IPS: Final[tuple[str, ...]] = ("0.0.0.0", "::")
CONF_AUTO_PLAY: Final[str] = "auto_play"
CONF_PLAY_MEDIA_OVERRIDES_GROUP: Final[str] = "play_media_overrides_group"
CONF_GROUP_MEMBERS: Final[str] = "group_members"
CONF_DYNAMIC_GROUP_MEMBERS: Final[str] = "dynamic_members"
CONF_HIDE_IN_UI: Final[str] = "hide_in_ui"
CONF_EXPOSE_PLAYER_TO_HA: Final[str] = "expose_player_to_ha"
CONF_SYNC_ADJUST: Final[str] = "sync_adjust"
CONF_TTS_PRE_ANNOUNCE: Final[str] = "tts_pre_announce"
CONF_ANNOUNCE_VOLUME_STRATEGY: Final[str] = "announce_volume_strategy"
CONF_ANNOUNCE_VOLUME: Final[str] = "announce_volume"
CONF_ANNOUNCE_VOLUME_MIN: Final[str] = "announce_volume_min"
CONF_ANNOUNCE_VOLUME_MAX: Final[str] = "announce_volume_max"
CONF_PRE_ANNOUNCE_CHIME_URL: Final[str] = "pre_announcement_chime_url"
CONF_ICON: Final[str] = "icon"
CONF_LANGUAGE: Final[str] = "language"
CONF_SAMPLE_RATES: Final[str] = "sample_rates"
CONF_HTTP_PROFILE: Final[str] = "http_profile"
CONF_BYPASS_NORMALIZATION_RADIO: Final[str] = "bypass_normalization_radio"
CONF_ENABLE_ICY_METADATA: Final[str] = "enable_icy_metadata"
CONF_VOLUME_NORMALIZATION_RADIO: Final[str] = "volume_normalization_radio"
CONF_VOLUME_NORMALIZATION_TRACKS: Final[str] = "volume_normalization_tracks"
CONF_VOLUME_NORMALIZATION_FIXED_GAIN_RADIO: Final[str] = "volume_normalization_fixed_gain_radio"
CONF_VOLUME_NORMALIZATION_FIXED_GAIN_TRACKS: Final[str] = "volume_normalization_fixed_gain_tracks"
CONF_POWER_CONTROL: Final[str] = "power_control"
CONF_VOLUME_CONTROL: Final[str] = "volume_control"
CONF_MUTE_CONTROL: Final[str] = "mute_control"
CONF_MIN_VOLUME: Final[str] = "min_volume"
CONF_MAX_VOLUME: Final[str] = "max_volume"
CONF_PREFERRED_OUTPUT_PROTOCOL: Final[str] = "preferred_output_protocol"
CONF_LINKED_PROTOCOL_IDS: Final[str] = "linked_protocol_ids"  # cached for fast restart
CONF_PROTOCOL_PARENT_ID: Final[str] = (
    "protocol_parent_id"  # cached native player ID for protocol player
)
CONF_UNDERLYING_PLAYER_ID: Final[str] = (
    "underlying_player_id"  # player this (bridge) protocol player is derived from
)
CONF_CACHED_ARP_MAC: Final[str] = "cached_arp_mac"  # cached ARP-resolved MAC for fast restart
CONF_REPORTED_MAC: Final[str] = "reported_mac"  # original MAC reported by provider (before ARP)
CONF_OUTPUT_CODEC: Final[str] = "output_codec"
CONF_ALLOW_AUDIO_CACHE: Final[str] = "allow_audio_cache"
CONF_SMART_FADES_MODE: Final[str] = "smart_fades_mode"  # legacy; consumed by one-time migration
CONF_CROSSFADE_MODE: Final[str] = "crossfade_mode"
CONF_SOCKS_URL: Final[str] = "socks_url"
CONF_USE_SSL: Final[str] = "use_ssl"
CONF_VERIFY_SSL: Final[str] = "verify_ssl"
CONF_SSL_FINGERPRINT: Final[str] = "ssl_fingerprint"
CONF_AUTH_ALLOW_SELF_REGISTRATION: Final[str] = "auth_allow_self_registration"
CONF_ZEROCONF_INTERFACES: Final[str] = "zeroconf_interfaces"
CONF_ENABLED: Final[str] = "enabled"
CONF_PROTOCOL_KEY_SPLITTER: Final[str] = "||protocol||"
CONF_PROTOCOL_CATEGORY_PREFIX: Final[str] = "protocol"
CONF_DEFAULT_PROVIDERS_SETUP: Final[str] = "default_providers_setup"
CONF_BACKGROUND_SCAN_CONCURRENCY: Final[str] = "background_scan_concurrency"

# Tri-state option VALUES for per-queue settings that can follow the global (queue controller)
# default. "global" resolves to the queue-controller value (like the log_level "GLOBAL" pattern);
# "enabled"/"disabled" force the setting on/off for that queue. (Distinct from the CONF_ENABLED key.)
CONF_VALUE_GLOBAL: Final[str] = "global"
CONF_VALUE_ENABLED: Final[str] = "enabled"
CONF_VALUE_DISABLED: Final[str] = "disabled"

# Sentinel option VALUE for network settings that are resolved at runtime when not explicitly
# set (e.g. streams publish_ip / webserver base_url resolve to the server's primary IP at
# startup), keeping the config entry defaults static/deterministic.
CONF_VALUE_AUTO: Final[str] = "auto"


def _default_background_scan_concurrency() -> int:
    # Slow and steady by default: at most 2 tracks at once even on big machines. Users who
    # want faster overnight scans can raise it (up to 16) at the cost of more CPU at night.
    cpu_count = os.process_cpu_count() or os.cpu_count() or 4
    if cpu_count >= 4:
        return 2
    return 1


# config default values
DEFAULT_HOST: Final[str] = "0.0.0.0"
DEFAULT_PORT: Final[int] = 8095
DEFAULT_BACKGROUND_SCAN_CONCURRENCY: Final[int] = _default_background_scan_concurrency()


# common db tables
DB_TABLE_PLAYLOG: Final[str] = "playlog"
DB_TABLE_ARTISTS: Final[str] = "artists"
DB_TABLE_ALBUMS: Final[str] = "albums"
DB_TABLE_TRACKS: Final[str] = "tracks"
DB_TABLE_PLAYLISTS: Final[str] = "playlists"
DB_TABLE_RADIOS: Final[str] = "radios"
DB_TABLE_AUDIOBOOKS: Final[str] = "audiobooks"
DB_TABLE_PODCASTS: Final[str] = "podcasts"
DB_TABLE_CACHE: Final[str] = "cache"
DB_TABLE_SETTINGS: Final[str] = "settings"
DB_TABLE_THUMBS: Final[str] = "thumbnails"
DB_TABLE_PROVIDER_MAPPINGS: Final[str] = "provider_mappings"
DB_TABLE_EXTERNAL_ID_LOOKUP: Final[str] = "external_id_lookup"
DB_TABLE_ALBUM_TRACKS: Final[str] = "album_tracks"
DB_TABLE_TRACK_ARTISTS: Final[str] = "track_artists"
DB_TABLE_ALBUM_ARTISTS: Final[str] = "album_artists"
DB_TABLE_AUDIOBOOK_ARTISTS: Final[str] = "audiobook_artists"
DB_TABLE_LOUDNESS_MEASUREMENTS: Final[str] = "loudness_measurements"
DB_TABLE_AUDIO_ANALYSIS: Final[str] = "audio_analysis"
DB_TABLE_AUDIO_ANALYSIS_FAILURES: Final[str] = "audio_analysis_failures"
DB_TABLE_GENRES: Final[str] = "genres"
DB_TABLE_GENRE_MEDIA_ITEM_MAPPING: Final[str] = "genre_media_item_mapping"
DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION: Final[str] = "genre_media_item_exclusion"

# all media item tables, each of which has a search_name column
# backed by a {table}_fts FTS5 index table
MEDIA_ITEM_DB_TABLES: Final[tuple[str, ...]] = (
    DB_TABLE_ARTISTS,
    DB_TABLE_ALBUMS,
    DB_TABLE_TRACKS,
    DB_TABLE_PLAYLISTS,
    DB_TABLE_RADIOS,
    DB_TABLE_AUDIOBOOKS,
    DB_TABLE_PODCASTS,
    DB_TABLE_GENRES,
)

# Min fraction of a database file reclaimable before a startup VACUUM is worth running.
VACUUM_MIN_RECLAIM_RATIO: Final[float] = 0.2

# Loudness measurements at or below this value are considered unreliable:
# ebur128 reports ~-70 LUFS when it receives near-silence or very little
# audio (e.g. when a stream was cancelled early).
LOUDNESS_MEASUREMENT_MIN_LUFS: Final[float] = -50.0


def load_genre_mapping(mapping_file: pathlib.Path) -> list[dict[str, Any]]:
    """
    Load a default genre mapping from a JSON file.

    :param mapping_file: Path to the genre mapping JSON file (music / podcast / audiobook).
    :return: List of genre mapping dictionaries with 'genre' and 'aliases' keys.
    :raises FileNotFoundError: If the mapping file is missing.
    :raises ValueError: If JSON is malformed or missing required fields.
    """
    try:
        content = mapping_file.read_text(encoding="utf-8")
        data = json.loads(content)
    except FileNotFoundError as err:
        msg = f"Genre mapping file not found: {mapping_file}"
        raise FileNotFoundError(msg) from err
    except json.JSONDecodeError as err:
        msg = f"Invalid JSON in genre mapping file: {mapping_file}"
        raise ValueError(msg) from err

    if not isinstance(data, list):
        msg = f"Genre mapping must be a list, got {type(data).__name__}"
        raise TypeError(msg)

    for idx, entry in enumerate(data):
        if not isinstance(entry, dict):
            msg = f"Genre mapping entry {idx} must be a dict, got {type(entry).__name__}"
            raise TypeError(msg)
        if "genre" not in entry:
            msg = f"Genre mapping entry {idx} missing required field 'genre'"
            raise ValueError(msg)
        if "aliases" not in entry:
            msg = f"Genre mapping entry {idx} missing required field 'aliases'"
            raise ValueError(msg)

    return cast("list[dict[str, Any]]", data)


DEFAULT_GENRE_MAPPING: Final[list[dict[str, Any]]] = load_genre_mapping(GENRE_MAPPING_FILE)
DEFAULT_PODCAST_GENRE_MAPPING: Final[list[dict[str, Any]]] = load_genre_mapping(
    PODCAST_GENRE_MAPPING_FILE
)
DEFAULT_AUDIOBOOK_GENRE_MAPPING: Final[list[dict[str, Any]]] = load_genre_mapping(
    AUDIOBOOK_GENRE_MAPPING_FILE
)
DEFAULT_GENRES: Final[tuple[str, ...]] = tuple(entry["genre"] for entry in DEFAULT_GENRE_MAPPING)


# all other
MASS_LOGO_ONLINE: Final[str] = (
    "https://raw.githubusercontent.com/music-assistant/server/refs/heads/dev/music_assistant/logo.png"
)
ENCRYPT_SUFFIX = "_encrypted_"
CONFIGURABLE_CORE_CONTROLLERS = (
    "discovery",
    "streams",
    "webserver",
    "players",
    "metadata",
    "cache",
    "music",
    "player_queues",
)
VERBOSE_LOG_LEVEL: Final[int] = 5
PROVIDERS_WITH_SHAREABLE_URLS = ("spotify", "qobuz", "apple_music", "deezer")


####### REUSABLE CONFIG ENTRIES #######

CONF_ENTRY_LOG_LEVEL = ConfigEntry(
    key=CONF_LOG_LEVEL,
    type=ConfigEntryType.STRING,
    options=[
        ConfigValueOption("GLOBAL"),
        ConfigValueOption("INFO"),
        ConfigValueOption("WARNING"),
        ConfigValueOption("ERROR"),
        ConfigValueOption("DEBUG"),
        ConfigValueOption("VERBOSE"),
    ],
    default_value="GLOBAL",
    advanced=True,
    requires_reload=False,  # applied dynamically via _set_logger()
)

DEFAULT_PROVIDER_CONFIG_ENTRIES = (CONF_ENTRY_LOG_LEVEL,)
DEFAULT_CORE_CONFIG_ENTRIES = (CONF_ENTRY_LOG_LEVEL,)

# some reusable player config entries

CONF_ENTRY_FLOW_MODE = ConfigEntry(
    key=CONF_FLOW_MODE,
    type=ConfigEntryType.BOOLEAN,
    default_value=False,
    category="protocol_generic",
    advanced=True,
    requires_reload=True,
)


FLOW_MODE_SAMPLE_RATE_SMART: Final[str] = "smart"
FLOW_MODE_SAMPLE_RATE_BIT_PERFECT: Final[str] = "bit_perfect"
FLOW_MODE_SAMPLE_RATE_48000: Final[str] = "48000"
FLOW_MODE_SAMPLE_RATE_96000: Final[str] = "96000"
FLOW_MODE_SAMPLE_RATE_HIGHEST: Final[str] = "highest"

CONF_ENTRY_FLOW_MODE_SAMPLE_RATE = ConfigEntry(
    key=CONF_FLOW_MODE_SAMPLE_RATE,
    type=ConfigEntryType.STRING,
    options=[
        ConfigValueOption(FLOW_MODE_SAMPLE_RATE_SMART),
        ConfigValueOption(FLOW_MODE_SAMPLE_RATE_BIT_PERFECT),
        ConfigValueOption(FLOW_MODE_SAMPLE_RATE_48000),
        ConfigValueOption(FLOW_MODE_SAMPLE_RATE_96000),
        ConfigValueOption(FLOW_MODE_SAMPLE_RATE_HIGHEST),
    ],
    default_value=FLOW_MODE_SAMPLE_RATE_SMART,
    category="protocol_generic",
    advanced=True,
    requires_reload=True,
)


CONF_ENTRY_AUTO_PLAY = ConfigEntry(
    key=CONF_AUTO_PLAY,
    type=ConfigEntryType.BOOLEAN,
    default_value=False,
    depends_on=CONF_POWER_CONTROL,
    depends_on_value_not="none",
    category="player_controls",
)

CONF_ENTRY_PLAY_MEDIA_OVERRIDES_GROUP = ConfigEntry(
    key=CONF_PLAY_MEDIA_OVERRIDES_GROUP,
    type=ConfigEntryType.BOOLEAN,
    default_value=True,
    category="generic",
    advanced=False,
)

CONF_ENTRY_MIN_VOLUME = ConfigEntry(
    key=CONF_MIN_VOLUME,
    type=ConfigEntryType.INTEGER,
    range=(0, 100),
    default_value=0,
    category="player_controls",
    advanced=True,
    depends_on=CONF_VOLUME_CONTROL,
    depends_on_value_not=PLAYER_CONTROL_NONE,
)

CONF_ENTRY_MAX_VOLUME = ConfigEntry(
    key=CONF_MAX_VOLUME,
    type=ConfigEntryType.INTEGER,
    range=(0, 100),
    default_value=100,
    category="player_controls",
    advanced=True,
    depends_on=CONF_VOLUME_CONTROL,
    depends_on_value_not=PLAYER_CONTROL_NONE,
)

CONF_ENTRY_OUTPUT_CHANNELS = ConfigEntry(
    key=CONF_OUTPUT_CHANNELS,
    type=ConfigEntryType.STRING,
    options=[
        ConfigValueOption("stereo"),
        ConfigValueOption("left"),
        ConfigValueOption("right"),
        ConfigValueOption("mono"),
    ],
    default_value="stereo",
    category="protocol_generic",
    advanced=True,
    requires_reload=True,
)

CONF_ENTRY_VOLUME_NORMALIZATION_TARGET = ConfigEntry(
    key=CONF_VOLUME_NORMALIZATION_TARGET,
    type=ConfigEntryType.INTEGER,
    range=(-30, -5),
    default_value=-14,
    category="playback",
    advanced=True,
    requires_reload=True,
)

CONF_ENTRY_OUTPUT_LIMITER = ConfigEntry(
    key=CONF_OUTPUT_LIMITER,
    type=ConfigEntryType.BOOLEAN,
    default_value=True,
    category="playback",
    advanced=True,
    requires_reload=True,
)


# Note: the crossfade_mode select entry (standard/smart) is built dynamically in the config
# controller because its options and default depend on smart fades availability.

# Crossfade duration is a global-only (queue controller) setting; it is read fresh per stream, so a
# change applies on the next track (no reload needed — a reload of the queues core is disruptive).
CONF_ENTRY_CROSSFADE_DURATION = ConfigEntry(
    key=CONF_CROSSFADE_DURATION,
    type=ConfigEntryType.INTEGER,
    range=(1, 15),
    default_value=8,
    category="crossfade",
    depends_on=CONF_CROSSFADE_MODE,
    depends_on_value=CrossfadeMode.STANDARD_CROSSFADE.value,
)


CONF_ENTRY_OUTPUT_CODEC = ConfigEntry(
    key=CONF_OUTPUT_CODEC,
    type=ConfigEntryType.STRING,
    default_value="flac",
    options=[
        ConfigValueOption("flac"),
        ConfigValueOption("mp3"),
        ConfigValueOption("aac"),
        ConfigValueOption("wav"),
    ],
    category="protocol_generic",
    advanced=True,
    requires_reload=True,
)

CONF_ENTRY_OUTPUT_CODEC_DEFAULT_MP3 = ConfigEntry.from_dict(
    {**CONF_ENTRY_OUTPUT_CODEC.to_dict(), "default_value": "mp3"}
)
CONF_ENTRY_OUTPUT_CODEC_ENFORCE_MP3 = ConfigEntry.from_dict(
    {**CONF_ENTRY_OUTPUT_CODEC.to_dict(), "default_value": "mp3", "hidden": True}
)
CONF_ENTRY_OUTPUT_CODEC_HIDDEN = ConfigEntry.from_dict(
    {**CONF_ENTRY_OUTPUT_CODEC.to_dict(), "hidden": True}
)
CONF_ENTRY_OUTPUT_CODEC_ENFORCE_FLAC = ConfigEntry.from_dict(
    {**CONF_ENTRY_OUTPUT_CODEC.to_dict(), "default_value": "flac", "hidden": True}
)


def create_output_codec_config_entry(
    hidden: bool = False, default_value: str = "flac"
) -> ConfigEntry:
    """Create output codec config entry based on player specific helpers."""
    conf_entry = ConfigEntry.from_dict(CONF_ENTRY_OUTPUT_CODEC.to_dict())
    conf_entry.hidden = hidden
    conf_entry.default_value = default_value
    return conf_entry


CONF_ENTRY_SYNC_ADJUST = ConfigEntry(
    key=CONF_SYNC_ADJUST,
    type=ConfigEntryType.INTEGER,
    range=(-500, 500),
    default_value=0,
    category="protocol_generic",
    advanced=True,
    requires_reload=True,
)


CONF_ENTRY_TTS_PRE_ANNOUNCE = ConfigEntry(
    key=CONF_TTS_PRE_ANNOUNCE,
    type=ConfigEntryType.BOOLEAN,
    default_value=True,
    category="announcements",
)


CONF_ENTRY_ANNOUNCE_VOLUME_STRATEGY = ConfigEntry(
    key=CONF_ANNOUNCE_VOLUME_STRATEGY,
    type=ConfigEntryType.STRING,
    options=[
        ConfigValueOption("absolute"),
        ConfigValueOption("relative"),
        ConfigValueOption("percentual"),
        ConfigValueOption("none"),
    ],
    default_value="percentual",
    category="announcements",
)

CONF_ENTRY_ANNOUNCE_VOLUME_STRATEGY_HIDDEN = ConfigEntry.from_dict(
    {**CONF_ENTRY_ANNOUNCE_VOLUME_STRATEGY.to_dict(), "hidden": True}
)

CONF_ENTRY_ANNOUNCE_VOLUME = ConfigEntry(
    key=CONF_ANNOUNCE_VOLUME,
    type=ConfigEntryType.INTEGER,
    default_value=85,
    category="announcements",
)
CONF_ENTRY_ANNOUNCE_VOLUME_HIDDEN = ConfigEntry.from_dict(
    {**CONF_ENTRY_ANNOUNCE_VOLUME.to_dict(), "hidden": True}
)

CONF_ENTRY_ANNOUNCE_VOLUME_MIN = ConfigEntry(
    key=CONF_ANNOUNCE_VOLUME_MIN,
    type=ConfigEntryType.INTEGER,
    default_value=15,
    category="announcements",
)
CONF_ENTRY_ANNOUNCE_VOLUME_MIN_HIDDEN = ConfigEntry.from_dict(
    {**CONF_ENTRY_ANNOUNCE_VOLUME_MIN.to_dict(), "hidden": True}
)

CONF_ENTRY_ANNOUNCE_VOLUME_MAX = ConfigEntry(
    key=CONF_ANNOUNCE_VOLUME_MAX,
    type=ConfigEntryType.INTEGER,
    default_value=75,
    category="announcements",
)
CONF_ENTRY_ANNOUNCE_VOLUME_MAX_HIDDEN = ConfigEntry.from_dict(
    {**CONF_ENTRY_ANNOUNCE_VOLUME_MAX.to_dict(), "hidden": True}
)


HIDDEN_ANNOUNCE_VOLUME_CONFIG_ENTRIES = (
    CONF_ENTRY_ANNOUNCE_VOLUME_HIDDEN,
    CONF_ENTRY_ANNOUNCE_VOLUME_MIN_HIDDEN,
    CONF_ENTRY_ANNOUNCE_VOLUME_MAX_HIDDEN,
    CONF_ENTRY_ANNOUNCE_VOLUME_STRATEGY_HIDDEN,
)


CONF_ENTRY_SAMPLE_RATES = ConfigEntry(
    key=CONF_SAMPLE_RATES,
    type=ConfigEntryType.SPLITTED_STRING,
    multi_value=True,
    options=[
        ConfigValueOption(f"44100{MULTI_VALUE_SPLITTER}16"),
        ConfigValueOption(f"44100{MULTI_VALUE_SPLITTER}24"),
        ConfigValueOption(f"48000{MULTI_VALUE_SPLITTER}16"),
        ConfigValueOption(f"48000{MULTI_VALUE_SPLITTER}24"),
        ConfigValueOption(f"88200{MULTI_VALUE_SPLITTER}16"),
        ConfigValueOption(f"88200{MULTI_VALUE_SPLITTER}24"),
        ConfigValueOption(f"96000{MULTI_VALUE_SPLITTER}16"),
        ConfigValueOption(f"96000{MULTI_VALUE_SPLITTER}24"),
        ConfigValueOption(f"176400{MULTI_VALUE_SPLITTER}16"),
        ConfigValueOption(f"176400{MULTI_VALUE_SPLITTER}24"),
        ConfigValueOption(f"192000{MULTI_VALUE_SPLITTER}16"),
        ConfigValueOption(f"192000{MULTI_VALUE_SPLITTER}24"),
        ConfigValueOption(f"352800{MULTI_VALUE_SPLITTER}16"),
        ConfigValueOption(f"352800{MULTI_VALUE_SPLITTER}24"),
        ConfigValueOption(f"384000{MULTI_VALUE_SPLITTER}16"),
        ConfigValueOption(f"384000{MULTI_VALUE_SPLITTER}24"),
    ],
    default_value=[f"44100{MULTI_VALUE_SPLITTER}16", f"48000{MULTI_VALUE_SPLITTER}16"],
    required=True,
    category="protocol_generic",
    advanced=True,
    requires_reload=True,
)


CONF_ENTRY_HTTP_PROFILE = ConfigEntry(
    key=CONF_HTTP_PROFILE,
    type=ConfigEntryType.STRING,
    options=[
        ConfigValueOption("chunked"),
        ConfigValueOption("no_content_length"),
        ConfigValueOption("forced_content_length"),
    ],
    default_value="no_content_length",
    category="protocol_generic",
    advanced=True,
    requires_reload=True,
)

CONF_ENTRY_HTTP_PROFILE_DEFAULT_1 = ConfigEntry.from_dict(
    {**CONF_ENTRY_HTTP_PROFILE.to_dict(), "default_value": "chunked"}
)

CONF_ENTRY_HTTP_PROFILE_DEFAULT_2 = ConfigEntry.from_dict(
    {**CONF_ENTRY_HTTP_PROFILE.to_dict(), "default_value": "no_content_length"}
)
CONF_ENTRY_HTTP_PROFILE_DEFAULT_3 = ConfigEntry.from_dict(
    {**CONF_ENTRY_HTTP_PROFILE.to_dict(), "default_value": "forced_content_length"}
)

CONF_ENTRY_HTTP_PROFILE_FORCED_1 = ConfigEntry.from_dict(
    {**CONF_ENTRY_HTTP_PROFILE_DEFAULT_1.to_dict(), "hidden": True}
)
CONF_ENTRY_HTTP_PROFILE_FORCED_2 = ConfigEntry.from_dict(
    {
        **CONF_ENTRY_HTTP_PROFILE.to_dict(),
        "default_value": "no_content_length",
        "hidden": True,
    }
)
CONF_ENTRY_HTTP_PROFILE_HIDDEN = ConfigEntry.from_dict(
    {**CONF_ENTRY_HTTP_PROFILE.to_dict(), "hidden": True}
)


CONF_ENTRY_ENABLE_ICY_METADATA = ConfigEntry(
    key=CONF_ENABLE_ICY_METADATA,
    type=ConfigEntryType.STRING,
    options=[
        ConfigValueOption("disabled"),
        ConfigValueOption("basic"),
        ConfigValueOption("full"),
    ],
    depends_on=CONF_FLOW_MODE,
    depends_on_value_not=False,
    default_value="disabled",
    category="protocol_generic",
    advanced=True,
    requires_reload=True,
)

CONF_ENTRY_ENABLE_ICY_METADATA_HIDDEN = ConfigEntry.from_dict(
    {**CONF_ENTRY_ENABLE_ICY_METADATA.to_dict(), "hidden": True}
)

CONF_ENTRY_ICY_METADATA_HIDDEN_DISABLED = ConfigEntry.from_dict(
    {
        **CONF_ENTRY_ENABLE_ICY_METADATA.to_dict(),
        "default_value": "disabled",
        "value": "disabled",
        "hidden": True,
    }
)

CONF_ENTRY_ICY_METADATA_DEFAULT_FULL = ConfigEntry.from_dict(
    {
        **CONF_ENTRY_ENABLE_ICY_METADATA.to_dict(),
        "default_value": "full",
    }
)

CONF_ENTRY_CROSSFADE_DIFFERENT_SAMPLE_RATES = ConfigEntry(
    key="crossfade_different_sample_rates",
    type=ConfigEntryType.BOOLEAN,
    default_value=True,
    category="protocol_generic",
    advanced=True,
    requires_reload=True,
    depends_on=CONF_FLOW_MODE,
    depends_on_value_not=True,
)

CONF_ENTRY_WARN_PREVIEW = ConfigEntry(
    key="preview_note",
    type=ConfigEntryType.ALERT,
    required=False,
)

CONF_ENTRY_UNOFFICIAL_PROVIDER = ConfigEntry(
    key="unofficial_provider_note",
    type=ConfigEntryType.ALERT,
    required=False,
)

CONF_ENTRY_MANUAL_DISCOVERY_IPS = ConfigEntry(
    key="manual_discovery_ip_addresses",
    type=ConfigEntryType.STRING,
    advanced=True,
    default_value=[],
    required=False,
    multi_value=True,
)

CONF_ENTRY_LIBRARY_SYNC_ARTISTS = ConfigEntry(
    key="library_sync_artists",
    type=ConfigEntryType.BOOLEAN,
    default_value=True,
    category="sync_options",
)


CONF_ENTRY_ZEROCONF_INTERFACES = ConfigEntry(
    key=CONF_ZEROCONF_INTERFACES,
    type=ConfigEntryType.STRING,
    options=[
        ConfigValueOption("default"),
        ConfigValueOption("all"),
    ],
    default_value="default",
    advanced=True,
    requires_reload=True,
)
CONF_ENTRY_LIBRARY_SYNC_ALBUMS = ConfigEntry(
    key="library_sync_albums",
    type=ConfigEntryType.BOOLEAN,
    default_value=True,
    category="sync_options",
)
CONF_ENTRY_LIBRARY_SYNC_TRACKS = ConfigEntry(
    key="library_sync_tracks",
    type=ConfigEntryType.BOOLEAN,
    default_value=True,
    category="sync_options",
)
CONF_ENTRY_LIBRARY_SYNC_PLAYLISTS = ConfigEntry(
    key="library_sync_playlists",
    type=ConfigEntryType.BOOLEAN,
    default_value=True,
    category="sync_options",
)
CONF_ENTRY_LIBRARY_SYNC_PODCASTS = ConfigEntry(
    key="library_sync_podcasts",
    type=ConfigEntryType.BOOLEAN,
    default_value=True,
    category="sync_options",
)
CONF_ENTRY_LIBRARY_SYNC_AUDIOBOOKS = ConfigEntry(
    key="library_sync_audiobooks",
    type=ConfigEntryType.BOOLEAN,
    default_value=True,
    category="sync_options",
)
CONF_ENTRY_LIBRARY_SYNC_RADIOS = ConfigEntry(
    key="library_sync_radios",
    type=ConfigEntryType.BOOLEAN,
    default_value=True,
    category="sync_options",
)
CONF_ENTRY_LIBRARY_SYNC_ALBUM_TRACKS = ConfigEntry(
    key="library_sync_album_tracks",
    type=ConfigEntryType.BOOLEAN,
    default_value=False,
    category="sync_options",
)
CONF_ENTRY_LIBRARY_SYNC_PLAYLIST_TRACKS = ConfigEntry(
    key="library_sync_playlist_tracks",
    type=ConfigEntryType.STRING,
    default_value=[],
    category="sync_options",
    multi_value=True,
)

CONF_ENTRY_LIBRARY_SYNC_BACK = ConfigEntry(
    key="library_sync_back",
    type=ConfigEntryType.BOOLEAN,
    default_value=True,
    category="sync_options",
)

CONF_ENTRY_LIBRARY_SYNC_DELETIONS = ConfigEntry(
    key="library_sync_deletions",
    type=ConfigEntryType.BOOLEAN,
    default_value=True,
    category="sync_options",
    advanced=True,
)

CONF_ENTRY_PLAYER_ICON = ConfigEntry(
    key=CONF_ICON,
    type=ConfigEntryType.ICON,
    default_value="mdi-speaker",
    category="generic",
)

CONF_ENTRY_PLAYER_ICON_GROUP = ConfigEntry.from_dict(
    {**CONF_ENTRY_PLAYER_ICON.to_dict(), "default_value": "mdi-speaker-multiple"}
)


def create_sample_rates_config_entry(
    supported_sample_rates: list[int] | None = None,
    supported_bit_depths: list[int] | None = None,
    hidden: bool = False,
    max_sample_rate: int | None = None,
    max_bit_depth: int | None = None,
    safe_max_sample_rate: int = 48000,
    safe_max_bit_depth: int = 16,
) -> ConfigEntry:
    """Create sample rates config entry based on player specific helpers."""
    assert CONF_ENTRY_SAMPLE_RATES.options
    # if no supported sample rates are defined, we apply the default 44100 as only option
    if not supported_sample_rates and max_sample_rate is None:
        supported_sample_rates = [44100]
    if not supported_bit_depths and max_bit_depth is None:
        supported_bit_depths = [16]
    final_supported_sample_rates = supported_sample_rates or []
    final_supported_bit_depths = supported_bit_depths or []
    conf_entry = deepcopy(CONF_ENTRY_SAMPLE_RATES)
    conf_entry.hidden = hidden
    options: list[ConfigValueOption] = []
    default_value: list[str] = []

    for option in CONF_ENTRY_SAMPLE_RATES.options:
        option_value = cast("str", option.value)
        sample_rate_str, bit_depth_str = option_value.split(MULTI_VALUE_SPLITTER, 1)
        sample_rate = int(sample_rate_str)
        bit_depth = int(bit_depth_str)
        # if no supported sample rates are defined, we accept all within max_sample_rate
        if not supported_sample_rates and max_sample_rate and sample_rate <= max_sample_rate:
            final_supported_sample_rates.append(sample_rate)
        if not supported_bit_depths and max_bit_depth and bit_depth <= max_bit_depth:
            final_supported_bit_depths.append(bit_depth)

        if sample_rate not in final_supported_sample_rates:
            continue
        if bit_depth not in final_supported_bit_depths:
            continue
        options.append(option)
        if sample_rate <= safe_max_sample_rate and bit_depth <= safe_max_bit_depth:
            default_value.append(option_value)
    conf_entry.options = options
    conf_entry.default_value = default_value
    return conf_entry


DEFAULT_STREAM_HEADERS = {
    "Server": APPLICATION_NAME,
    "transferMode.dlna.org": "Streaming",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Accept-Ranges": "none",
    "Connection": "close",
    "icy-name": APPLICATION_NAME,
}

# DLNA contentFeatures header values for different stream types.
# ORG_OP=00: no time-seek, no byte-seek (we encode on-the-fly, unknown size).
# ORG_FLAGS bit layout (hex, first 8 chars of 32-char field):
#   bit 31 (0x80000000): Sender Paced - server controls data rate
#   bit 24 (0x01000000): Streaming Transfer Mode
#   bit 22 (0x00400000): Background Transfer Mode
#   bit 21 (0x00200000): HTTP Connection Stalling permitted
#   bit 20 (0x00100000): DLNA V1.5
#
# Bufferable streams (tracks, flow mode): player may buffer aggressively.
# Flags: 0x01700000 = Streaming + Background + Connection Stalling + V1.5
DLNA_CONTENT_FEATURES = "DLNA.ORG_OP=00;DLNA.ORG_FLAGS=01700000000000000000000000000000"
# Realtime streams (radio, plugin sources): server controls data rate,
# player should not try to buffer ahead faster than the server delivers.
# Flags: 0x81700000 = Sender Paced + Streaming + Background + Connection Stalling + V1.5
DLNA_CONTENT_FEATURES_REALTIME = "DLNA.ORG_OP=00;DLNA.ORG_FLAGS=81700000000000000000000000000000"
ICY_HEADERS = {
    "icy-name": APPLICATION_NAME,
    "icy-description": f"{APPLICATION_NAME} - Your personal music assistant",
    "icy-version": "1",
    "icy-logo": MASS_LOGO_ONLINE,
}

INTERNAL_PCM_FORMAT = AudioFormat(
    # always prefer float32 as internal pcm format to create headroom
    # for filters such as dsp and volume normalization
    content_type=ContentType.PCM_F32LE,
    bit_depth=32,  # related to float32
    sample_rate=48000,  # static for flow stream, dynamic for anything else
    channels=2,  # static for flow stream, dynamic for anything else
)

# Seconds without a new chunk before the source is treated as stalled (above ffmpeg's ~15s reconnect window).
STREAM_STALL_TIMEOUT: Final[int] = 20
# Longer budget for the first chunk to allow for connect + probe.
STREAM_START_TIMEOUT: Final[int] = 30

# extra data / extra attributes keys
ATTR_FAKE_POWER: Final[str] = "fake_power"
ATTR_FAKE_VOLUME: Final[str] = "fake_volume_level"
ATTR_FAKE_MUTE: Final[str] = "fake_volume_muted"
ATTR_ANNOUNCEMENT_IN_PROGRESS: Final[str] = "announcement_in_progress"
ATTR_PREVIOUS_VOLUME: Final[str] = "previous_volume"
ATTR_LAST_POLL: Final[str] = "last_poll"
ATTR_GROUP_MEMBERS: Final[str] = "group_members"
ATTR_GROUP_VOLUME_SNAPSHOT: Final[str] = "group_volume_snapshot"
ATTR_ENABLED: Final[str] = "enabled"
ATTR_AVAILABLE: Final[str] = "available"
ATTR_POWERED: Final[str] = "powered"
ATTR_MUTE_LOCK: Final[str] = "mute_lock"
ATTR_ACTIVE_SOURCE: Final[str] = "active_source"
ATTR_SUPPORTED_FEATURES: Final[str] = "supported_features"
ATTR_MUTE_CONTROL: Final[str] = "mute_control"
ATTR_VOLUME_CONTROL: Final[str] = "volume_control"
ATTR_POWER_CONTROL: Final[str] = "power_control"
ATTR_PLAY_ACTION_IN_PROGRESS: Final[str] = "play_action_in_progress"

# Album type detection patterns
LIVE_INDICATORS = [
    r"\bunplugged\b",
    r"\bin concert\b",
    r"\bon stage\b",
    r"\blive\b",
]

SOUNDTRACK_INDICATORS = [
    r"\bsoundtrack\b",  # Catches all soundtrack variations
    r"\bmusic from the .* motion picture\b",
    r"\boriginal score\b",
    r"\bthe score\b",
    r"\bfilm score\b",
    r"(^|\b)score:\s*",  # e.g., "Score: The Two Towers"
    r"\bfrom the film\b",
    r"\boriginal.*cast.*recording\b",
]

# how often we report the playback progress in the player_queues controller
PLAYBACK_REPORT_INTERVAL_SECONDS = 30

# List of providers that do not use HTTP streaming
# but consume raw audio data over other protocols
# for provider domains in this list, we won't show the default
# http-streaming specific config options in player settings
NON_HTTP_PROVIDERS = ("airplay", "sendspin", "snapcast")

# Protocol priority values (lower = more preferred)
PROTOCOL_PRIORITY: Final[dict[str, int]] = {
    "airplay": 10,
    "squeezelite": 20,
    "chromecast": 30,
    "sendspin": 40,
    "dlna": 50,
}

PROTOCOL_FEATURES: Final[set[PlayerFeature]] = {
    # Player features that may be copied from (inactive) protocol implementations
    PlayerFeature.PLAY_ANNOUNCEMENT,
    PlayerFeature.SET_MEMBERS,
}

ACTIVE_PROTOCOL_FEATURES: Final[set[PlayerFeature]] = {
    # Player features that may be copied from the active output protocol
    *PROTOCOL_FEATURES,
    PlayerFeature.ENQUEUE,
    PlayerFeature.GAPLESS_PLAYBACK,
    PlayerFeature.MULTI_DEVICE_DSP,
    PlayerFeature.PAUSE,
}

PLAYER_CONTROL_PROTOCOL: Final[str] = "follow_protocol"
DEFAULT_PROVIDERS: Final[set[tuple[str, bool]]] = {
    # list of providers that are setup by default once
    # (and they can be removed/disabled by the user if they want to)
    # the boolean value indicates whether it needs to be discovered on mdns
    ("airplay", False),
    ("chromecast", False),
    ("dlna", False),
    ("sonos", True),
    ("bluesound", True),
    ("heos", True),
    ("wiim", True),
    ("party", False),
    # smart_fades gates on system requirements (RAM/CPU) in its own setup(); an
    # under-spec host has the auto-created config removed again at load time.
    ("smart_fades", False),
    ("lastfm_recommendations", False),
    ("playlist_metadata", False),
    # ambient_sounds provides out-of-the-box sound effects (e.g. for the queue
    # audio overlay feature) at zero resource cost until actually used
    ("ambient_sounds", False),
}

EXTERNAL_SOURCES: Final[set[str]] = {
    # list of sources that are definitely considered "external"
    # values are matched case-insensitive against the player's active_source
    # streaming services / connect sources
    "spotify",
    "spotify_connect",
    "spotify connect",
    "apple_music",
    "apple music",
    "tidal",
    "tidal_connect",
    "tidal connect",
    "qobuz",
    "deezer",
    "amazon_music",
    "amazon music",
    "pandora",
    "iheartradio",
    "napster",
    "rhapsody",
    "siriusxm",
    "soundcloud",
    "tunein",
    "tune in",
    "radioparadise",
    "radio paradise",
    "radiko",
    "juke",
    "alexa",
    "radio",
    "airplay",
    "chromecast",
    # bluetooth (bluesound, musiccast)
    "bluetooth",
    # physical/analog inputs (sonos, heos, musiccast, demo)
    "line-in",
    "linein",
    "line in",
    "line_in",
    "aux",
    "tuner",
    # tv / hdmi (sonos, bluesound)
    "tv",
    "tv input",
    "hdmi arc",
    # local/usb playback on device (musiccast)
    "usb",
    # external (hass_players)
    "external",
}
